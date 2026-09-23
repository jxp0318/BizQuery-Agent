"""历史会话 API 的请求与响应结构。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel


class APIModel(BaseModel):
    """会话 API 的公共模型配置。

    ORM 字段使用 snake_case，前端接口使用 camelCase；在模型层统一转换可以避免
    每个路由手工改名，并允许直接从 SQLAlchemy 对象构建响应。
    """

    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        alias_generator=to_camel,
    )


class ConversationCreateSchema(APIModel):
    """创建会话请求；标题会先规范空白，避免历史列表出现视觉重复项。"""

    # 新会话在左侧历史列表中显示的名称；未指定时使用产品默认标题。
    title: str = Field(default="新会话", min_length=1, max_length=120)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        """合并连续空白并拒绝仅包含空白字符的标题。"""

        title = " ".join(value.split()).strip()
        if not title:
            raise ValueError("会话标题不能为空。")
        return title


class ConversationUpdateSchema(APIModel):
    """重命名会话请求，复用创建时的标题规则以保持数据一致。"""

    # 用户修改后的会话标题；长度约束与创建接口保持一致。
    title: str = Field(min_length=1, max_length=120)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        return ConversationCreateSchema.normalize_title(value)


class ConversationSummarySchema(APIModel):
    """左侧历史列表需要的轻量会话摘要，不加载完整消息。"""

    # 会话 ID；前端继续提问时会随请求传回，并在后端映射为 thread_id。
    id: str
    # 左侧历史列表展示的会话名称。
    title: str
    # 会话最初创建时间，使用 ISO 时间字符串返回给前端。
    created_at: datetime
    # 最近一次消息或会话信息发生变化的时间，用于历史列表倒序排列。
    updated_at: datetime
    # 会话包含的 user 与 assistant 消息总数，只用于列表概览。
    message_count: int = 0


class ConversationMessageSchema(APIModel):
    """恢复历史会话时返回的一条完整消息及其执行证据。"""

    # MySQL 消息主键，也是前端合并流式事件和 LangGraph 消息去重的稳定标识。
    id: str
    # 所属会话 ID，前端可用它校验消息是否属于当前打开的会话。
    conversation_id: str
    # 消息发送方，目前为 user 或 assistant。
    role: str
    # 页面气泡直接展示的用户问题或 Agent 回复正文。
    content: str
    # assistant 执行状态；用于恢复 streaming、done、error 或 cancelled 页面状态。
    status: str
    # 结合短期记忆补全后的独立问题，帮助用户理解本轮真正执行的查询语义。
    resolved_query: str | None = None
    # 本轮最终执行的 SQL；用户消息没有该字段。
    sql: str | None = None
    # 完整查询结果，来源于 MySQL 历史而不是 Redis Checkpoint。
    result: Any = None
    # Agent 执行步骤，供页面恢复进度轨迹。
    steps: list[dict[str, Any]] = Field(default_factory=list)
    # 失败或取消时的诊断信息；成功消息通常为 None。
    error: str | None = None
    # 消息持久化时间；严格的会话顺序由数据库 position 保证。
    created_at: datetime

    @field_validator("steps", mode="before")
    @classmethod
    def default_steps(cls, value):
        """兼容早期记录中的 NULL，使前端始终可以按数组渲染步骤。"""

        return value or []


class ConversationDetailSchema(ConversationSummarySchema):
    """历史会话详情，包含按 position 排序的完整消息列表。"""

    # 用户打开历史会话时一次性恢复的完整消息序列。
    messages: list[ConversationMessageSchema] = Field(default_factory=list)
