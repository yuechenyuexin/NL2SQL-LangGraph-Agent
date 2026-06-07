"""SQL 修正循环验证：路由单测 + 正常 E2E + 修正耗尽子图验证"""

import asyncio
import sys
from unittest.mock import patch

from langchain_core.messages import HumanMessage
from langgraph.constants import END
from langgraph.graph import StateGraph

from app.agent.context import DataAgentContext
from app.agent.graph import graph
from app.agent.nodes.sql_correction_failed import sql_correction_failed
from app.agent.nodes.validate_sql import validate_sql
from app.agent.routers import (
    ROUTE_CORRECT_SQL,
    ROUTE_RUN_SQL,
    ROUTE_SQL_CORRECTION_FAILED,
    VALIDATE_SQL_PATH_MAP,
    route_after_validate,
)
from app.agent.state import DataAgentState
from app.clients.embedding_client_manager import embedding_client_manager
from app.clients.es_client_manager import es_client_manager
from app.clients.mysql_client_manager import (
    dw_mysql_client_manager,
    meta_mysql_client_manager,
)
from app.clients.qdrant_client_manager import qdrant_client_manager
from app.conf.app_config import app_config
from app.repositories.es.value_es_repository import ValueESRepository
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository
from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository
from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository

SUCCESS_QUERY = "统计华北地区的销售总额"
THREAD_SUCCESS = "verify-correction-loop-success"


def test_route_after_validate() -> None:
    """路由函数单元校验"""

    max_attempts = app_config.agent.max_correction_attempts

    assert route_after_validate({"error": None}) == ROUTE_RUN_SQL
    assert route_after_validate({"error": "syntax", "correction_attempts": 0}) == ROUTE_CORRECT_SQL
    assert (
        route_after_validate({"error": "syntax", "correction_attempts": max_attempts - 1})
        == ROUTE_CORRECT_SQL
    )
    assert (
        route_after_validate({"error": "syntax", "correction_attempts": max_attempts})
        == ROUTE_SQL_CORRECTION_FAILED
    )
    print("[PASS] route_after_validate 单元测试")


async def _init_clients() -> None:
    qdrant_client_manager.init()
    embedding_client_manager.init()
    es_client_manager.init()
    meta_mysql_client_manager.init()
    dw_mysql_client_manager.init()


async def _close_clients() -> None:
    await qdrant_client_manager.close()
    await es_client_manager.close()
    await meta_mysql_client_manager.close()
    await dw_mysql_client_manager.close()


def _find_event(events: list[dict], event_type: str) -> dict | None:
    for event in reversed(events):
        if event.get("type") == event_type:
            return event
    return None


def _count_correction_steps(events: list[dict]) -> int:
    return sum(
        1
        for e in events
        if e.get("type") == "progress"
        and isinstance(e.get("step"), str)
        and e["step"].startswith("校正SQL（第")
        and e.get("status") == "success"
    )


async def verify_success_path(context: DataAgentContext) -> None:
    """场景 1：正常提问，全链路 E2E，应返回 result"""

    print(f"\n=== 场景 1：正常提问（全链路 E2E）===\nquery: {SUCCESS_QUERY}")

    config = {"configurable": {"thread_id": THREAD_SUCCESS}}
    input_state = {
        "query": SUCCESS_QUERY,
        "messages": [HumanMessage(content=SUCCESS_QUERY)],
    }

    events: list[dict] = []
    async for chunk in graph.astream(
        input=input_state,
        config=config,
        context=context,
        stream_mode="custom",
    ):
        events.append(chunk)

    snapshot = await graph.aget_state(config)
    state = snapshot.values

    result_event = _find_event(events, "result")
    error_event = _find_event(events, "error")

    assert result_event is not None, "预期收到 type=result 事件"
    assert error_event is None, "正常路径不应出现 type=error 事件"
    assert state.get("final_error") is None

    correction_count = _count_correction_steps(events)
    print(f"  修正次数: {correction_count}")
    print(f"  返回行数: {len(result_event.get('data') or [])}")
    print(f"  correction_attempts(state): {state.get('correction_attempts', 0)}")
    print("[PASS] 正常提问路径")


async def _passthrough_correct(state: DataAgentState, runtime):
    """跳过 LLM，仅递增计数，用于修正耗尽子图验证"""

    attempts_done = state.get("correction_attempts", 0)
    attempt_no = attempts_done + 1
    error = state.get("error") or "mock error"
    errors = list(state.get("correction_errors") or [])
    errors.append(error)

    writer = runtime.stream_writer
    step = f"校正SQL（第{attempt_no}次）"
    writer({"type": "progress", "step": step, "status": "running"})
    writer({"type": "progress", "step": step, "status": "success"})

    return {
        "sql": "SELECT * FROM __nonexistent_table_for_test__",
        "correction_attempts": attempt_no,
        "correction_errors": errors,
    }


def _build_correction_loop_graph():
    """仅包含 validate ↔ correct ↔ fail 的子图，用于失败路径验证"""

    async def stub_run_sql(state: DataAgentState, runtime):
        writer = runtime.stream_writer
        writer({"type": "result", "data": []})
        return {}

    builder = StateGraph(state_schema=DataAgentState, context_schema=DataAgentContext)
    builder.add_node("validate_sql", validate_sql)
    builder.add_node("correct_sql", _passthrough_correct)
    builder.add_node("sql_correction_failed", sql_correction_failed)
    builder.add_node("run_sql", stub_run_sql)
    builder.set_entry_point("validate_sql")
    builder.add_conditional_edges(
        "validate_sql",
        route_after_validate,
        VALIDATE_SQL_PATH_MAP,
    )
    builder.add_edge("correct_sql", "validate_sql")
    builder.add_edge("run_sql", END)
    builder.add_edge("sql_correction_failed", END)
    return builder.compile()


async def verify_failure_path(context: DataAgentContext) -> None:
    """场景 2：validate 始终失败，子图验证修正配额耗尽"""

    print("\n=== 场景 2：修正耗尽失败（SQL 闭环子图）===")
    max_attempts = app_config.agent.max_correction_attempts

    async def always_fail_validate(self, sql: str) -> None:
        raise RuntimeError(f"mock EXPLAIN 失败: {sql[:60]}")

    loop_graph = _build_correction_loop_graph()
    initial_state: DataAgentState = {
        "query": "测试修正耗尽",
        "messages": [],
        "keywords": [],
        "retrieved_column_infos": [],
        "retrieved_metric_infos": [],
        "retrieved_value_infos": [],
        "table_infos": [],
        "metric_infos": [],
        "date_info": {"date": "", "weekday": "", "quarter": ""},
        "db_info": {"dialect": "mysql", "version": "8.0"},
        "sql": "SELECT * FROM bad_initial_sql",
        "error": None,
        "correction_attempts": 0,
        "correction_errors": [],
        "final_error": None,
    }

    events: list[dict] = []
    with patch.object(DWMySQLRepository, "validate", always_fail_validate):
        async for chunk in loop_graph.astream(
            input=initial_state,
            context=context,
            stream_mode="custom",
        ):
            events.append(chunk)

    error_event = _find_event(events, "error")
    result_event = _find_event(events, "result")
    correction_count = _count_correction_steps(events)

    assert result_event is None, "失败路径不应返回 result"
    assert error_event is not None, "失败路径应收到 type=error 事件"
    assert correction_count == max_attempts, f"预期修正 {max_attempts} 次，实际 {correction_count}"
    assert "已尝试修正" in (error_event.get("message") or "")

    print(f"  修正次数: {correction_count}")
    print(f"  SSE error 摘要: {(error_event.get('message') or '')[:120]}…")
    print("[PASS] 修正耗尽失败路径")


async def main() -> None:
    test_route_after_validate()

    await _init_clients()
    try:
        async with (
            meta_mysql_client_manager.session_factory() as meta_session,
            dw_mysql_client_manager.session_factory() as dw_session,
        ):
            context = DataAgentContext(
                column_qdrant_repository=ColumnQdrantRepository(
                    qdrant_client_manager.client
                ),
                embedding_client=embedding_client_manager.client,
                metric_qdrant_repository=MetricQdrantRepository(
                    qdrant_client_manager.client
                ),
                value_es_repository=ValueESRepository(es_client_manager.client),
                meta_mysql_repository=MetaMySQLRepository(meta_session),
                dw_mysql_repository=DWMySQLRepository(dw_session),
            )

            await verify_success_path(context)
            await verify_failure_path(context)
    finally:
        await _close_clients()

    print("\n全部验证通过。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"\n[FAIL] {e}", file=sys.stderr)
        raise
