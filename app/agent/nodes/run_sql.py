"""SQL 执行节点（P3：并发、超时、行数截断、取消 KILL）。

负责执行已通过 sql_guard 与 EXPLAIN 的 SQL，并把完整结果经 custom stream
送回 QueryService。执行层资源失败（超时/并发）不进入修正循环。
"""

from __future__ import annotations

import asyncio
from typing import Any

from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.sql_errors import (
    CANCELLED,
    QUERY_TIMEOUT,
    USER_MESSAGES,
)
from app.agent.state import DataAgentState
from app.conf.app_config import app_config
from app.core.log import logger

# 全局查询信号量：单 Worker 下保护数仓连接与 CPU（P3）。
_QUERY_SEMAPHORE: asyncio.Semaphore | None = None


def get_query_semaphore() -> asyncio.Semaphore:
    """按配置创建/复用全局查询信号量。"""

    global _QUERY_SEMAPHORE
    if _QUERY_SEMAPHORE is None:
        limit = max(1, int(app_config.sql_exec.max_concurrent_queries))
        _QUERY_SEMAPHORE = asyncio.Semaphore(limit)
    return _QUERY_SEMAPHORE


async def run_sql(state: DataAgentState, runtime: Runtime[DataAgentContext]) -> dict[str, Any]:
    """执行最终 SQL 并产出结果事件；记录 connection_id 以便取消时 KILL。

    Args:
        state: 读取已通过校验的 `sql`。
        runtime: stream_writer 与数仓仓储。

    Returns:
        空更新；结果通过 writer(type=result) 暴露，避免大结果进入 Checkpoint。
    """

    writer = runtime.stream_writer
    step = "执行SQL"
    writer({"type": "progress", "step": step, "status": "running"})
    sql = state["sql"]
    dw = runtime.context["dw_mysql_repository"]
    exec_cfg = app_config.sql_exec
    semaphore = get_query_semaphore()

    connection_id: int | None = None
    try:
        connection_id = await dw.connection_id()
    except Exception as error:  # noqa: BLE001
        logger.warning(f"connection_id_unavailable: {error}")

    try:
        # 全局并发：已满时等待，不在此处拒绝，避免体验过差；观测用 lock 指标另记。
        await semaphore.acquire()
        try:
            payload = await asyncio.wait_for(
                dw.run(
                    sql,
                    max_rows=exec_cfg.max_rows,
                    timeout_seconds=exec_cfg.query_timeout_seconds,
                ),
                timeout=exec_cfg.query_timeout_seconds + 5,
            )
        finally:
            semaphore.release()

        rows = payload["rows"]
        truncated = payload["truncated"]
        logger.info(
            f"sql_exec_ok rows={payload['row_count']} truncated={truncated} "
            f"connection_id={connection_id}"
        )
        writer({"type": "progress", "step": step, "status": "success"})
        if truncated:
            writer(
                {
                    "type": "truncated",
                    "message": USER_MESSAGES["result_truncated"],
                    "max_rows": exec_cfg.max_rows,
                }
            )
        writer({"type": "result", "data": rows})
    except asyncio.TimeoutError:
        logger.error(f"sql_timeout connection_id={connection_id}")
        writer({"type": "progress", "step": step, "status": "error"})
        writer(
            {
                "type": "error",
                "code": QUERY_TIMEOUT,
                "message": USER_MESSAGES[QUERY_TIMEOUT],
            }
        )
        await _kill_quietly(dw, connection_id)
        return {"error": USER_MESSAGES[QUERY_TIMEOUT], "error_code": QUERY_TIMEOUT}
    except asyncio.CancelledError:
        logger.info(f"sql_cancelled connection_id={connection_id}")
        await _kill_quietly(dw, connection_id)
        writer({"type": "progress", "step": step, "status": "error"})
        raise
    except Exception as error:  # noqa: BLE001 - DB 错误统一失败，避免堆栈外泄
        logger.error(f"{step} failed: {error}")
        writer({"type": "progress", "step": step, "status": "error"})
        raise


async def _kill_quietly(dw, connection_id: int | None) -> None:
    """取消/超时时尽力 KILL 连接；失败只记日志，不向用户二次报错。"""

    if connection_id is None:
        return
    try:
        ok = await dw.kill_connection(connection_id)
        logger.info(f"kill_connection id={connection_id} ok={ok} code={CANCELLED}")
    except Exception as error:  # noqa: BLE001
        logger.warning(f"kill_connection_failed id={connection_id}: {error}")
