"""会话级 LangGraph 短期记忆状态。

该 State 只保存下一轮推理真正需要的紧凑信息。字段召回、候选表、完整查询结果
等一次查询内的临时数据仍留在 DataAgentState 中，不进入 Redis Checkpoint。
"""

from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class ConversationAgentState(TypedDict, total=False):
    """按 conversation_id/thread_id 隔离并写入 Redis 的短期记忆。

    这里只声明跨节点、跨请求真正需要恢复的紧凑字段。单轮 RAG 召回结果由
    `DataAgentState` 承载并在子图结束后释放，避免每个节点都把大对象写入
    Checkpoint。
    """

    # add_messages 会按消息 ID 合并新增消息和 RemoveMessage，使同一轮重试不会
    # 简单追加重复内容，也允许会话主图在窗口溢出时删除旧消息。
    messages: Annotated[list[BaseMessage], add_messages]
    # 窗口外的较早对话压缩到摘要中，下一轮仍能继承时间、地区和指标等约束。
    conversation_summary: str
    # 用户本轮原始输入，每次请求都会覆盖；会话主图据此启动一次 NL2SQL 子图。
    current_query: str
    # 本轮 MySQL user 消息 ID，同时作为 HumanMessage ID，避免水合与当前输入重复。
    current_user_message_id: str
    # 本轮 MySQL assistant 占位消息 ID，最终也作为 AIMessage ID 写入 Checkpoint。
    current_assistant_message_id: str
    # 上一轮结合上下文补全后的独立问题，供后续追问理解和故障排查使用。
    last_resolved_query: str | None
    # 上一轮最终执行 SQL；保存的是文本证据，不包含数仓返回的完整数据集。
    last_sql: str | None
    # 只保存行数等摘要，不保存完整查询行；完整结果属于 MySQL 事实历史。
    last_result_summary: dict[str, Any] | None
