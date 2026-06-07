"""
SQL 修正配额耗尽失败节点

当 correction_attempts 达到 max_correction_attempts 且校验仍失败时进入。
优雅返回结构化错误信息，不抛异常中断图执行。
"""

from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.state import DataAgentState
from app.conf.app_config import app_config
from app.core.log import logger

SQL_PREVIEW_MAX_LEN = 200
STEP = "SQL校正失败"


def _build_failure_message(state: DataAgentState) -> str:
    """构造包含尝试次数、错误链与 SQL 预览的失败信息"""

    attempts = state.get("correction_attempts", 0)
    max_attempts = app_config.agent.max_correction_attempts
    last_error = state.get("error") or "未知错误"
    history = state.get("correction_errors") or []
    sql = state.get("sql") or ""
    sql_preview = (
        sql if len(sql) <= SQL_PREVIEW_MAX_LEN else sql[:SQL_PREVIEW_MAX_LEN] + "…"
    )

    lines = [
        f"SQL 校验失败：已尝试修正 {attempts}/{max_attempts} 次，仍未通过。",
        f"最后一次错误：{last_error}",
    ]
    if history:
        lines.append(f"修正过程错误记录：{' → '.join(history)}")
    if sql_preview:
        lines.append(f"最后 SQL：{sql_preview}")
    return "\n".join(lines)


async def sql_correction_failed(
    state: DataAgentState, runtime: Runtime[DataAgentContext]
):
    """配额耗尽后的终态失败处理"""

    writer = runtime.stream_writer
    message = _build_failure_message(state)
    last_error = state.get("error") or "未知错误"

    logger.error(message)
    writer({"type": "progress", "step": STEP, "status": "error"})
    writer({"type": "error", "message": message})

    summary = f"【问数失败】\n问题：{state['query']}\n{message}"
    return {
        "final_error": last_error,
        "messages": [AIMessage(content=summary)],
    }
