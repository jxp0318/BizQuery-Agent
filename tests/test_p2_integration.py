"""P2 Redis + MySQL 双存储集成路径测试。

用 InMemorySaver 复现 Checkpointer 线程语义，用假仓储复现 MySQL Turn 生命周期，
覆盖：新建会话首问、同会话续聊命中 Checkpoint、miss 懒恢复、删除幂等重试和
锁等待指标日志路径。不要求本机 Docker 全部在线，便于 CI 重复执行。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from langgraph.checkpoint.memory import InMemorySaver

import app.agent.conversation_graph as conversation_graph_module
from app.agent.context import DataAgentContext
from app.agent.conversation_graph import build_conversation_graph
from app.agent.state import DataAgentState
from app.clients.redis_checkpoint_manager import (
    RedisCheckpointManager,
    redis_checkpoint_manager,
)
from app.services.query_service import QueryService


class FakeConversationRepository:
    """内存版会话仓储，记录 Turn 生命周期供断言。"""

    def __init__(self):
        self.conversations: dict[str, dict] = {}
        self.messages: dict[str, list] = {}
        self.finished: list[dict] = []
        self.failed: list[dict] = []
        self._seq = 0

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    async def create(self, title: str):
        conversation_id = self._next_id("c")
        self.conversations[conversation_id] = {
            "id": conversation_id,
            "title": title,
        }
        self.messages[conversation_id] = []
        return SimpleNamespace(id=conversation_id, title=title)

    async def start_turn(self, conversation_id: str, query: str):
        user = SimpleNamespace(id=self._next_id("u"), role="user", content=query)
        assistant = SimpleNamespace(id=self._next_id("a"), role="assistant")
        self.messages.setdefault(conversation_id, []).append(user)
        self.messages[conversation_id].append(assistant)
        return user, assistant

    async def finish_turn(self, message_id: str, **kwargs):
        self.finished.append({"message_id": message_id, **kwargs})

    async def fail_turn(self, message_id: str, **kwargs):
        self.failed.append({"message_id": message_id, **kwargs})

    async def list_messages(self, conversation_id: str, limit: int | None = None):
        items = self.messages.get(conversation_id, [])
        if limit is not None:
            items = items[-limit:]
        # 懒恢复只应消费已完成的助手消息；测试里补齐 status/sql 字段。
        restored = []
        for item in items:
            role = getattr(item, "role", None)
            content = getattr(item, "content", "")
            if role == "user":
                restored.append(
                    SimpleNamespace(
                        id=item.id,
                        role="user",
                        content=content,
                        status="done",
                        resolved_query=None,
                        sql=None,
                        steps=None,
                    )
                )
            else:
                restored.append(
                    SimpleNamespace(
                        id=item.id,
                        role="assistant",
                        content=content,
                        status="done",
                        resolved_query=getattr(item, "resolved_query", None),
                        sql=getattr(item, "sql", None),
                        steps=None,
                    )
                )
        return restored


class P2IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repository = FakeConversationRepository()
        self.checkpointer = InMemorySaver()
        self.manager = RedisCheckpointManager()
        self.manager.saver = self.checkpointer
        self.manager._graph = build_conversation_graph(
            self.checkpointer,
            recent_message_limit=8,
            summary_max_chars=500,
        )

        async def fake_query_node(state, runtime):
            runtime.stream_writer(
                {"type": "resolved_query", "query": state["original_query"]}
            )
            runtime.stream_writer({"type": "sql", "sql": "SELECT 1 AS ok"})
            runtime.stream_writer(
                {"type": "result", "data": [{"ok": 1, "secret": "no-store"}]}
            )
            return {}

        builder_module = conversation_graph_module
        fake_builder_state = DataAgentState
        from langgraph.constants import END, START
        from langgraph.graph import StateGraph

        fake_builder = StateGraph(
            state_schema=fake_builder_state,
            context_schema=DataAgentContext,
        )
        fake_builder.add_node("query", fake_query_node)
        fake_builder.add_edge(START, "query")
        fake_builder.add_edge("query", END)
        self._original_data_graph = builder_module.data_graph
        builder_module.data_graph = fake_builder.compile()

        self.service = QueryService(
            meta_mysql_repository=None,
            embedding_client=None,
            dw_mysql_repository=None,
            column_qdrant_repository=None,
            metric_qdrant_repository=None,
            value_es_repository=None,
            conversation_repository=self.repository,
        )
        self._original_manager_graph = redis_checkpoint_manager._graph
        self._original_manager_saver = redis_checkpoint_manager.saver
        self._original_delete_safely = redis_checkpoint_manager.delete_thread_safely
        self._original_capture = redis_checkpoint_manager.capture_state_metrics
        self._original_has_checkpoint = redis_checkpoint_manager.has_checkpoint
        redis_checkpoint_manager._graph = self.manager._graph
        redis_checkpoint_manager.saver = self.manager.saver
        redis_checkpoint_manager.delete_thread_safely = (
            self.manager.delete_thread_safely
        )
        redis_checkpoint_manager.capture_state_metrics = (
            self.manager.capture_state_metrics
        )
        redis_checkpoint_manager.has_checkpoint = self.manager.has_checkpoint

    async def asyncTearDown(self):
        conversation_graph_module.data_graph = self._original_data_graph
        redis_checkpoint_manager._graph = self._original_manager_graph
        redis_checkpoint_manager.saver = self._original_manager_saver
        redis_checkpoint_manager.delete_thread_safely = self._original_delete_safely
        redis_checkpoint_manager.capture_state_metrics = self._original_capture
        redis_checkpoint_manager.has_checkpoint = self._original_has_checkpoint
        await self.manager.close()

    async def _run_query(self, conversation_id: str, query: str) -> list[str]:
        return [event async for event in self.service.query(conversation_id, query)]

    async def test_new_conversation_persists_turn_and_checkpoint(self):
        conversation_id = "conv-new"
        events = await self._run_query(conversation_id, "统计一季度 GMV")

        self.assertTrue(any('"type": "result"' in event for event in events))
        self.assertEqual(len(self.repository.finished), 1)
        self.assertEqual(self.repository.failed, [])
        self.assertEqual(self.repository.finished[0]["sql"], "SELECT 1 AS ok")
        # 完整结果只进 MySQL，不进 Checkpoint。
        self.assertIn("secret", str(self.repository.finished[0]["result"]))
        snapshot = await self.manager.graph.aget_state(
            RedisCheckpointManager.thread_config(conversation_id)
        )
        self.assertNotIn("secret", str(snapshot.values))
        self.assertEqual(snapshot.values["last_result_summary"], {"row_count": 1})

    async def test_followup_hits_checkpoint_without_hydration_duplication(self):
        conversation_id = "conv-hit"
        await self._run_query(conversation_id, "统计华东 GMV")
        self.assertTrue(await self.manager.has_checkpoint(conversation_id))

        events = await self._run_query(conversation_id, "再按省份拆分")
        self.assertTrue(any('"type": "result"' in event for event in events))
        self.assertEqual(len(self.repository.finished), 2)

        snapshot = await self.manager.graph.aget_state(
            RedisCheckpointManager.thread_config(conversation_id)
        )
        message_ids = [message.id for message in snapshot.values["messages"]]
        self.assertEqual(len(message_ids), len(set(message_ids)))
        self.assertGreaterEqual(len(message_ids), 4)

    async def test_miss_hydrates_from_mysql_history(self):
        conversation_id = "conv-hydrate"
        await self.run_conversation_without_checkpoint(conversation_id)

        self.assertFalse(await self.manager.has_checkpoint(conversation_id))
        await self._run_query(conversation_id, "只看二月份")

        snapshot = await self.manager.graph.aget_state(
            RedisCheckpointManager.thread_config(conversation_id)
        )
        contents = [str(message.content) for message in snapshot.values["messages"]]
        joined = "\n".join(contents)
        self.assertTrue(any("历史问题" in item for item in contents) or "历史问题" in snapshot.values.get("conversation_summary", ""))
        self.assertIn("只看二月份", joined)

    async def run_conversation_without_checkpoint(self, conversation_id: str) -> None:
        """先走一遍完整 MySQL 历史，再清掉 Redis Thread，模拟冷启动懒恢复。"""

        await self._run_query(conversation_id, "历史问题：统计华北 GMV")
        self.repository.messages[conversation_id].extend(
            [
                SimpleNamespace(
                    id="hist-u",
                    role="user",
                    content="历史问题：统计华北 GMV",
                ),
                SimpleNamespace(
                    id="hist-a",
                    role="assistant",
                    content="查询完成，共返回 2 行结果。",
                    resolved_query="统计华北地区 GMV",
                    sql="SELECT 1",
                ),
            ]
        )
        await self.manager.delete_thread(conversation_id)

    async def test_delete_thread_safely_retries_until_success(self):
        thread_id = "conv-delete-retry"
        attempts = {"count": 0}

        async def flaky_delete(target: str) -> None:
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise ConnectionError("redis flaky")
            self.manager._pending_deletes.discard(target)

        self.manager.delete_thread = flaky_delete  # type: ignore[method-assign]
        queued = await self.manager.delete_thread_safely(thread_id)
        self.assertFalse(queued)
        self.assertIn(thread_id, self.manager._pending_deletes)

        # 缩短重试间隔，避免测试空等。
        from app.conf.app_config import app_config

        original_interval = app_config.redis.delete_retry_interval_seconds
        original_max = app_config.redis.delete_retry_max_attempts
        app_config.redis.delete_retry_interval_seconds = 0.01
        app_config.redis.delete_retry_max_attempts = 5
        try:
            self.manager._ensure_delete_retry_worker()
            for _ in range(50):
                if thread_id not in self.manager._pending_deletes:
                    break
                await asyncio.sleep(0.02)
        finally:
            app_config.redis.delete_retry_interval_seconds = original_interval
            app_config.redis.delete_retry_max_attempts = original_max

        self.assertNotIn(thread_id, self.manager._pending_deletes)
        self.assertGreaterEqual(attempts["count"], 3)

    async def test_same_conversation_serializes_turns(self):
        conversation_id = "conv-lock"
        started = asyncio.Event()
        release = asyncio.Event()

        original_start_turn = self.repository.start_turn

        async def slow_start_turn(cid, query):
            if query == "first":
                started.set()
                await release.wait()
            return await original_start_turn(cid, query)

        self.repository.start_turn = slow_start_turn  # type: ignore[method-assign]
        first = asyncio.create_task(self._run_query(conversation_id, "first"))
        await started.wait()
        second = asyncio.create_task(self._run_query(conversation_id, "second"))
        await asyncio.sleep(0.02)
        release.set()
        await asyncio.gather(first, second)

        # 同一会话两次 Turn 均完成，且消息不交错丢失。
        self.assertEqual(len(self.repository.finished), 2)


if __name__ == "__main__":
    unittest.main()
