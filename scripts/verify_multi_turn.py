"""批次 C 多轮对话端到端验证脚本（临时，验证后可删除）"""

import asyncio

from langchain_core.messages import AIMessage, HumanMessage

from app.agent.context import DataAgentContext
from app.agent.graph import graph
from app.clients.embedding_client_manager import embedding_client_manager
from app.clients.es_client_manager import es_client_manager
from app.clients.mysql_client_manager import (
    dw_mysql_client_manager,
    meta_mysql_client_manager,
)
from app.clients.qdrant_client_manager import qdrant_client_manager
from app.repositories.es.value_es_repository import ValueESRepository
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository
from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository
from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository

THREAD_ID = "batch-c-multi-turn-test"


async def run_round(query: str, context: DataAgentContext) -> None:
    config = {"configurable": {"thread_id": THREAD_ID}}
    input_state = {
        "query": query,
        "messages": [HumanMessage(content=query)],
    }
    print(f"\n=== 用户输入: {query} ===")
    async for chunk in graph.astream(
        input=input_state,
        config=config,
        context=context,
        stream_mode="custom",
    ):
        if chunk.get("type") == "result":
            print(f"结果: {chunk.get('data')}")

    snapshot = await graph.aget_state(config)
    values = snapshot.values
    print(f"改写后 query: {values.get('query')}")
    messages = values.get("messages") or []
    ai_count = sum(1 for m in messages if isinstance(m, AIMessage))
    print(f"messages 条数: {len(messages)}, AIMessage 数: {ai_count}")


async def main():
    qdrant_client_manager.init()
    embedding_client_manager.init()
    es_client_manager.init()
    meta_mysql_client_manager.init()
    dw_mysql_client_manager.init()

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

        await run_round("统计华北地区的销售总额", context)
        await run_round("那按省份细分呢？", context)

    await qdrant_client_manager.close()
    await es_client_manager.close()
    await meta_mysql_client_manager.close()
    await dw_mysql_client_manager.close()


if __name__ == "__main__":
    asyncio.run(main())
