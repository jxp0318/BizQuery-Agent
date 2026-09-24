"""Redis-backed LangGraph Checkpointer 生命周期管理。"""

import asyncio
import json
import sys
import time
from typing import Any

from langgraph.checkpoint.redis.aio import AsyncRedisSaver

from app.agent.conversation_graph import build_conversation_graph
from app.conf.app_config import app_config
from app.core.log import logger


class RedisCheckpointManager:
    """在 FastAPI 生命周期内复用 Redis Saver 和会话主图。

    Saver 持有连接池和 Redis Search 索引，不适合在每个 HTTP 请求中重复创建。
    manager 只管理可重建的 Agent Runtime State；完整历史仍由 MySQL 仓储负责。
    """

    def __init__(self) -> None:
        # AsyncRedisSaver 是 LangGraph 读写 Checkpoint 的公开入口；None 表示应用
        # 尚未完成 lifespan 初始化，或已经进入关闭阶段。
        self.saver: AsyncRedisSaver | None = None
        # 保存 from_conn_string 返回的异步上下文管理器，关闭时通过它释放连接池。
        self._context_manager: Any = None
        # 绑定当前 saver 编译出的会话主图；应用级复用，避免每个请求重复编译。
        self._graph: Any = None
        # 删除失败的 thread_id 集合；单 Worker 进程内重试，不引入额外外部队列。
        self._pending_deletes: set[str] = set()
        # 每个 thread_id 已尝试删除的次数，超过 delete_retry_max_attempts 后停止。
        self._delete_attempts: dict[str, int] = {}
        # 后台幂等重试协程；与 pending 集合配套启停。
        self._delete_retry_task: asyncio.Task | None = None

    @property
    def graph(self):
        """返回已绑定 Redis Checkpointer 的会话主图。

        Raises:
            RuntimeError: FastAPI lifespan 尚未完成初始化，避免请求静默退化为无记忆图。
        """

        if self._graph is None:
            raise RuntimeError("Redis Checkpointer 尚未初始化。")
        return self._graph

    @staticmethod
    def thread_config(thread_id: str) -> dict[str, dict[str, str]]:
        """生成 LangGraph 线程配置，使 conversation_id 成为状态隔离边界。"""

        return {"configurable": {"thread_id": thread_id}}

    @staticmethod
    def _thread_id_from_config(config: Any) -> str:
        """从 Checkpointer 配置中取出 thread_id，用于指标日志关联。"""

        try:
            return str((config or {}).get("configurable", {}).get("thread_id", "-"))
        except Exception:
            return "-"

    def _instrument_saver(self, saver: AsyncRedisSaver) -> None:
        """包装 Checkpointer 写入方法，记录保存耗时。

        LangGraph 在节点边界自动调用 aput/aput_writes；这里只叠加计时日志，
        不改变持久化语义，便于观察 Redis 写入是否成为问数链路瓶颈。
        """

        original_aput = saver.aput

        async def aput_with_metrics(config: Any, *args: Any, **kwargs: Any):
            started_at = time.perf_counter()
            result = await original_aput(config, *args, **kwargs)
            logger.info(
                f"checkpoint_save thread_id={self._thread_id_from_config(config)} "
                f"save_ms={(time.perf_counter() - started_at) * 1000:.1f}"
            )
            return result

        saver.aput = aput_with_metrics

        original_aput_writes = getattr(saver, "aput_writes", None)
        if original_aput_writes is not None:

            async def aput_writes_with_metrics(config: Any, *args: Any, **kwargs: Any):
                started_at = time.perf_counter()
                result = await original_aput_writes(config, *args, **kwargs)
                logger.info(
                    f"checkpoint_save_writes "
                    f"thread_id={self._thread_id_from_config(config)} "
                    f"save_ms={(time.perf_counter() - started_at) * 1000:.1f}"
                )
                return result

            saver.aput_writes = aput_writes_with_metrics

    async def init(self) -> None:
        """连接 Redis、创建 Checkpoint 索引并编译会话主图。

        启动失败会按配置重试，耗尽后阻止应用进入就绪状态。短期记忆是当前问数
        链路的明确依赖，因此不能在 Redis 不可用时悄悄退回进程内记忆。

        Raises:
            RuntimeError: 所有启动重试均失败。
        """

        # 使用局部别名是为了让本次初始化读取同一份配置快照，也缩短重试代码。
        redis_config = app_config.redis
        # 保留最后一次异常，所有重试耗尽后把真实根因串到 RuntimeError 中。
        last_error: Exception | None = None
        for attempt in range(1, redis_config.startup_retries + 1):
            try:
                # 使用异步上下文管理器创建 Saver，确保应用退出时能够关闭底层
                # Redis 连接；TTL 作用于可重建 Checkpoint，不影响 MySQL 历史。
                context_manager = AsyncRedisSaver.from_conn_string(
                    redis_config.url,
                    connection_args={
                        "socket_connect_timeout": redis_config.connect_timeout_seconds,
                        "socket_timeout": redis_config.operation_timeout_seconds,
                        "retry_on_timeout": True,
                        "health_check_interval": 30,
                    },
                    ttl={
                        "default_ttl": redis_config.checkpoint_ttl_minutes,
                        "refresh_on_read": redis_config.refresh_on_read,
                    },
                )
                saver = await context_manager.__aenter__()
                try:
                    # Redis Saver 依赖 Search/JSON 索引。asetup 可重复执行，因此
                    # 每次进程启动都调用，兼容新建或保留的数据卷。
                    await saver.asetup()
                except Exception:
                    await context_manager.__aexit__(*sys.exc_info())
                    raise

                self._context_manager = context_manager
                self.saver = saver
                self._instrument_saver(saver)
                self._graph = build_conversation_graph(
                    saver,
                    recent_message_limit=redis_config.recent_message_limit,
                    summary_max_chars=redis_config.summary_max_chars,
                )
                logger.info("Redis LangGraph Checkpointer 初始化完成")
                return
            except Exception as error:
                last_error = error
                logger.warning(
                    f"Redis Checkpointer 初始化失败（{attempt}/"
                    f"{redis_config.startup_retries}）：{error}"
                )
                if attempt < redis_config.startup_retries:
                    await asyncio.sleep(redis_config.retry_delay_seconds)

        raise RuntimeError(f"Redis Checkpointer 初始化失败：{last_error}") from last_error

    async def has_checkpoint(self, thread_id: str) -> bool:
        """检查 Thread 是否已有可恢复 State，并记录读取耗时。

        Args:
            thread_id: 与 MySQL conversation_id 相同的线程标识。

        Returns:
            True 表示下一轮可直接由 Redis 恢复；False 表示需要从 MySQL 懒恢复。
        """

        if self.saver is None:
            raise RuntimeError("Redis Checkpointer 尚未初始化。")
        # perf_counter 使用单调高精度时钟，适合记录耗时，不受系统时间调整影响。
        started_at = time.perf_counter()
        checkpoint = await self.saver.aget_tuple(self.thread_config(thread_id))
        logger.info(
            f"checkpoint_{'hit' if checkpoint else 'miss'} "
            f"thread_id={thread_id} load_ms="
            f"{(time.perf_counter() - started_at) * 1000:.1f}"
        )
        return checkpoint is not None

    async def capture_state_metrics(self, thread_id: str) -> None:
        """记录当前 Thread 序列化后 State 体积，监控短期记忆是否膨胀。

        Args:
            thread_id: 与 conversation_id 相同的线程标识。

        只做观测，不修改 State。完整结果本就不应进入 Checkpoint；若 state_bytes
        持续变大，优先检查是否误把召回结果或大结果写入了 ConversationAgentState。
        """

        if self.saver is None or self._graph is None:
            return
        started_at = time.perf_counter()
        try:
            snapshot = await self._graph.aget_state(self.thread_config(thread_id))
            values = snapshot.values if snapshot is not None else {}
            state_bytes = len(
                json.dumps(values, ensure_ascii=False, default=str).encode("utf-8")
            )
            logger.info(
                f"checkpoint_state_metrics thread_id={thread_id} "
                f"state_bytes={state_bytes} "
                f"snapshot_load_ms={(time.perf_counter() - started_at) * 1000:.1f}"
            )
        except Exception as error:
            logger.warning(
                f"checkpoint_state_metrics_failed thread_id={thread_id}: {error}"
            )

    async def delete_thread(self, thread_id: str) -> None:
        """删除指定会话的 Checkpoint 与关联 Pending Writes。

        MySQL 删除由业务 API 先行确认；这里清理的是可重建 Runtime State，失败时
        调用方应记录错误，但不回滚用户已经确认的历史删除。
        """

        if self.saver is None:
            raise RuntimeError("Redis Checkpointer 尚未初始化。")
        await self.saver.adelete_thread(thread_id)
        logger.info(f"checkpoint_deleted thread_id={thread_id}")

    async def delete_thread_safely(self, thread_id: str) -> bool:
        """删除 Thread；失败时进入后台幂等重试队列，不向上抛出业务错误。

        Args:
            thread_id: 需要清理的 LangGraph thread_id。

        Returns:
            True 表示本次删除成功；False 表示已排队等待后台重试。

        删除目标是可重建 Runtime State，重复删除天然幂等。MySQL 产品历史删除
        成功后，即使本轮 Redis 删除失败，也必须保证用户删除语义成立。
        """

        try:
            await self.delete_thread(thread_id)
            self._pending_deletes.discard(thread_id)
            self._delete_attempts.pop(thread_id, None)
            return True
        except Exception as error:
            self._pending_deletes.add(thread_id)
            self._delete_attempts.setdefault(thread_id, 0)
            self._ensure_delete_retry_worker()
            logger.error(
                f"checkpoint_delete_failed thread_id={thread_id} "
                f"queued_for_retry=true: {error}"
            )
            return False

    def _ensure_delete_retry_worker(self) -> None:
        """按需启动后台删除重试协程；单 Worker 下进程内队列足够。"""

        if self._delete_retry_task is None or self._delete_retry_task.done():
            self._delete_retry_task = asyncio.create_task(self._run_delete_retries())

    async def _run_delete_retries(self) -> None:
        """对 pending Thread 做有限次幂等重试，避免删除失败只停留在错误日志。"""

        redis_config = app_config.redis
        while self._pending_deletes and self.saver is not None:
            await asyncio.sleep(redis_config.delete_retry_interval_seconds)
            for thread_id in list(self._pending_deletes):
                if self.saver is None:
                    return
                attempts = self._delete_attempts.get(thread_id, 0) + 1
                self._delete_attempts[thread_id] = attempts
                try:
                    await self.delete_thread(thread_id)
                    self._pending_deletes.discard(thread_id)
                    self._delete_attempts.pop(thread_id, None)
                    logger.info(
                        f"checkpoint_delete_retry_success thread_id={thread_id} "
                        f"attempts={attempts}"
                    )
                except Exception as error:
                    if attempts >= redis_config.delete_retry_max_attempts:
                        self._pending_deletes.discard(thread_id)
                        self._delete_attempts.pop(thread_id, None)
                        logger.error(
                            f"checkpoint_delete_retry_exhausted "
                            f"thread_id={thread_id} attempts={attempts}: {error}"
                        )
                    else:
                        logger.warning(
                            f"checkpoint_delete_retry_failed "
                            f"thread_id={thread_id} attempts={attempts}: {error}"
                        )

    async def close(self) -> None:
        """释放 Saver 连接并清空已编译图引用，供应用安全退出或测试重建。"""

        if self._delete_retry_task is not None and not self._delete_retry_task.done():
            self._delete_retry_task.cancel()
            try:
                await self._delete_retry_task
            except asyncio.CancelledError:
                pass
        self._delete_retry_task = None
        if self._pending_deletes:
            logger.warning(
                "checkpoint_delete_pending_on_close "
                f"thread_ids={sorted(self._pending_deletes)}"
            )
        self._pending_deletes.clear()
        self._delete_attempts.clear()
        if self._context_manager is not None:
            await self._context_manager.__aexit__(None, None, None)
        self.saver = None
        self._context_manager = None
        self._graph = None


redis_checkpoint_manager = RedisCheckpointManager()
