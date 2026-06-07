"""
LangGraph 条件边路由函数

将图编排（graph.py）与分支判断逻辑分离，便于单测与维护。
"""

from app.agent.state import DataAgentState
from app.conf.app_config import app_config

ROUTE_RUN_SQL = "run_sql"
ROUTE_CORRECT_SQL = "correct_sql"
ROUTE_SQL_CORRECTION_FAILED = "sql_correction_failed"

VALIDATE_SQL_PATH_MAP = {
    ROUTE_RUN_SQL: ROUTE_RUN_SQL,
    ROUTE_CORRECT_SQL: ROUTE_CORRECT_SQL,
    ROUTE_SQL_CORRECTION_FAILED: ROUTE_SQL_CORRECTION_FAILED,
}


def route_after_validate(state: DataAgentState) -> str:
    """validate_sql 之后的三分支路由。

    correction_attempts 含义：correct_sql 已成功执行的次数。
    路由规则：
      - error is None           → run_sql（校验通过）
      - error 且 attempts < max  → correct_sql（仍有修正配额）
      - error 且 attempts >= max → sql_correction_failed（配额耗尽）
    """
    if state.get("error") is None:
        return ROUTE_RUN_SQL

    attempts = state.get("correction_attempts", 0)
    max_attempts = app_config.agent.max_correction_attempts

    if attempts < max_attempts:
        return ROUTE_CORRECT_SQL
    return ROUTE_SQL_CORRECTION_FAILED
