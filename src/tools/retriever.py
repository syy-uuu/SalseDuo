"""unstructured_agent 节点调用 Vector Search 的轻量封装：输入 query，返回 top-k chunk。

按 CLAUDE.md 第3节要求，直接调 Vector Search 查询接口，不额外包装成 Knowledge Assistant。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from databricks.ai_search.client import VectorSearchClient

from src.config import settings

_RESULT_COLUMNS = ["chunk_id", "source_file", "section_title", "chunk_type", "content"]


@dataclass
class RetrievedChunk:
    chunk_id: str
    source_file: str
    section_title: str
    chunk_type: str
    content: str
    score: float


@lru_cache(maxsize=1)
def _get_index():
    settings.require("vector_search_endpoint", "vector_search_index")
    vsc = VectorSearchClient(disable_notice=True)
    return vsc.get_index(
        endpoint_name=settings.vector_search_endpoint,
        index_name=settings.vector_search_index,
    )


def retrieve(query: str, k: int = 5) -> list[RetrievedChunk]:
    index = _get_index()
    result = index.similarity_search(columns=_RESULT_COLUMNS, query_text=query, num_results=k)
    data_array = result.get("result", {}).get("data_array", [])
    chunks = []
    for row in data_array:
        values = dict(zip(_RESULT_COLUMNS + ["score"], row))
        chunks.append(
            RetrievedChunk(
                chunk_id=values["chunk_id"],
                source_file=values["source_file"],
                section_title=values["section_title"],
                chunk_type=values["chunk_type"],
                content=values["content"],
                score=float(values.get("score", 0.0)),
            )
        )
    return chunks
