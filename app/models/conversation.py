"""历史会话与消息 ORM 模型。

Meta MySQL 是用户可见历史和审计信息的事实来源：完整消息、SQL、查询结果、
执行步骤和错误都保存在这里。Redis Checkpoint 只保存可由这些记录重建的精简
运行状态，因此不能用 Redis 数据替代本模块中的业务持久化模型。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from sqlalchemy.types import JSON

from app.models.base import Base


class ConversationMySQL(Base):
    """一个可以被重新打开并继续追问的产品会话。

    `updated_at` 同时承担历史列表排序依据：任意新消息或状态变化都应更新它，
    让最近使用的会话优先显示在左侧栏。

    字段声明中的 ``Mapped[T]`` 表示 Python 代码读取该属性时得到的类型，
    ``mapped_column(...)`` 则声明它在 MySQL 中对应列的长度、约束和默认值。
    """

    # SQLAlchemy 约定的类属性：指定该 ORM 类映射到 MySQL 的 conversation 表。
    __tablename__ = "conversation"

    # 会话的全局唯一标识。P2 直接复用该值作为 LangGraph thread_id，
    # 因而 MySQL 历史与 Redis Checkpoint 可以定位到同一段对话。
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # 展示在左侧历史列表中的会话名称；新会话默认根据首个问题生成标题。
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    # 会话首次创建时间，用于审计和空会话的稳定排序，不随后续提问改变。
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, server_default=func.now()
    )
    # 会话最近一次发生变化的时间，是左侧历史列表“最近使用优先”的排序依据。
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.now,
        server_default=func.now(),
        onupdate=datetime.now,
        index=True,
    )


class ConversationMessageMySQL(Base):
    """会话中的用户消息或 Agent 消息及其完整执行证据。

    一轮查询固定写入相邻的 user/assistant 两条消息。assistant 消息先以
    `streaming` 占位，完成或失败后原位更新，从而使浏览器刷新时也能恢复
    正在执行、成功、失败或取消等状态。
    """

    # SQLAlchemy 约定的类属性：指定该 ORM 类映射到 conversation_message 表。
    __tablename__ = "conversation_message"
    # 表级约束与索引不能附着在某一个字段上，因此集中写在 __table_args__：
    # 唯一约束防止同一会话出现重复 position，联合索引加速按会话读取历史消息。
    __table_args__ = (
        UniqueConstraint("conversation_id", "position", name="uq_message_position"),
        Index("ix_message_conversation_created", "conversation_id", "created_at"),
    )

    # 单条消息的全局唯一标识。开始一轮查询时会同时生成 user 和 assistant 消息 ID，
    # 后续流式完成或失败都更新同一条 assistant 记录，而不是再插入一条结果消息。
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # 消息所属的会话 ID；删除会话时由数据库级联删除它的全部消息。
    conversation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("conversation.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # position 是会话内稳定顺序，唯一约束同时充当数据库层的最终防重边界。
    # 当前单 Worker 由进程内会话锁保证分配顺序，多 Worker 前需要升级原子序号方案。
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    # 消息发送方，目前保存 user 或 assistant；恢复页面和构造短期记忆时据此还原消息类型。
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    # 用户原始问题或 Agent 最终回复正文，是历史页面直接展示的主要内容。
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # assistant 消息的执行状态（streaming/done/error 等），用于刷新后恢复执行结果或失败提示。
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="done")
    # 保存结合短期记忆补全后的独立问题，既便于审计，也能用于 Redis miss 后恢复。
    resolved_query: Mapped[str | None] = mapped_column(Text)
    # 本轮最终通过校验并执行的 SQL；仅 assistant 消息会写入，便于历史展示和问题定位。
    sql: Mapped[str | None] = mapped_column(Text)
    # 完整查询结果只进入 MySQL；Redis 仅保存行数等摘要，避免 Checkpoint 膨胀。
    result: Mapped[Any | None] = mapped_column(JSON)
    # Agent 各阶段的可视化执行记录，例如问题改写、召回、SQL 生成和执行状态。
    steps: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    # 本轮失败时保存可展示、可诊断的错误信息；成功消息保持为 NULL。
    error: Mapped[str | None] = mapped_column(Text)
    # P5.1：分节点耗时与 token 用量，由 QueryService 在结束时写入；与 steps 分离，
    # 避免前端执行轨迹列表被指标结构污染。仅审计/评测消费，不进 Redis。
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # 消息写入时间，用于审计；会话内的严格显示顺序仍以 position 为准。
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, server_default=func.now()
    )
