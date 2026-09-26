"""
问数查询接口路由

负责定义前端访问的 `/api/query` 接口，把 HTTP 请求交给 QueryService，
并把问数智能体执行过程以 SSE 形式持续返回给客户端。
路由层只处理请求体、依赖声明和响应类型，不直接创建 Repository 或执行图节点。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from starlette.responses import StreamingResponse

from app.api.dependencies import get_query_service
from app.api.schemas.query_schema import QuerySchema
from app.core.context import request_id_ctx_var
from app.services.query_service import QueryService


def _sse_response(generator) -> StreamingResponse:
    """SSE 响应统一带上 X-Request-Id，便于与日志、消息 metrics 对齐。"""

    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Request-Id": str(request_id_ctx_var.get()),
        },
    )

# 当前模块只维护查询相关接口，避免后续所有 API 都挤在 main.py 中
query_router = APIRouter()


@query_router.post("/api/query")
async def query_handler(
    query: QuerySchema,
    query_service: Annotated[QueryService, Depends(get_query_service)],
):
    """兼容旧单轮接口：自动创建会话后执行可持久化查询。

    返回 StreamingResponse 后，节点异常只能通过 SSE `error` 事件表达，不能再
    修改已经发送的 HTTP 状态码；持久化失败记录由 QueryService 负责。
    """

    conversation_id = await query_service.create_conversation(query.query)

    return _sse_response(query_service.query(conversation_id, query.query))


@query_router.post("/api/conversations/{conversation_id}/query")
async def conversation_query_handler(
    conversation_id: str,
    query: QuerySchema,
    query_service: Annotated[QueryService, Depends(get_query_service)],
):
    """在指定历史会话中执行一次可续聊的流式查询。

    Args:
        conversation_id: MySQL 会话 ID，同时作为 LangGraph thread_id。
        query: 已由 Pydantic 清理空白的自然语言问题。
        query_service: FastAPI 依赖系统组装的请求级业务服务。

    Returns:
        持续发送 Agent 自定义事件的 SSE StreamingResponse。
    """

    # 在创建 StreamingResponse 前完成 404 校验，此时仍能返回标准 HTTP 错误；
    # 一旦流开始，后续异常只能编码为 SSE 事件。
    if not await query_service.conversation_exists(conversation_id):
        raise HTTPException(status_code=404, detail="会话不存在或已被删除。")

    return _sse_response(query_service.query(conversation_id, query.query))
