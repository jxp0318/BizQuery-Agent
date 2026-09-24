"""P3 修正循环：sql_guard 真检 + 路由语义 + reject 文案。"""

from __future__ import annotations

import unittest

import app.agent.graph as graph_module
from app.agent.nodes.reject_sql import reject_sql
from app.agent.nodes.sql_guard import sql_guard
from app.agent.sql_errors import CORRECTION_EXHAUSTED, UNSAFE_STATEMENT
from app.agent.sql_guard import check_sql
from app.agent.state import DataAgentState


class _Writer:
    def __init__(self) -> None:
        self.chunks: list[dict] = []

    def __call__(self, chunk) -> None:
        self.chunks.append(chunk)


class _Runtime:
    def __init__(self, writer: _Writer) -> None:
        self.stream_writer = writer
        self.context: dict = {}


class CorrectionLoopTests(unittest.IsolatedAsyncioTestCase):
    def test_route_allows_two_corrections_then_reject(self):
        """校验最多 3 轮：correct_sql 最多 2 次。"""

        self.assertEqual(graph_module._can_correct({"sql_correction_count": 0}), "correct_sql")
        self.assertEqual(graph_module._can_correct({"sql_correction_count": 1}), "correct_sql")
        self.assertEqual(graph_module._can_correct({"sql_correction_count": 2}), "reject_sql")

    def test_route_after_guard(self):
        self.assertEqual(
            graph_module._route_after_guard({"error": None}), "validate_sql"
        )
        self.assertEqual(
            graph_module._route_after_guard(
                {"error": "x", "sql_correction_count": 0}
            ),
            "correct_sql",
        )
        self.assertEqual(
            graph_module._route_after_guard(
                {"error": "x", "sql_correction_count": 2}
            ),
            "reject_sql",
        )

    def test_route_after_validate(self):
        self.assertEqual(
            graph_module._route_after_validate({"error": None}), "run_sql"
        )
        self.assertEqual(
            graph_module._route_after_validate(
                {"error": "x", "sql_correction_count": 1}
            ),
            "correct_sql",
        )

    async def test_sql_guard_rejects_write_and_sets_code(self):
        writer = _Writer()
        state = DataAgentState(
            query="q",
            original_query="q",
            sql="DELETE FROM fact_order",
            sql_correction_count=0,
        )
        update = await sql_guard(state, _Runtime(writer))  # type: ignore[arg-type]
        self.assertIsNotNone(update["error"])
        self.assertEqual(update["error_code"], UNSAFE_STATEMENT)
        self.assertIn("[unsafe_statement]", update["error"])
        self.assertIsNotNone(check_sql("DELETE FROM fact_order"))

    async def test_sql_guard_allows_select(self):
        writer = _Writer()
        state = DataAgentState(
            query="q",
            original_query="q",
            sql="SELECT COUNT(*) FROM fact_order",
            sql_correction_count=0,
        )
        update = await sql_guard(state, _Runtime(writer))  # type: ignore[arg-type]
        self.assertIsNone(update["error"])
        self.assertIsNone(update["error_code"])

    async def test_reject_sql_emits_user_error(self):
        writer = _Writer()
        state = DataAgentState(
            query="q",
            original_query="q",
            error="still bad",
            sql_correction_count=2,
        )
        await reject_sql(state, _Runtime(writer))  # type: ignore[arg-type]
        errors = [c for c in writer.chunks if c.get("type") == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["code"], CORRECTION_EXHAUSTED)
        self.assertIn("改写", errors[0]["message"])


if __name__ == "__main__":
    unittest.main()
