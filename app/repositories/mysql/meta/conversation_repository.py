"""历史会话与消息的数据访问层。"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import ConversationMessageMySQL, ConversationMySQL


class ConversationRepository:
    """在 Meta MySQL 中持久化会话、消息和完整执行结果。

    仓储方法在完成一次业务写入后自行提交事务，使 SSE 请求中已经产生的 Turn
    即使后续 Redis 或 Agent 执行失败，也能被恢复为明确的失败记录。
    """

    def __init__(self, session: AsyncSession):
        # FastAPI 为每次 HTTP 请求注入一个 AsyncSession；Repository 不负责创建
        # 连接池，但会在完整业务写入完成后 commit，失败补写前会 rollback。
        self.session = session

    async def create(self, title: str) -> ConversationMySQL:
        """创建空会话并返回已刷新主键和时间字段的 ORM 对象。

        Args:
            title: 已通过 API Schema 规范化的会话标题。

        Returns:
            已提交到 Meta MySQL 的会话对象。
        """

        now = datetime.now()
        conversation = ConversationMySQL(
            id=str(uuid.uuid4()), title=title, created_at=now, updated_at=now
        )
        self.session.add(conversation)
        await self.session.commit()
        await self.session.refresh(conversation)
        return conversation

    async def get(self, conversation_id: str) -> ConversationMySQL | None:
        """按 ID 查询会话；不存在时返回 None 供 API 转换为 404。"""

        return await self.session.get(ConversationMySQL, conversation_id)

    async def list(self) -> list[tuple[ConversationMySQL, int]]:
        """按最近更新时间列出会话，并在同一次查询中统计消息数量。

        使用 outer join 是为了让尚未发送问题的新会话也能出现在历史列表中。

        Returns:
            `(会话对象, 消息数量)` 列表，按 `updated_at` 倒序排列。
        """

        stmt = (
            select(ConversationMySQL, func.count(ConversationMessageMySQL.id))
            .outerjoin(
                ConversationMessageMySQL,
                ConversationMessageMySQL.conversation_id == ConversationMySQL.id,
            )
            .group_by(ConversationMySQL.id)
            .order_by(ConversationMySQL.updated_at.desc())
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            (conversation, int(message_count)) for conversation, message_count in rows
        ]

    async def list_messages(
        self, conversation_id: str, limit: int | None = None
    ) -> list[ConversationMessageMySQL]:
        """读取会话消息，并始终按实际对话顺序返回。

        Args:
            conversation_id: MySQL 会话 ID，同时也是 LangGraph thread_id。
            limit: 仅恢复最近多少条消息；None 表示读取完整产品历史。

        Returns:
            按 `position` 升序排列的消息列表。
        """

        stmt = (
            select(ConversationMessageMySQL)
            .where(ConversationMessageMySQL.conversation_id == conversation_id)
            .order_by(ConversationMessageMySQL.position.asc())
        )
        if limit is not None:
            # 懒恢复需要“最近 N 条但仍按正序消费”。子查询先倒序选出最近 ID，
            # 外层再按 position 升序排列，避免模型看到时间顺序颠倒的上下文。
            recent_ids = (
                select(ConversationMessageMySQL.id)
                .where(ConversationMessageMySQL.conversation_id == conversation_id)
                .order_by(ConversationMessageMySQL.position.desc())
                .limit(limit)
                .subquery()
            )
            stmt = (
                select(ConversationMessageMySQL)
                .where(ConversationMessageMySQL.id.in_(select(recent_ids.c.id)))
                .order_by(ConversationMessageMySQL.position.asc())
            )
        return list((await self.session.scalars(stmt)).all())

    async def start_turn(
        self, conversation_id: str, query: str
    ) -> tuple[ConversationMessageMySQL, ConversationMessageMySQL]:
        """原子创建一轮相邻的用户消息和助手占位消息。

        Args:
            conversation_id: 本轮所属会话。
            query: 用户提交的原始自然语言问题。

        Returns:
            已提交的 `(user_message, assistant_message)`，其稳定 ID 会同时用于
            前端事件关联和 LangGraph Message 去重。

        Raises:
            LookupError: 会话已不存在，不能继续追加消息。
        """

        conversation = await self.get(conversation_id)
        if conversation is None:
            raise LookupError("会话不存在或已被删除。")

        # 当前部署使用同会话 asyncio.Lock，因此读最大 position 后递增是稳定的。
        # 数据库唯一约束负责兜底；多 Worker 部署前必须改为数据库级原子分配。
        max_position = await self.session.scalar(
            select(func.max(ConversationMessageMySQL.position)).where(
                ConversationMessageMySQL.conversation_id == conversation_id
            )
        )
        next_position = (max_position or 0) + 1
        now = datetime.now()
        user_message = ConversationMessageMySQL(
            id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            position=next_position,
            role="user",
            content=query,
            status="done",
            created_at=now,
        )
        # 先写助手占位消息，后续即使 Redis、LLM 或数据库查询失败，也有一条
        # 可被 fail_turn 更新的持久化记录，而不会让用户问题处于“静默丢失”状态。
        assistant_message = ConversationMessageMySQL(
            id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            position=next_position + 1,
            role="assistant",
            content="正在处理问题……",
            status="streaming",
            steps=[],
            created_at=now,
        )
        conversation.updated_at = datetime.now()
        self.session.add_all([user_message, assistant_message])
        await self.session.commit()
        return user_message, assistant_message

    async def finish_turn(
        self,
        message_id: str,
        *,
        content: str,
        resolved_query: str | None,
        sql: str | None,
        result: Any,
        steps: list[dict[str, Any]],
    ) -> None:
        """把助手占位消息更新为成功结果。

        Args:
            message_id: `start_turn` 创建的助手消息 ID。
            content: 面向历史列表的简短结果说明。
            resolved_query: 结合短期记忆补全后的独立问题。
            sql: 最终执行的 SQL。
            result: 返回给用户的完整结构化结果，仅保存在 MySQL。
            steps: 前端可恢复的 Agent 执行步骤。
        """

        message = await self.session.get(ConversationMessageMySQL, message_id)
        if message is None:
            return
        message.content = content
        message.status = "done"
        message.resolved_query = resolved_query
        message.sql = sql
        message.result = result
        message.steps = steps
        message.error = None
        conversation = await self.get(message.conversation_id)
        if conversation is not None:
            conversation.updated_at = datetime.now()
        await self.session.commit()

    async def fail_turn(
        self,
        message_id: str,
        *,
        content: str,
        error: str,
        steps: list[dict[str, Any]],
        status: str = "error",
    ) -> None:
        """把助手占位消息更新为失败或取消状态。

        该方法是流式执行的补偿入口。调用方已经开始返回 SSE 后不能再修改 HTTP
        状态码，因此必须把失败状态持久化，保证刷新页面后仍能看到真实结果。

        Args:
            message_id: 需要补偿更新的助手消息 ID。
            content: 面向用户的简短失败说明。
            error: 用于诊断的内部错误文本。
            steps: 失败前已经完成或正在执行的步骤。
            status: `error` 或 `cancelled`。
        """

        # 图节点可能在共享的 Meta Session 上触发数据库异常，先恢复事务状态，
        # 再写入可持久化的失败消息。
        await self.session.rollback()
        message = await self.session.get(ConversationMessageMySQL, message_id)
        if message is None:
            return
        message.content = content
        message.status = status
        message.error = error
        message.steps = steps
        conversation = await self.get(message.conversation_id)
        if conversation is not None:
            conversation.updated_at = datetime.now()
        await self.session.commit()

    async def rename(
        self, conversation_id: str, title: str
    ) -> ConversationMySQL | None:
        """更新会话标题和活跃时间；会话不存在时返回 None。"""

        conversation = await self.get(conversation_id)
        if conversation is None:
            return None
        conversation.title = title
        conversation.updated_at = datetime.now()
        await self.session.commit()
        await self.session.refresh(conversation)
        return conversation

    async def delete(self, conversation_id: str) -> bool:
        """删除 MySQL 中的会话及完整消息历史。

        Redis Thread 由 API 层在 MySQL 删除成功后单独清理。两种存储不使用分布式
        事务，因为 Redis State 可重建，而 MySQL 才是用户删除语义的事实来源。

        Returns:
            True 表示会话存在且已删除，False 表示目标不存在。
        """

        conversation = await self.get(conversation_id)
        if conversation is None:
            return False
        await self.session.execute(
            delete(ConversationMessageMySQL).where(
                ConversationMessageMySQL.conversation_id == conversation_id
            )
        )
        await self.session.delete(conversation)
        await self.session.commit()
        return True
