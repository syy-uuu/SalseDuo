"""unstructured_agent 节点：调用 Vector Search 检索 documents_generated/ 的政策文档片段。"""

from __future__ import annotations

from src.graph.state import AgentState
from src.clients.retriever import retrieve

# 实测发现英文 embedding 模型（databricks-gte-large-en）对中文查询的跨语言匹配不够强，
# 有些关键段落（比如"超限15%需要谁审批"对应的 Exception Handling 段落）要到 top-7/8
# 才能出现，top-5 会漏掉。这里保守取 8，而不是继续调 embedding 模型/query 改写
# （更大的改动，超出这次修复范围）。
_TOP_K = 8


def unstructured_agent_node(state: AgentState) -> AgentState:
    query = state.get("user_query", "")
    chunks = retrieve(query, k=_TOP_K)

    if not chunks:
        credit_info = "未在政策文档中检索到相关内容。"
    else:
        credit_info = "\n---\n".join(
            f"[{c.source_file} | {c.section_title}] {c.content}" for c in chunks
        )

    return {
        **state,
        "credit_info": credit_info,
        "trace": [
            {
                "step": "unstructured_agent",
                "loop_index": state.get("loop_count", 0),
                "reasoning": f"检索 query: {query}",
                "retrieved_chunks": [
                    {
                        "chunk_id": c.chunk_id,
                        "source_file": c.source_file,
                        "section_title": c.section_title,
                        "score": c.score,
                    }
                    for c in chunks
                ],
                "output_summary": credit_info,
            }
        ],
    }
