"""历史会话管理接口。

读取列表和详情只访问 MySQL，不会因为用户浏览历史而创建 Redis Checkpoint；
只有用户继续提问时，QueryService 才恢复或懒初始化短期记忆。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.api.dependencies import get_conversation_repository
from app.api.schemas.conversation_schema import (
    ConversationCreateSchema,
    ConversationDetailSchema,
    ConversationMessageSchema,
    ConversationSummarySchema,
    ConversationUpdateSchema,
)
from app.clients.redis_checkpoint_manager import redis_checkpoint_manager
from app.core.log import logger
from app.repositories.mysql.meta.conversation_repository import ConversationRepository

conversation_router = APIRouter(prefix="/api/conversations", tags=["conversations"])


def _summary(conversation, message_count: int) -> ConversationSummarySchema:
    """把 ORM 会话转换为左侧历史列表使用的轻量响应。"""

    return ConversationSummarySchema(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        message_count=message_count,
    )


@conversation_router.post(
    "", response_model=ConversationSummarySchema, status_code=status.HTTP_201_CREATED
)
async def create_conversation(
    payload: ConversationCreateSchema,
    repository: Annotated[ConversationRepository, Depends(get_conversation_repository)],
):
    """创建一个尚无消息的持久化会话。"""

    conversation = await repository.create(payload.title.strip())
    return _summary(conversation, 0)


@conversation_router.get("", response_model=list[ConversationSummarySchema])
async def list_conversations(
    repository: Annotated[ConversationRepository, Depends(get_conversation_repository)],
):
    """返回按最近活跃时间排序的历史会话及消息数量。"""

    return [
        _summary(conversation, message_count)
        for conversation, message_count in await repository.list()
    ]


@conversation_router.get("/{conversation_id}", response_model=ConversationDetailSchema)
async def get_conversation(
    conversation_id: str,
    repository: Annotated[ConversationRepository, Depends(get_conversation_repository)],
):
    """从 MySQL 恢复完整会话，用于页面刷新或历史切换。"""

    conversation = await repository.get(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在或已被删除。")
    messages = await repository.list_messages(conversation_id)
    return ConversationDetailSchema(
        **_summary(conversation, len(messages)).model_dump(),
        messages=[
            ConversationMessageSchema.model_validate(message) for message in messages
        ],
    )


@conversation_router.patch(
    "/{conversation_id}", response_model=ConversationSummarySchema
)
async def update_conversation(
    conversation_id: str,
    payload: ConversationUpdateSchema,
    repository: Annotated[ConversationRepository, Depends(get_conversation_repository)],
):
    """更新会话标题；短期记忆不依赖标题，因此无需改写 Redis State。"""

    conversation = await repository.rename(conversation_id, payload.title.strip())
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在或已被删除。")
    messages = await repository.list_messages(conversation_id)
    return _summary(conversation, len(messages))


@conversation_router.delete(
    "/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_conversation(
    conversation_id: str,
    repository: Annotated[ConversationRepository, Depends(get_conversation_repository)],
):
    """删除产品历史，并尽力清理同 ID 的 Redis Thread。

    MySQL 删除成功即代表用户删除语义已经成立；Redis 是可重建缓存，清理失败
    只记录诊断信息，不使用跨数据库事务回滚 MySQL 删除。
    """

    if not await repository.delete(conversation_id):
        raise HTTPException(status_code=404, detail="会话不存在或已被删除。")
    try:
        await redis_checkpoint_manager.delete_thread(conversation_id)
    except Exception as error:
        # MySQL 是产品历史事实来源，Redis Thread 可重建；清理失败不能撤销用户删除。
        logger.error(
            f"checkpoint_delete_failed thread_id={conversation_id}: {error}"
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
