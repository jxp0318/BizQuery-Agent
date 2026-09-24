"""SQL 安全原因码与三份文案映射。

同一失败会拆给三类消费者：用户看可操作短句，correct_sql 看可改写的结构原因，
日志保留 code 与脱敏详情。禁止把堆栈或原始 DB 长报错直接甩给前端。
"""

from __future__ import annotations

from dataclasses import dataclass

# ---- 原因码（P3 最小集）----
UNSAFE_STATEMENT = "unsafe_statement"
FORBIDDEN_FUNCTION = "forbidden_function"
TABLE_NOT_ALLOWED = "table_not_allowed"
PARSE_FAILED = "parse_failed"
INVALID_SQL = "invalid_sql"
CORRECTION_EXHAUSTED = "correction_exhausted"
QUERY_TIMEOUT = "query_timeout"
RESULT_TRUNCATED = "result_truncated"
CONCURRENT_LIMIT = "concurrent_limit"
CANCELLED = "cancelled"

# 用户可见短句：只描述「怎么办」，不含内部细节。
USER_MESSAGES: dict[str, str] = {
    UNSAFE_STATEMENT: "当前问题无法用只读查询完成，请改写问题后重试。",
    FORBIDDEN_FUNCTION: "查询中包含不允许的函数，请改写问题后重试。",
    TABLE_NOT_ALLOWED: "涉及未授权的数据表，不在可分析范围内，请改写问题。",
    PARSE_FAILED: "生成的 SQL 不符合规范，请改写问题后重试。",
    INVALID_SQL: "生成的 SQL 无法在数据库执行，已尝试自动修正。",
    CORRECTION_EXHAUSTED: "连续修正仍未通过校验，请改写问题后重试。",
    QUERY_TIMEOUT: "查询超时，请缩小时间范围或减少分组维度。",
    RESULT_TRUNCATED: "结果较多，仅展示前 1000 行。",
    CONCURRENT_LIMIT: "系统繁忙，请稍后再试。",
    CANCELLED: "已停止本次查询。",
}


@dataclass(frozen=True)
class SqlCheckError:
    """一次安全/校验失败的三份语义载荷。

    Attributes:
        code: 稳定原因码，供日志检索与前端映射。
        detail: 给 correct_sql 的结构化说明，足以改写但不含连接密钥等敏感信息。
        user_message: 给页面的短句；默认由 code 映射，可覆盖。
    """

    code: str
    detail: str
    user_message: str | None = None

    def for_model(self) -> str:
        """返回写入 state.error、供修正节点消费的文本。"""

        return f"[{self.code}] {self.detail}"

    def for_user(self) -> str:
        """返回面向终端用户的短句。"""

        return self.user_message or USER_MESSAGES.get(
            self.code, "查询未成功，请改写问题后重试。"
        )
