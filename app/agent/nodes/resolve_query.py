"""把依赖历史上下文的追问改写为独立问题。"""

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.llm import llm
from app.agent.state import DataAgentState
from app.core.log import logger
from app.prompt.prompt_loader import load_prompt


async def resolve_query(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """把依赖历史的追问改写为可独立执行的问数问题。

    Args:
        state: 读取 `original_query`、`query` 和 `conversation_context`；只写回
            补全后的 `query`，不会修改用于界面展示的原始问题。
        runtime: 提供 SSE stream writer；本节点不直接访问数据库或检索后端。

    Returns:
        包含独立问题的局部 State 更新 `{"query": resolved_query}`。

    流程作用:
        该节点位于召回之前，确保“只看二月份”等追问先补齐指标、时间和地区，
        再进入关键词抽取与 RAG，避免后续检索建立在不完整语义上。
    """

    writer = runtime.stream_writer
    step = "理解会话上下文"
    writer({"type": "progress", "step": step, "status": "running"})
    original_query = state.get("original_query", state["query"])
    conversation_context = state.get("conversation_context", "").strip()

    if not conversation_context:
        # 新会话没有可继承条件，跳过一次不必要的 LLM 调用，也避免模型凭空补全。
        writer({"type": "resolved_query", "query": original_query})
        writer({"type": "progress", "step": step, "status": "success"})
        return {"query": original_query}

    try:
        # Prompt 只接收精简摘要、近期消息和必要 SQL，不包含完整查询结果，
        # 既控制上下文体积，也降低把敏感业务数据重复发送给模型的风险。
        prompt = PromptTemplate(
            template=load_prompt("resolve_query"),
            input_variables=["conversation_context", "query"],
        )
        result = await (prompt | llm | StrOutputParser()).ainvoke(
            {"conversation_context": conversation_context, "query": original_query}
        )
        resolved_query = result.strip() or original_query
        writer({"type": "resolved_query", "query": resolved_query})
        writer({"type": "progress", "step": step, "status": "success"})
        logger.info(f"上下文问题改写：{original_query} -> {resolved_query}")
        return {"query": resolved_query}
    except Exception as error:
        # 历史改写属于增强能力；失败时降级为本轮原问题，不阻断基础问数。
        logger.warning(f"上下文问题改写失败，使用原问题继续执行：{error}")
        writer({"type": "resolved_query", "query": original_query})
        writer({"type": "progress", "step": step, "status": "success"})
        return {"query": original_query}
