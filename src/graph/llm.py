"""编排用的 LLM 客户端，统一从这里获取，走 Databricks Foundation Model API。"""

from __future__ import annotations

from functools import lru_cache

from databricks_langchain import ChatDatabricks

from src.config import settings


@lru_cache(maxsize=1)
def get_llm() -> ChatDatabricks:
    return ChatDatabricks(endpoint=settings.llm_serving_endpoint, temperature=0)
