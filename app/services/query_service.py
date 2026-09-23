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
from app.core.log import logger
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

        lock = await conversation_lock_manager.get_lock(conversation_id)
        if lock.locked():
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
            # 这些变量是“一轮请求执行账本”：SSE 事件到达时逐步填充，最后整体写回
            # MySQL。即使中途异常，也能用已经收集到的信息更新 assistant 占位消息。
            assistant_message_id: str | None = None
            steps: list[dict[str, Any]] = []
            resolved_query: str | None = None
            generated_sql: str | None = None
            result_data: Any = None
            # 仅表示外层 LangGraph 是否已正常结束，用来判断 Redis 是否可能领先于
            # 尚未成功提交的 MySQL 事实历史。
            graph_completed = False

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
                    {
                        "type": "turn",
                        "conversationId": conversation_id,
                        "userMessageId": user_message.id,
                        "assistantMessageId": assistant_message.id,
                    }
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

                async for chunk in redis_checkpoint_manager.graph.astream(
                    input=state,
                    config=redis_checkpoint_manager.thread_config(conversation_id),
                    context=context,
                    stream_mode="custom",
                ):
                    # SSE 一边转发给浏览器，一边提取需要写入 MySQL 的执行证据；
                    # 这样刷新历史会话后仍能恢复步骤、独立问题、SQL 和完整结果。
                    if chunk.get("type") == "progress":
                        steps = self._upsert_step(steps, chunk)
                    elif chunk.get("type") == "resolved_query":
                        resolved_query = chunk.get("query")
                    elif chunk.get("type") == "sql":
                        generated_sql = chunk.get("sql")
                    elif chunk.get("type") == "result":
                        result_data = _json_safe(chunk.get("data"))
                    yield _sse(chunk)
                graph_completed = True

                assistant_content, _ = summarize_result(result_data)
                # Redis 主图已经完成后，再把完整结果提交到 MySQL 事实历史。
                # 若这里失败，异常分支会删除已领先的 Redis Thread，下轮重新水合。
                await self.conversation_repository.finish_turn(
                    assistant_message.id,
                    content=assistant_content,
                    resolved_query=resolved_query,
                    sql=generated_sql,
                    result=result_data,
                    steps=steps,
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
                    )
                raise
            except Exception as error:
                # StreamingResponse 一旦开始发送就不能再改 HTTP 状态码，因此异常既要
                # 持久化到 MySQL，也要编码为 SSE error 事件通知当前页面。
                if assistant_message_id is not None:
                    await self.conversation_repository.fail_turn(
                        assistant_message_id,
                        content="这次查询没有成功。",
                        error=str(error),
                        steps=steps,
                    )
                if graph_completed:
                    # 图已经保存而 MySQL 最终写入失败时，删除可重建的 Redis Thread，
                    # 下一轮从 MySQL 重新水合，避免短期状态领先于事实历史。
                    try:
                        await redis_checkpoint_manager.delete_thread(conversation_id)
                    except Exception as cleanup_error:
                        logger.error(
                            f"checkpoint_cleanup_failed thread_id={conversation_id}: "
                            f"{cleanup_error}"
                        )
                yield _sse({"type": "error", "message": str(error)})
