"""P2 Redis/LangGraph 短期记忆的纯逻辑测试。"""

import unittest
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.constants import END, START
from langgraph.graph import StateGraph

import app.agent.conversation_graph as conversation_graph_module
from app.agent.context import DataAgentContext
from app.agent.conversation_graph import (
    build_conversation_graph,
    compact_messages,
    format_conversation_context,
    summarize_result,
)
from app.agent.conversation_state import ConversationAgentState
from app.agent.state import DataAgentState
from app.clients.redis_checkpoint_manager import (
    RedisCheckpointManager,
    redis_checkpoint_manager,
)
from app.services.query_service import QueryService


class ConversationMemoryTests(unittest.TestCase):
    def test_compact_messages_keeps_recent_window_and_summarizes_older(self):
        messages = [
            HumanMessage(id="u1", content="统计一季度 GMV"),
            AIMessage(id="a1", content="返回 4 行"),
            HumanMessage(id="u2", content="再按省份拆分"),
            AIMessage(id="a2", content="返回 12 行"),
        ]

        recent, summary = compact_messages(messages, max_messages=2)

        self.assertEqual([message.id for message in recent], ["u2", "a2"])
        self.assertIn("统计一季度 GMV", summary)
        self.assertIn("返回 4 行", summary)

    def test_context_excludes_current_message_and_keeps_sql_summary(self):
        state = ConversationAgentState(
            messages=[
                HumanMessage(id="u1", content="统计 GMV"),
                AIMessage(
                    id="a1",
                    content="查询完成，共返回 4 行结果。",
                    additional_kwargs={
                        "resolved_query": "统计 2025 年第一季度 GMV",
                        "sql": "SELECT SUM(gmv) FROM sales",
                    },
                ),
                HumanMessage(id="u2", content="只看二月份"),
            ],
            conversation_summary="用户此前关注华北地区。",
            current_user_message_id="u2",
        )

        context = format_conversation_context(state)

        self.assertIn("用户此前关注华北地区", context)
        self.assertIn("SELECT SUM(gmv) FROM sales", context)
        self.assertNotIn("只看二月份", context)

    def test_result_summary_never_contains_full_rows(self):
        content, summary = summarize_result([{"region": "华北", "gmv": 100}])

        self.assertEqual(content, "查询完成，共返回 1 行结果。")
        self.assertEqual(summary, {"row_count": 1})
        self.assertNotIn("华北", str(summary))

    def test_long_context_keeps_core_constraints_after_window_compaction(self):
        messages = [
            HumanMessage(
                id="u1",
                content="统计 2025 年第一季度华东地区的 GMV，并按省份分组",
            ),
            AIMessage(id="a1", content="查询完成，共返回 4 行结果。"),
            HumanMessage(id="u2", content="按 GMV 从高到低排序"),
            AIMessage(id="a2", content="查询完成，共返回 4 行结果。"),
            HumanMessage(id="u3", content="排除退款订单"),
            AIMessage(id="a3", content="查询完成，共返回 4 行结果。"),
        ]

        recent, summary = compact_messages(
            messages,
            max_messages=2,
            summary_max_chars=500,
        )
        context = format_conversation_context(
            ConversationAgentState(
                messages=[
                    *recent,
                    HumanMessage(id="u4", content="只看销售额最高的两个省份"),
                ],
                conversation_summary=summary,
                current_user_message_id="u4",
            )
        )

        self.assertIn("2025 年第一季度", context)
        self.assertIn("华东地区", context)
        self.assertIn("GMV", context)
        self.assertIn("按省份分组", context)

    def test_conversation_id_maps_directly_to_thread_id(self):
        config = RedisCheckpointManager.thread_config("conversation-123")
        self.assertEqual(
            config, {"configurable": {"thread_id": "conversation-123"}}
        )


class ConversationGraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_checkpointer_restores_context_without_full_result(self):
        contexts: list[str] = []

        async def fake_query_node(state, runtime):
            contexts.append(state.get("conversation_context", ""))
            runtime.stream_writer(
                {"type": "resolved_query", "query": state["original_query"]}
            )
            runtime.stream_writer({"type": "sql", "sql": "SELECT 1"})
            runtime.stream_writer(
                {"type": "result", "data": [{"sensitive_value": "not-stored"}]}
            )
            return {}

        fake_builder = StateGraph(
            state_schema=DataAgentState,
            context_schema=DataAgentContext,
        )
        fake_builder.add_node("query", fake_query_node)
        fake_builder.add_edge(START, "query")
        fake_builder.add_edge("query", END)
        original_data_graph = conversation_graph_module.data_graph
        conversation_graph_module.data_graph = fake_builder.compile()
        try:
            checkpointer = InMemorySaver()
            graph = build_conversation_graph(
                checkpointer,
                recent_message_limit=4,
                summary_max_chars=500,
            )
            config = {"configurable": {"thread_id": "thread-1"}}

            first_state = ConversationAgentState(
                messages=[HumanMessage(id="u1", content="统计 GMV")],
                current_query="统计 GMV",
                current_user_message_id="u1",
                current_assistant_message_id="a1",
            )
            _ = [
                chunk
                async for chunk in graph.astream(
                    first_state,
                    config=config,
                    context={},
                    stream_mode="custom",
                )
            ]

            second_state = ConversationAgentState(
                messages=[HumanMessage(id="u2", content="再按省份拆分")],
                current_query="再按省份拆分",
                current_user_message_id="u2",
                current_assistant_message_id="a2",
            )
            _ = [
                chunk
                async for chunk in graph.astream(
                    second_state,
                    config=config,
                    context={},
                    stream_mode="custom",
                )
            ]

            snapshot = await graph.aget_state(config)
            self.assertEqual(
                [message.id for message in snapshot.values["messages"]],
                ["u1", "a1", "u2", "a2"],
            )
            self.assertEqual(snapshot.values["last_result_summary"], {"row_count": 1})
            self.assertNotIn("sensitive_value", str(snapshot.values))
            self.assertEqual(contexts[0], "")
            self.assertIn("统计 GMV", contexts[1])
            self.assertIn("SELECT 1", contexts[1])
            self.assertNotIn("再按省份拆分", contexts[1])
            other_snapshot = await graph.aget_state(
                {"configurable": {"thread_id": "thread-2"}}
            )
            self.assertEqual(other_snapshot.values, {})
        finally:
            conversation_graph_module.data_graph = original_data_graph


class RedisFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_redis_failure_is_recorded_in_mysql_turn(self):
        class FakeConversationRepository:
            def __init__(self):
                self.failed: dict | None = None

            async def start_turn(self, conversation_id, query):
                return (
                    SimpleNamespace(id="user-1"),
                    SimpleNamespace(id="assistant-1"),
                )

            async def fail_turn(self, message_id, **kwargs):
                self.failed = {"message_id": message_id, **kwargs}

        repository = FakeConversationRepository()
        service = QueryService(
            meta_mysql_repository=None,
            embedding_client=None,
            dw_mysql_repository=None,
            column_qdrant_repository=None,
            metric_qdrant_repository=None,
            value_es_repository=None,
            conversation_repository=repository,
        )
        original_has_checkpoint = redis_checkpoint_manager.has_checkpoint

        async def unavailable(_thread_id):
            raise ConnectionError("Redis unavailable")

        redis_checkpoint_manager.has_checkpoint = unavailable
        try:
            events = [
                event async for event in service.query("conversation-1", "统计 GMV")
            ]
        finally:
            redis_checkpoint_manager.has_checkpoint = original_has_checkpoint

        self.assertIsNotNone(repository.failed)
        self.assertEqual(repository.failed["message_id"], "assistant-1")
        # P5.4：诊断字段只保留脱敏 code/detail，不再写入原始驱动文案
        self.assertNotIn("Redis unavailable", repository.failed.get("error") or "")
        self.assertNotIn("Redis unavailable", repository.failed.get("content") or "")
        self.assertTrue(
            (repository.failed.get("error") or "").startswith("["),
            repository.failed.get("error"),
        )
        self.assertIn('"type": "error"', events[-1])


if __name__ == "__main__":
    unittest.main()
