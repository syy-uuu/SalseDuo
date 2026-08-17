"""Lightweight wrapper the unstructured_agent node uses to call Vector Search:
takes a query, returns the top-k chunks.

Doesn't use the third-party `databricks-ai-search` package — its auth logic only
accepts a static PAT or Service Principal token, not the dynamically-refreshed token
Azure CLI (`az login`) produces. After this project switched to native Azure auth, that
package fails outright at client-construction time with "Please specify either personal
access token or service principal client ID and secret." Uses databricks-sdk's own
`WorkspaceClient().vector_search_indexes` instead, going through the same auth as
every other module in the project (`get_workspace_client()`) — no need to handle auth
separately just for this one module.
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
