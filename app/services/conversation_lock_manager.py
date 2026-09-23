"""同一进程内按会话串行化问数任务。"""

import asyncio


class ConversationLockManager:
    """为单 FastAPI 进程提供会话级串行控制。

    同一个 conversation_id 共用一把锁，防止两次请求同时计算消息 position、
    覆盖 Redis Thread State 或交错写入 SSE 结果；不同会话使用不同锁，仍可并行。
    该实现不跨进程，多 Worker 部署前必须替换为分布式锁和数据库原子序号。
    """

    def __init__(self):
        # conversation_id -> 会话专属业务锁；相同会话复用同一实例，不同会话互不阻塞。
        self._locks: dict[str, asyncio.Lock] = {}
        # 只保护上面字典的“查找或创建”，不覆盖耗时的 Agent 执行过程。
        self._guard = asyncio.Lock()

    async def get_lock(self, conversation_id: str) -> asyncio.Lock:
        """取得会话专属锁，不在这里直接等待业务锁。

        Args:
            conversation_id: 需要串行化的会话 ID。

        Returns:
            当前进程内与该会话稳定对应的 `asyncio.Lock`。
        """

        # _guard 只保护“查找或创建锁”这一瞬间，避免两个协程同时为同一会话
        # 创建不同锁；真正耗时的问数流程在返回的会话锁上等待。
        async with self._guard:
            return self._locks.setdefault(conversation_id, asyncio.Lock())

# 应用进程内共享同一个管理器，路由请求才能取得相同 conversation_id 对应的同一把锁。
conversation_lock_manager = ConversationLockManager()
