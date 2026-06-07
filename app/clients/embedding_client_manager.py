import asyncio
from typing import Optional
from langchain_openai import OpenAIEmbeddings
from app.conf.app_config import EmbeddingConfig, app_config

class EmbeddingClientManager:
    def __init__(self, config: EmbeddingConfig):
        self.client: Optional[OpenAIEmbeddings] = None
        self.config = config

    def init(self):
        self.client = OpenAIEmbeddings(
            model=self.config.model,
            base_url=self.config.base_url,
            api_key=self.config.api_key,
            max_retries=5,
            timeout=60,
            check_embedding_ctx_length=False,
            model_kwargs={"encoding_format": "float"},
        )


embedding_client_manager = EmbeddingClientManager(app_config.embedding)

if __name__ == "__main__":
    embedding_client_manager.init()
    client = embedding_client_manager.client

    async def test():
        text = "What is deep learning?"
        query_result = await client.aembed_query(text)
        print(query_result[:3])

    asyncio.run(test())

