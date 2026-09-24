"""修正轮次耗尽后的明确失败节点。

安全/EXPLAIN 连续未通过且校验已达 3 轮上限时进入这里：只产出可诊断的 error 事件，
绝不进入 run_sql，避免带病执行。
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.sql_errors import CORRECTION_EXHAUSTED, USER_MESSAGES
from app.agent.state import DataAgentState
from app.core.log import logger


async def reject_sql(
    state: DataAgentState, runtime: Runtime[DataAgentContext]
) -> dict[str, Any]:
    """终止本轮 SQL 闭环并向前端发送明确失败事件。

    Args:
        state: 读取已有 error/error_code 供日志关联。
        runtime: stream_writer，写入 type=error 事件。

    Returns:
        空更新；流程进入 END。
    """

    writer = runtime.stream_writer
    step = "放弃修正SQL"
    code = CORRECTION_EXHAUSTED
    previous = state.get("error") or ""
    logger.info(
        f"sql_correction_exhausted code={code} previous_error={previous[:200]}"
    )
    writer({"type": "progress", "step": step, "status": "error"})
    writer(
        {
            "type": "error",
            "code": code,
            "message": USER_MESSAGES[code],
            "detail": previous[:500] if previous else "校验未通过",
        }
    )
    return {"error": USER_MESSAGES[code], "error_code": code}
