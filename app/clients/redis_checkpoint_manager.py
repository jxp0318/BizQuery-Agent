"""Redis-backed LangGraph Checkpointer 生命周期管理。"""

import asyncio
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

    async def delete_thread(self, thread_id: str) -> None:
        """删除指定会话的 Checkpoint 与关联 Pending Writes。

        MySQL 删除由业务 API 先行确认；这里清理的是可重建 Runtime State，失败时
        调用方应记录错误，但不回滚用户已经确认的历史删除。
        """

        if self.saver is None:
            raise RuntimeError("Redis Checkpointer 尚未初始化。")
        await self.saver.adelete_thread(thread_id)
        logger.info(f"checkpoint_deleted thread_id={thread_id}")

    async def close(self) -> None:
        """释放 Saver 连接并清空已编译图引用，供应用安全退出或测试重建。"""

        if self._context_manager is not None:
            await self._context_manager.__aexit__(None, None, None)
        self.saver = None
        self._context_manager = None
        self._graph = None


redis_checkpoint_manager = RedisCheckpointManager()
