"""修正轮次耗尽或超范围拒答的明确终止节点。

两条进入路径：安全/EXPLAIN 连续未通过且校验达 3 轮上限；或 sql_guard 判定
模型输出的是自然语言拒答（out_of_scope）直接分流。两条都只产出可诊断的
error 事件，绝不进入 run_sql，避免带病执行。
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.sql_errors import CORRECTION_EXHAUSTED, OUT_OF_SCOPE, USER_MESSAGES
from app.agent.state import DataAgentState
from app.core.log import logger


async def reject_sql(
    state: DataAgentState, runtime: Runtime[DataAgentContext]
) -> dict[str, Any]:
    """终止本轮 SQL 闭环并向前端发送明确失败/拒答事件。

    Args:
        state: 读取 error_code 决定文案；out_of_scope 时读取 state.sql
            （此处存的是模型自己的拒答解释文本）作为用户消息主体。
        runtime: stream_writer，写入 type=error 事件。

    Returns:
        error/error_code 写回 State；流程进入 END。

    流程作用:
        位于 sql_guard/validate_sql 失败分支之后，是图内唯一的非执行终点；
        与 run_sql 互斥，保证被拒 SQL 永不落执行。
    """

    writer = runtime.stream_writer
    code = state.get("error_code") or CORRECTION_EXHAUSTED
    previous = state.get("error") or ""
    if code == OUT_OF_SCOPE:
        # 拒答解释本来就是模型说给人看的话，截断后直接透出让用户看懂原因；
        # 为空或异常时回退到 USER_MESSAGES 固定短句。
        step = "超出范围拒答"
        reason = (state.get("sql") or "").strip()[:200]
        message = reason or USER_MESSAGES[code]
    else:
        step = "放弃修正SQL"
        code = CORRECTION_EXHAUSTED
        message = USER_MESSAGES[code]
    logger.info(f"sql_rejected code={code} previous_error={previous[:200]}")
    writer({"type": "progress", "step": step, "status": "error"})
    # P5.4：SSE 只给用户短句 + code；previous 明细仅进上方日志，避免泄露驱动报错
    writer({"type": "error", "code": code, "message": message})
    return {"error": message, "error_code": code}
