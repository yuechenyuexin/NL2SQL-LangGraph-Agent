"""
SQL 执行节点

负责执行最终 SQL，并记录查询结果。
它是当前 SQL 闭环的结束节点，执行完成后流程进入 END。
"""

import json

from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.state import DataAgentState
from app.core.log import logger

SQL_PREVIEW_MAX_LEN = 200
RESULT_PREVIEW_MAX_LEN = 500
RESULT_PREVIEW_ROWS = 3


def _build_ai_summary(query: str, sql: str, result: list[dict]) -> str:
    """构造写入 messages 的助手摘要，供下轮 rewrite_query 参考"""

    row_count = len(result)
    if row_count == 0:
        preview = "返回 0 行"
    else:
        preview = json.dumps(result[:RESULT_PREVIEW_ROWS], ensure_ascii=False, default=str)
        if len(preview) > RESULT_PREVIEW_MAX_LEN:
            preview = preview[:RESULT_PREVIEW_MAX_LEN] + "…"

    sql_preview = sql if len(sql) <= SQL_PREVIEW_MAX_LEN else sql[:SQL_PREVIEW_MAX_LEN] + "…"

    summary = (
        f"【问数结果】\n"
        f"问题：{query}\n"
        f"SQL：{sql_preview}\n"
        f"返回行数：{row_count}\n"
        f"结果摘要：{preview}"
    )
    if len(summary) > RESULT_PREVIEW_MAX_LEN + 200:
        summary = summary[: RESULT_PREVIEW_MAX_LEN + 200] + "…"
    return summary


async def run_sql(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """执行 SQL 并产出最终问数结果"""

    writer = runtime.stream_writer
    step = "执行SQL"
    writer({"type": "progress", "step": step, "status": "running"})

    try:
        # 这里拿到的可能是 generate_sql 直接通过校验的 SQL，也可能是 correct_sql 覆盖后的 SQL
        sql = state["sql"]
        dw_mysql_repository = runtime.context["dw_mysql_repository"]

        # 真实数据库访问统一封装在仓储层，节点只负责从状态取 SQL 并触发执行
        result = await dw_mysql_repository.run(sql)
        logger.info(f"SQL执行结果：{result}")
        writer({"type": "progress", "step": step, "status": "success"})
        writer({"type": "result", "data": result})

        summary = _build_ai_summary(state["query"], sql, result)
        return {"messages": [AIMessage(content=summary)]}

    except Exception as e:
        logger.error(f"{step} failed: {e}")
        writer({"type": "progress", "step": step, "status": "error"})
        raise
