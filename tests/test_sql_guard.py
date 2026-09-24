"""P3 SQL 安全闸与修正轮次单元测试。"""

from __future__ import annotations

import unittest

from app.agent.sql_errors import (
    CORRECTION_EXHAUSTED,
    FORBIDDEN_FUNCTION,
    PARSE_FAILED,
    TABLE_NOT_ALLOWED,
    UNSAFE_STATEMENT,
)
from app.agent.sql_guard import check_sql, load_allowed_table_names


class SqlGuardStatementTests(unittest.TestCase):
    def test_plain_select_passes(self):
        self.assertIsNone(
            check_sql("SELECT region, SUM(gmv) FROM fact_order GROUP BY region")
        )

    def test_with_select_passes(self):
        self.assertIsNone(
            check_sql(
                "WITH t AS (SELECT region, gmv FROM fact_order) "
                "SELECT region, SUM(gmv) FROM t GROUP BY region"
            )
        )

    def test_union_passes(self):
        self.assertIsNone(
            check_sql(
                "SELECT region FROM fact_order UNION SELECT region FROM fact_order"
            )
        )

    def test_trailing_semicolon_passes(self):
        self.assertIsNone(check_sql("SELECT 1 FROM fact_order;"))

    def test_multi_statement_rejected(self):
        err = check_sql("SELECT 1 FROM fact_order; DROP TABLE fact_order")
        self.assertIsNotNone(err)
        self.assertEqual(err.code, UNSAFE_STATEMENT)

    def test_delete_rejected(self):
        err = check_sql("DELETE FROM fact_order")
        self.assertEqual(err.code, UNSAFE_STATEMENT)

    def test_update_rejected(self):
        err = check_sql("UPDATE fact_order SET gmv = 1")
        self.assertEqual(err.code, UNSAFE_STATEMENT)

    def test_create_rejected(self):
        err = check_sql("CREATE TABLE t (id INT)")
        self.assertEqual(err.code, UNSAFE_STATEMENT)

    def test_for_update_rejected(self):
        err = check_sql("SELECT * FROM fact_order FOR UPDATE")
        self.assertEqual(err.code, UNSAFE_STATEMENT)

    def test_into_outfile_rejected(self):
        err = check_sql("SELECT gmv FROM fact_order INTO OUTFILE '/tmp/x'")
        self.assertEqual(err.code, UNSAFE_STATEMENT)

    def test_parse_failed_fail_closed(self):
        err = check_sql("SELECT FROM WHERE")
        self.assertEqual(err.code, PARSE_FAILED)

    def test_empty_sql(self):
        self.assertEqual(check_sql("").code, PARSE_FAILED)


class SqlGuardFunctionTests(unittest.TestCase):
    def test_sleep_rejected(self):
        err = check_sql("SELECT SLEEP(10) FROM fact_order")
        self.assertEqual(err.code, FORBIDDEN_FUNCTION)

    def test_load_file_rejected(self):
        err = check_sql("SELECT LOAD_FILE('/etc/passwd') FROM fact_order")
        self.assertEqual(err.code, FORBIDDEN_FUNCTION)

    def test_benchmark_rejected(self):
        err = check_sql("SELECT BENCHMARK(1000000, 1) FROM fact_order")
        self.assertEqual(err.code, FORBIDDEN_FUNCTION)

    def test_aggregate_allowed(self):
        self.assertIsNone(check_sql("SELECT COUNT(*), AVG(gmv) FROM fact_order"))

    def test_window_allowed(self):
        self.assertIsNone(
            check_sql(
                "SELECT region, ROW_NUMBER() OVER (ORDER BY SUM(gmv) DESC) AS rk "
                "FROM fact_order GROUP BY region"
            )
        )


class SqlGuardTableTests(unittest.TestCase):
    def test_allowed_tables_loaded_from_meta(self):
        names = load_allowed_table_names()
        self.assertIn("fact_order", names)
        self.assertIn("dim_region", names)

    def test_unknown_table_rejected(self):
        err = check_sql("SELECT * FROM secret_table")
        self.assertEqual(err.code, TABLE_NOT_ALLOWED)

    def test_system_schema_rejected(self):
        err = check_sql("SELECT * FROM information_schema.tables")
        self.assertEqual(err.code, TABLE_NOT_ALLOWED)

    def test_cte_name_not_treated_as_physical(self):
        self.assertIsNone(
            check_sql(
                "WITH t AS (SELECT gmv FROM fact_order) SELECT SUM(gmv) FROM t"
            )
        )

    def test_cross_db_rejected(self):
        err = check_sql("SELECT * FROM mysql.user")
        self.assertEqual(err.code, TABLE_NOT_ALLOWED)


class CorrectionRoundTests(unittest.TestCase):
    def test_route_exhausts_after_two_corrections(self):
        from app.agent.graph import _can_correct

        self.assertEqual(_can_correct({"sql_correction_count": 0}), "correct_sql")
        self.assertEqual(_can_correct({"sql_correction_count": 1}), "correct_sql")
        self.assertEqual(_can_correct({"sql_correction_count": 2}), "reject_sql")

    def test_correction_exhausted_user_message(self):
        from app.agent.sql_errors import USER_MESSAGES

        self.assertIn("改写", USER_MESSAGES[CORRECTION_EXHAUSTED])


if __name__ == "__main__":
    unittest.main()
