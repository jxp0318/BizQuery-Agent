"""问数查询服务与会话持久化编排。"""

import asyncio
import json
import time
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_huggingface import HuggingFaceEndpointEmbeddings

from app.agent.context import DataAgentContext
from app.agent.conversation_graph import compact_messages, summarize_result
from app.agent.conversation_state import ConversationAgentState
from app.clients.redis_checkpoint_manager import redis_checkpoint_manager
from app.conf.app_config import app_config
from app.core.context import request_id_ctx_var
from app.core.error_sanitizer import sanitize_exception
from app.core.log import logger
from app.core.metrics import RunMetrics, set_run_metrics
from app.repositories.es.value_es_repository import ValueESRepository
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository
from app.repositories.mysql.meta.conversation_repository import ConversationRepository
from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository
from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository
from app.services.conversation_lock_manager import conversation_lock_manager


def _sse(event: dict[str, Any]) -> str:
    """把结构化 Agent 事件编码为浏览器可消费的 SSE 数据帧。"""

    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


def _json_safe(value: Any) -> Any:
    """把 Decimal、日期等查询结果转换为 MySQL JSON 与前端均可处理的值。"""

    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


# P5.2：长 LLM 等待超过该秒数则补发 heartbeat，降低代理/网关空闲超时风险
SSE_HEARTBEAT_SECONDS = 15.0


def _enrich_sse(
    event: dict[str, Any],
    *,
    request_id: str,
    started_at: float,
) -> dict[str, Any]:
    """给 SSE 事件统一补 request_id 与 elapsed_ms，供前端展示与排障。"""

    enriched = dict(event)
    enriched.setdefault("requestId", request_id)
    if enriched.get("type") != "heartbeat":
        enriched["elapsedMs"] = int((time.perf_counter() - started_at) * 1000)
    else:
        enriched["elapsedMs"] = int((time.perf_counter() - started_at) * 1000)
    return enriched


class QueryService:
    """编排 MySQL 产品历史、Redis 短期记忆和 NL2SQL 流式执行。

    Service 是一次查询的事务边界协调者：先在 MySQL 建立可诊断 Turn，再运行
    Redis-backed 会话图，最后把完整 SQL 结果和步骤写回 MySQL。它不把外部
    Repository/Client 放入 Graph State，而是通过 Runtime Context 传给节点。
    """

    def __init__(
        self,
        meta_mysql_repository: MetaMySQLRepository,
        embedding_client: HuggingFaceEndpointEmbeddings,
        dw_mysql_repository: DWMySQLRepository,
        column_qdrant_repository: ColumnQdrantRepository,
        metric_qdrant_repository: MetricQdrantRepository,
        value_es_repository: ValueESRepository,
        conversation_repository: ConversationRepository,
    ):
        # 以下依赖只负责原 NL2SQL 单轮子图：Meta MySQL 补齐结构元数据，DW MySQL
        # 执行最终 SQL，Embedding/Qdrant/ES 完成三路 RAG 召回。
        self.meta_mysql_repository = meta_mysql_repository
        self.dw_mysql_repository = dw_mysql_repository
        self.embedding_client = embedding_client
        self.column_qdrant_repository = column_qdrant_repository
        self.metric_qdrant_repository = metric_qdrant_repository
        self.value_es_repository = value_es_repository
        # 会话仓储属于 P1 产品历史链路，负责完整 Turn 的创建、完成和失败持久化。
        self.conversation_repository = conversation_repository

    async def conversation_exists(self, conversation_id: str) -> bool:
        """确认目标会话仍存在，防止向已删除历史继续追加消息。"""

        return await self.conversation_repository.get(conversation_id) is not None

    async def create_conversation(self, title: str) -> str:
        """创建会话并返回 ID；兼容旧 `/api/query` 自动建会话流程。"""

        normalized_title = " ".join(title.split()).strip() or "新会话"
        if len(normalized_title) > 32:
            normalized_title = f"{normalized_title[:32]}…"
        conversation = await self.conversation_repository.create(normalized_title)
        return conversation.id

    async def _get_hydration_memory(
        self,
        conversation_id: str,
        excluded_message_ids: set[str],
    ) -> tuple[list[BaseMessage], str]:
        """Redis 未命中时，从 MySQL 构造精简的初始短期记忆。

        Args:
            conversation_id: 需要恢复的会话，同时会作为 LangGraph thread_id。
            excluded_message_ids: 本轮刚创建的 user/assistant ID，防止它们既从
                MySQL 被恢复、又作为当前输入再次加入 State。

        Returns:
            `(近期 LangChain Messages, 较早历史摘要)`。
        """

        # 多取 excluded 数量，确保排除本轮占位消息后仍能获得配置要求的历史上限。
        messages = await self.conversation_repository.list_messages(
            conversation_id,
            limit=app_config.redis.hydration_message_limit + len(excluded_message_ids),
        )
        memory_messages: list[BaseMessage] = []
        for message in messages:
            if message.id in excluded_message_ids:
                continue
            if message.role == "user":
                memory_messages.append(
                    HumanMessage(id=message.id, content=message.content)
                )
            # 失败或仍在 streaming 的助手输出不是可靠事实，不应影响后续问题改写。
            elif message.status == "done":
                memory_messages.append(
                    AIMessage(
                        id=message.id,
                        content=message.content,
                        additional_kwargs={
                            "resolved_query": message.resolved_query,
                            "sql": message.sql,
                        },
                    )
                )
        return compact_messages(
            memory_messages,
            app_config.redis.recent_message_limit,
            summary_max_chars=app_config.redis.summary_max_chars,
        )

    @staticmethod
    def _upsert_step(
        steps: list[dict[str, Any]], event: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """按步骤名称更新最新状态，供 MySQL 恢复前端执行轨迹。"""

        next_steps = [item for item in steps if item["step"] != event["step"]]
        next_steps.append(
            {
                "step": event["step"],
                "status": event["status"],
                "updatedAt": int(time.time() * 1000),
            }
        )
        return next_steps

    async def query(self, conversation_id: str, query: str):
        """在持久化会话内执行一轮问数，并逐段产出 SSE。

        Args:
            conversation_id: MySQL 会话 ID，也是 Redis/LangGraph thread_id。
            query: 用户本轮原始自然语言问题。

        Yields:
            符合 SSE 格式的 `turn`、`progress`、`resolved_query`、`sql`、
            `result` 或 `error` 事件字符串。

        同一会话的整个流程都在进程内锁中运行，保证消息 position 和 Checkpoint
        更新不会交错；不同会话使用不同锁，可以并行执行。
        """

        # 记录会话锁等待起点，用于观测同会话排队是否成为体验瓶颈。
        lock_wait_started_at = time.perf_counter()
        lock = await conversation_lock_manager.get_lock(conversation_id)
        lock_contended = lock.locked()
        if lock_contended:
            yield _sse(
                {
                    "type": "progress",
                    "step": "等待会话空闲",
                    "status": "running",
                }
            )

        # 锁必须覆盖 MySQL start_turn、Graph 和最终落库，而不是只保护某一次写入，
        # 否则同会话的两个请求仍可能读取同一个旧 Checkpoint 并互相覆盖。
        async with lock:
            logger.info(
                f"conversation_lock_acquired conversation_id={conversation_id} "
                f"wait_ms={(time.perf_counter() - lock_wait_started_at) * 1000:.1f} "
                f"contended={lock_contended}"
            )
            # 这些变量是“一轮请求执行账本”：SSE 事件到达时逐步填充，最后整体写回
            # MySQL。即使中途异常，也能用已经收集到的信息更新 assistant 占位消息。
            assistant_message_id: str | None = None
            steps: list[dict[str, Any]] = []
            resolved_query: str | None = None
            generated_sql: str | None = None
            result_data: Any = None
            # P5.3：结果解释上下文（表/指标口径/日期），与 metrics 一起供前端面板使用
            explain_data: dict[str, Any] | None = None
            # 子图 reject/error 事件里的用户可读说明；非空且无结果时消息应落库为失败。
            stream_error_message: str | None = None
            # 仅表示外层 LangGraph 是否已正常结束，用来判断 Redis 是否可能领先于
            # 尚未成功提交的 MySQL 事实历史。
            graph_completed = False
            # P5.1：本轮指标账本；失败路径也写入已采集部分，便于事后定位。
            request_id = str(request_id_ctx_var.get())
            turn_started_at = time.perf_counter()
            run_metrics = RunMetrics(request_id=request_id)
            set_run_metrics(run_metrics)

            try:
                # 先创建 MySQL Turn，再访问 Redis。这样 Redis 或模型失败时仍有稳定
                # assistant_message_id，可将本轮更新为 error，而不是静默丢失。
                (
                    user_message,
                    assistant_message,
                ) = await self.conversation_repository.start_turn(
                    conversation_id, query
                )
                assistant_message_id = assistant_message.id
                yield _sse(
                    _enrich_sse(
                        {
                            "type": "turn",
                            "conversationId": conversation_id,
                            "userMessageId": user_message.id,
                            "assistantMessageId": assistant_message.id,
                        },
                        request_id=request_id,
                        started_at=turn_started_at,
                    )
                )

                # hit 表示由 LangGraph 自动恢复 Redis State；miss 才从 MySQL 构造
                # hydration_messages 和 hydration_summary 作为首次输入。
                checkpoint_hit = await redis_checkpoint_manager.has_checkpoint(
                    conversation_id
                )
                hydration_messages: list[BaseMessage] = []
                hydration_summary = ""
                if not checkpoint_hit:
                    # 只在 miss 时访问 MySQL 历史；正常命中直接使用 Checkpointer，
                    # 避免每轮都同时读取两套存储并增加一致性复杂度。
                    hydration_messages, hydration_summary = (
                        await self._get_hydration_memory(
                            conversation_id,
                            {user_message.id, assistant_message.id},
                        )
                    )
                    logger.info(
                        f"checkpoint_hydrated thread_id={conversation_id} "
                        f"messages={len(hydration_messages)}"
                    )

                # 使用 MySQL user_message.id 作为 LangChain Message ID，使 Redis 水合、
                # 重试和当前输入能够按稳定 ID 去重，而不是按文本猜测是否重复。
                current_message = HumanMessage(id=user_message.id, content=query)
                state = ConversationAgentState(
                    messages=[*hydration_messages, current_message]
                    if not checkpoint_hit
                    else [current_message],
                    current_query=query,
                    current_user_message_id=user_message.id,
                    current_assistant_message_id=assistant_message.id,
                )
                if not checkpoint_hit:
                    # 命中时不能写入空摘要，否则会覆盖 Checkpointer 恢复的旧摘要。
                    state["conversation_summary"] = hydration_summary
                # Repository 和网络 Client 属于本轮运行依赖，放入 Runtime Context
                # 而不是 State，避免连接对象被 Checkpointer 序列化到 Redis。
                context = DataAgentContext(
                    column_qdrant_repository=self.column_qdrant_repository,
                    embedding_client=self.embedding_client,
                    metric_qdrant_repository=self.metric_qdrant_repository,
                    value_es_repository=self.value_es_repository,
                    meta_mysql_repository=self.meta_mysql_repository,
                    dw_mysql_repository=self.dw_mysql_repository,
                )

                # P5.2：图执行与 SSE 心跳并行。LLM 单次可能十余秒无事件，
                # 直接挂在 astream 上会让代理误判空闲连接；超时则补 heartbeat。
                graph_events: asyncio.Queue = asyncio.Queue()
                graph_done = object()
                graph_error: list[BaseException] = []

                async def _pump_graph():
                    try:
                        async for chunk in redis_checkpoint_manager.graph.astream(
                            input=state,
                            config=redis_checkpoint_manager.thread_config(
                                conversation_id
                            ),
                            context=context,
                            stream_mode="custom",
                        ):
                            await graph_events.put(chunk)
                    except BaseException as exc:  # noqa: BLE001 - 原样转抛给外层
                        graph_error.append(exc)
                    finally:
                        await graph_events.put(graph_done)

                pump_task = asyncio.create_task(_pump_graph())
                try:
                    while True:
                        try:
                            item = await asyncio.wait_for(
                                graph_events.get(),
                                timeout=SSE_HEARTBEAT_SECONDS,
                            )
                        except asyncio.TimeoutError:
                            yield _sse(
                                _enrich_sse(
                                    {"type": "heartbeat"},
                                    request_id=request_id,
                                    started_at=turn_started_at,
                                )
                            )
                            continue
                        if item is graph_done:
                            break
                        chunk = item
                        # SSE 一边转发给浏览器，一边提取写入 MySQL 的执行证据；
                        # 这样刷新历史会话后仍能恢复步骤、独立问题、SQL 和完整结果。
                        if chunk.get("type") == "progress":
                            steps = self._upsert_step(steps, chunk)
                        elif chunk.get("type") == "resolved_query":
                            resolved_query = chunk.get("query")
                        elif chunk.get("type") == "sql":
                            generated_sql = chunk.get("sql")
                        elif chunk.get("type") == "result":
                            result_data = _json_safe(chunk.get("data"))
                        elif chunk.get("type") == "explain":
                            explain_data = chunk.get("data") or None
                        elif chunk.get("type") == "error":
                            stream_error_message = (
                                chunk.get("message") or "查询未成功"
                            )
                        yield _sse(
                            _enrich_sse(
                                chunk,
                                request_id=request_id,
                                started_at=turn_started_at,
                            )
                        )
                finally:
                    if not pump_task.done():
                        pump_task.cancel()
                        try:
                            await pump_task
                        except (asyncio.CancelledError, Exception):
                            pass
                if graph_error:
                    raise graph_error[0]
                graph_completed = True
                # P5.1：先推指标事件，再写 MySQL，避免落库失败丢观测数据。
                metrics_payload = run_metrics.to_dict()
                if explain_data is not None:
                    metrics_payload["explain"] = explain_data
                logger.info(f"run_metrics {metrics_payload}")
                yield _sse(
                    _enrich_sse(
                        {"type": "metrics", **metrics_payload},
                        request_id=request_id,
                        started_at=turn_started_at,
                    )
                )
                # 图结束后采集 State 体积与快照加载耗时，监控短期记忆是否膨胀。
                await redis_checkpoint_manager.capture_state_metrics(conversation_id)

                assistant_content, _ = summarize_result(result_data)
                # Redis 主图已经完成后，再把完整结果提交到 MySQL 事实历史。
                # 若这里失败，异常分支会删除已领先的 Redis Thread，下轮重新水合。
                # reject_sql 等路径只发 error 事件、不抛异常：必须落库为失败，
                # 避免「放弃修正」在历史里显示为 status=done 的成功消息。
                if stream_error_message and result_data is None:
                    # 子图 error 事件已使用 USER_MESSAGES；诊断字段与展示字段一致即可
                    await self.conversation_repository.fail_turn(
                        assistant_message.id,
                        content=stream_error_message,
                        error=stream_error_message,
                        steps=steps,
                        metrics=run_metrics.to_dict(),
                    )
                else:
                    await self.conversation_repository.finish_turn(
                        assistant_message.id,
                        content=assistant_content,
                        resolved_query=resolved_query,
                        sql=generated_sql,
                        result=result_data,
                        steps=steps,
                        metrics=run_metrics.to_dict(),
                    )
            except asyncio.CancelledError:
                # 浏览器主动中止流时保留明确的 cancelled 历史，但必须继续抛出
                # CancelledError，让 ASGI 栈真正停止后续协程，而不是误报普通错误。
                if assistant_message_id is not None:
                    await self.conversation_repository.fail_turn(
                        assistant_message_id,
                        content="已停止本次查询。",
                        error="查询被用户取消。",
                        steps=steps,
                        status="cancelled",
                        metrics=run_metrics.to_dict(),
                    )
                raise
            except Exception as error:
                # StreamingResponse 一旦开始发送就不能再改 HTTP 状态码，因此异常既要
                # 持久化到 MySQL，也要编码为 SSE error 事件通知当前页面。
                # P5.4：用户只拿脱敏短句；完整异常仅进日志，避免泄露连接串/账号。
                safe = sanitize_exception(error)
                logger.error(
                    f"query_failed request_id={request_id} code={safe.code} "
                    f"detail={safe.detail} error={error!r}"
                )
                if assistant_message_id is not None:
                    await self.conversation_repository.fail_turn(
                        assistant_message_id,
                        content=safe.for_user(),
                        # 诊断字段保留 code 与安全 detail，不写原始驱动报错
                        error=f"[{safe.code}] {safe.detail}",
                        steps=steps,
                        metrics=run_metrics.to_dict(),
                    )
                if graph_completed:
                    # 图已经保存而 MySQL 最终写入失败时，删除可重建的 Redis Thread，
                    # 下一轮从 MySQL 重新水合；删除失败进入后台幂等重试，不再只打日志。
                    await redis_checkpoint_manager.delete_thread_safely(conversation_id)
                yield _sse(
                    _enrich_sse(
                        {
                            "type": "error",
                            "message": safe.for_user(),
                            "code": safe.code,
                            "requestId": request_id,
                        },
                        request_id=request_id,
                        started_at=turn_started_at,
                    )
                )
            finally:
                # 解绑 ContextVar，避免同协程复用时串到下一轮。
                set_run_metrics(None)
