"""unstructured_agent 节点调用 Vector Search 的轻量封装：输入 query，返回 top-k chunk。

不用 databricks-ai-search 这个第三方包——它的认证逻辑只认 PAT 或 Service Principal 的静态
token，不支持 Azure CLI（az login）这种会自动刷新的动态令牌，本项目切到 Azure 原生认证后
这个包会在建 client 那一步直接报 "Please specify either personal access token or service
principal client ID and secret."。改用 databricks-sdk 自带的
`WorkspaceClient().vector_search_indexes`，走跟其他所有代码一致的同一套认证
（`get_workspace_client()`），不需要给这一个模块单独处理认证问题。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.config import settings
from src.db_client import get_workspace_client

_RESULT_COLUMNS = ["chunk_id", "source_file", "section_title", "chunk_type", "content"]


@dataclass
class RetrievedChunk:
    chunk_id: str
    source_file: str
    section_title: str
    chunk_type: str
    content: str
    score: float


def retrieve(query: str, k: int = 8) -> list[RetrievedChunk]:
    settings.require("vector_search_index")
    client = get_workspace_client()
    response = client.vector_search_indexes.query_index(
        index_name=settings.vector_search_index,
        columns=_RESULT_COLUMNS,
        query_text=query,
        num_results=k,
    )
    data_array = (response.result.data_array if response.result else None) or []
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
