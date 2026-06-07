"""
问题改写节点

多轮对话时，根据历史对话将当前追问改写成独立完整的问题；
每轮开头重置 pipeline 字段，避免 checkpoint 携带上轮 keywords / sql 等状态。
"""

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.llm import llm
from app.agent.state import DataAgentState
from app.core.log import logger
from app.prompt.prompt_loader import load_prompt

STEP = "问题改写"
MAX_HISTORY_ROUNDS = 5


# MemorySaver 会持久化整个 DataAgentState；同 thread_id 的下一轮请求会从 checkpoint
# 加载上轮 keywords、召回结果、sql 等字段。必须在每轮第一个节点开头清空这些 pipeline
# 字段，否则下游可能复用上轮中间状态，导致召回/SQL 结果错误。
def _pipeline_resets() -> dict:
    """返回本轮问数 pipeline 字段的空值，用于覆盖 checkpoint 中的上轮残留"""
    return {
        "keywords": [],
        "retrieved_column_infos": [],
        "retrieved_metric_infos": [],
        "retrieved_value_infos": [],
        "table_infos": [],
        "metric_infos": [],
        "sql": "",
        "error": None,
        "correction_attempts": 0,
        "correction_errors": [],
        "final_error": None,
    }


def _format_chat_history(messages: list[BaseMessage]) -> str:
    """将 messages 格式化为 Prompt 可读的历史文本，只保留最近 N 轮"""
    lines: list[str] = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            lines.append(f"用户：{msg.content}")
        elif isinstance(msg, AIMessage):
            lines.append(f"助手：{msg.content}")

    max_lines = MAX_HISTORY_ROUNDS * 2
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return "\n".join(lines) if lines else "（无历史对话）"


def _needs_rewrite(messages: list[BaseMessage]) -> bool:
    """存在助手历史回复时才需要 LLM 改写"""
    return any(isinstance(m, AIMessage) for m in messages)


async def rewrite_query(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """根据对话历史改写当前问题，并重置本轮 pipeline 字段

    本节点是 workflow 入口：同 thread_id 的追问会从 checkpoint 恢复完整状态，
    因此必须在任何业务逻辑之前先重置 keywords / 召回 / sql 等字段，仅保留 messages
    供多轮改写使用；messages 本身由 add_messages reducer 跨轮累积，不在此清空。
    """

    step = STEP
    writer = runtime.stream_writer
    writer({"type": "progress", "step": step, "status": "running"})

    # 每轮最先执行：清空上轮 pipeline 残留，再读取 query / messages 做改写
    resets = _pipeline_resets()
    query = state["query"]
    messages = state.get("messages") or []

    try:
        if not _needs_rewrite(messages):
            logger.info(f"首轮或无可参考历史，透传 query: {query}")
            writer({"type": "progress", "step": step, "status": "success"})
            return {**resets, "query": query}

        chat_history = _format_chat_history(messages)
        prompt = PromptTemplate(
            template=load_prompt("rewrite_query"),
            input_variables=["chat_history", "query"],
        )
        chain = prompt | llm | StrOutputParser()
        rewritten = await chain.ainvoke(
            {"chat_history": chat_history, "query": query}
        )
        rewritten = (rewritten or "").strip() or query
        logger.info(f"问题改写: {query!r} -> {rewritten!r}")
        writer({"type": "progress", "step": step, "status": "success"})
        return {**resets, "query": rewritten}

    except Exception as e:
        logger.error(f"{STEP} failed, fallback to original query: {e}")
        writer({"type": "progress", "step": step, "status": "success"})
        return {**resets, "query": query}
