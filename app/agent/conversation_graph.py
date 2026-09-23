"""带 Redis Checkpointer 的会话主图。

外层图持久化精简短期记忆，内层 data_graph 完成一次 NL2SQL 查询。这样既能使用
LangGraph 原生线程恢复能力，也不会把大体量召回结果和完整 SQL 结果写入 Redis。
"""

from collections.abc import Iterable
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.conversation_state import ConversationAgentState
from app.agent.graph import data_graph
from app.agent.state import DataAgentState


def summarize_result(result: Any) -> tuple[str, dict[str, Any]]:
    """把完整 SQL 结果转换为用户提示和可持久化摘要。

    Args:
        result: NL2SQL 子图通过 SSE 产生的完整查询结果。

    Returns:
        `(助手提示文本, Redis 结果摘要)`；摘要只包含行数，不包含业务数据行。

    该边界保证完整结果只进入 MySQL，避免 Redis Checkpoint 因大结果集膨胀，
    同时减少短期记忆中重复保存敏感数据的风险。
    """

    if isinstance(result, list):
        row_count = len(result)
        return f"查询完成，共返回 {row_count} 行结果。", {"row_count": row_count}
    return "查询完成。", {"row_count": None}


def _message_line(message: BaseMessage) -> str:
    """把 LangChain Message 转为问题改写节点能够消费的中文上下文行。"""

    content = str(message.content).strip()
    if isinstance(message, HumanMessage):
        return f"用户：{content}"
    if isinstance(message, AIMessage):
        line = f"助手：{content}"
        resolved_query = message.additional_kwargs.get("resolved_query")
        sql = message.additional_kwargs.get("sql")
        if resolved_query:
            line += f"\n独立问题：{resolved_query}"
        if sql:
            line += f"\n上一轮 SQL：{sql}"
        return line
    return content


def extend_summary(
    existing_summary: str,
    messages: Iterable[BaseMessage],
    max_chars: int,
) -> str:
    """把移出窗口的消息合并进确定性滚动摘要。

    Args:
        existing_summary: 上个 Checkpoint 已保存的较早对话摘要。
        messages: 本轮需要移出近期窗口的消息。
        max_chars: Redis 中允许保留的摘要最大字符数。

    Returns:
        合并并裁剪后的摘要文本。

    当前使用确定性文本压缩而不是额外调用 LLM，目的是避免每次窗口滚动都增加
    模型延迟和费用。超过上限时保留靠后的内容，因为它通常更接近当前问题。
    """

    additions = "\n\n".join(
        line for message in messages if (line := _message_line(message))
    )
    combined = "\n\n".join(
        part for part in (existing_summary.strip(), additions) if part
    )
    if len(combined) <= max_chars:
        return combined
    return f"……（更早内容已压缩）\n{combined[-max_chars:]}"


def compact_messages(
    messages: list[BaseMessage],
    max_messages: int,
    summary: str = "",
    summary_max_chars: int = 2400,
) -> tuple[list[BaseMessage], str]:
    """把 MySQL 历史压缩为 Redis 初始消息窗口和滚动摘要。

    Args:
        messages: 按真实对话顺序排列的有效历史消息。
        max_messages: Checkpoint 中最多保留的原始消息数。
        summary: 已有摘要；首次从 MySQL 水合时通常为空。
        summary_max_chars: 摘要字符上限。

    Returns:
        `(最近消息窗口, 更新后的较早历史摘要)`。
    """

    if len(messages) <= max_messages:
        return messages, summary
    removed = messages[:-max_messages]
    return messages[-max_messages:], extend_summary(
        summary, removed, summary_max_chars
    )


def format_conversation_context(state: ConversationAgentState) -> str:
    """把 Checkpoint State 转为 `resolve_query` 使用的精简上下文。

    Args:
        state: 已由 LangGraph 按 thread_id 恢复并合并本轮输入的会话 State。

    Returns:
        包含较早摘要、近期消息、上一轮独立问题和 SQL 的文本上下文。

    当前用户消息已经通过 `original_query` 单独传给改写节点，因此这里主动排除它，
    避免 Prompt 同时把同一句话当作“历史”和“当前输入”。
    """

    current_message_id = state.get("current_user_message_id")
    lines: list[str] = []
    summary = state.get("conversation_summary", "").strip()
    if summary:
        lines.append(f"较早对话摘要：\n{summary}")
    for message in state.get("messages", []):
        if message.id == current_message_id:
            continue
        line = _message_line(message)
        if line:
            lines.append(line)
    return "\n\n".join(lines)


def build_conversation_graph(
    checkpointer: BaseCheckpointSaver,
    *,
    recent_message_limit: int,
    summary_max_chars: int,
):
    """创建绑定 Checkpointer 的精简会话主图。

    Args:
        checkpointer: 应用启动时创建的 Redis-backed LangGraph Checkpointer。
        recent_message_limit: Checkpoint 中保留的近期原始消息数量。
        summary_max_chars: 较早对话摘要的字符上限。

    Returns:
        已编译的会话主图。调用时必须传入 `configurable.thread_id`，LangGraph 才能
        恢复同一会话的历史 State。

    会话主图只保存短期记忆，并把原有 NL2SQL 图作为不带 Checkpointer 的子图
    调用。这样既使用 LangGraph 原生恢复语义，又不会持久化字段召回和候选表等
    单轮临时数据。
    """

    async def run_data_agent(
        state: ConversationAgentState,
        runtime: Runtime[DataAgentContext],
    ) -> dict[str, Any]:
        """执行一轮 NL2SQL 子图，并返回可写入 Checkpoint 的紧凑更新。

        读取当前问题、消息窗口和摘要；向外转发子图产生的 SSE 自定义事件；
        最终写回助手消息、独立问题、SQL 与结果行数摘要。子图异常直接向上抛出，
        由 QueryService 把 MySQL 助手占位消息更新为失败状态。
        """

        writer = runtime.stream_writer
        current_query = state["current_query"]
        data_state = DataAgentState(
            query=current_query,
            original_query=current_query,
            conversation_context=format_conversation_context(state),
        )

        resolved_query: str | None = None
        generated_sql: str | None = None
        result_data: Any = None
        # 内层 data_graph 不绑定 Checkpointer，因此三路召回和 SQL 上下文只在
        # 本轮内存中流转；外层节点仅收集下一轮真正需要的少量结果。
        async for chunk in data_graph.astream(
            input=data_state,
            context=runtime.context,
            stream_mode="custom",
        ):
            if chunk.get("type") == "resolved_query":
                resolved_query = chunk.get("query")
            elif chunk.get("type") == "sql":
                generated_sql = chunk.get("sql")
            elif chunk.get("type") == "result":
                result_data = chunk.get("data")
            # 子图自己的 stream writer 不会自动变成外层 SSE 响应，必须在这里
            # 原样转发，才能保持 P1 已有的前端事件协议不变。
            writer(chunk)

        assistant_content, result_summary = summarize_result(result_data)
        assistant_message = AIMessage(
            id=state["current_assistant_message_id"],
            content=assistant_content,
            additional_kwargs={
                "resolved_query": resolved_query,
                "sql": generated_sql,
            },
        )

        combined_messages = [*state.get("messages", []), assistant_message]
        removed_messages = combined_messages[:-recent_message_limit]
        # add_messages reducer 通过 RemoveMessage 删除窗口外消息，再追加本轮助手
        # 消息；直接返回一个全新列表会破坏 LangGraph 的消息 ID 合并语义。
        message_updates: list[BaseMessage] = [
            RemoveMessage(id=message.id)
            for message in removed_messages
            if message.id is not None
        ]
        message_updates.append(assistant_message)

        return {
            "messages": message_updates,
            "conversation_summary": extend_summary(
                state.get("conversation_summary", ""),
                removed_messages,
                summary_max_chars,
            ),
            "last_resolved_query": resolved_query,
            "last_sql": generated_sql,
            "last_result_summary": result_summary,
        }

    # Checkpointer 只绑定外层会话图；内层 NL2SQL 子图保持单轮、无持久化。
    builder = StateGraph(
        state_schema=ConversationAgentState,
        context_schema=DataAgentContext,
    )
    builder.add_node("run_data_agent", run_data_agent)
    builder.add_edge(START, "run_data_agent")
    builder.add_edge("run_data_agent", END)
    return builder.compile(checkpointer=checkpointer)
