"""sql_guard 节点：在 EXPLAIN 之前做 AST 程序化安全检查。

纯本地 sqlglot + 白名单/黑名单规则，不访问数据库、不调用大模型。
失败时写入 state.error / error_code，由图条件边决定进入修正循环或放弃。
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.sql_errors import SqlCheckError
from app.agent.sql_guard import check_sql
from app.agent.state import DataAgentState
from app.core.log import logger


async def sql_guard(
    state: DataAgentState, runtime: Runtime[DataAgentContext]
) -> dict[str, Any]:
    """检查候选 SQL 是否符合只读问数安全策略。

    Args:
        state: 读取待检 `sql`；写回 `error` 与 `error_code`。
        runtime: 提供 SSE stream_writer；本节点不访问外部服务。

    Returns:
        局部 State 更新：通过时清空 error/error_code，失败时写入原因码与模型可读说明。

    流程作用:
        位于 generate_sql/correct_sql 之后、validate_sql 之前；与 EXPLAIN 职责分离。
    """

    writer = runtime.stream_writer
    step = "安全检查SQL"
    writer({"type": "progress", "step": step, "status": "running"})

    sql = (state.get("sql") or "").strip()
    try:
        result: SqlCheckError | None = check_sql(sql)
        if result is None:
            writer({"type": "progress", "step": step, "status": "success"})
            return {"error": None, "error_code": None}

        logger.info(
            f"sql_guard_rejected code={result.code} detail={result.detail}"
        )
        writer(
            {
                "type": "progress",
                "step": step,
                "status": "success",
            }
        )
        # error 供 correct_sql 改写；error_code 供路由与用户文案映射。
        return {"error": result.for_model(), "error_code": result.code}
    except Exception as error:  # noqa: BLE001 - 规则实现缺陷不应静默放行
        logger.error(f"{step} failed: {error}")
        writer({"type": "progress", "step": step, "status": "error"})
        raise
