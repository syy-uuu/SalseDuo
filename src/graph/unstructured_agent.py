"""unstructured_agent 节点：调用 Vector Search 检索 documents_generated/ 的政策文档片段。"""

from __future__ import annotations

from src.graph.state import AgentState
from src.clients.retriever import retrieve

# 实测发现英文 embedding 模型（databricks-gte-large-en）对中文查询的跨语言匹配不够强，
# 有些关键段落（比如"超限15%需要谁审批"对应的 Exception Handling 段落）要到 top-7/8
# 才能出现，top-5 会漏掉。这里保守取 8，而不是继续调 embedding 模型/query 改写
# （更大的改动，超出这次修复范围）。
_TOP_K = 8


def _build_query(state: AgentState) -> str:
    # 多跳场景里第二次(及以后)被路由回这个节点时，如果检索文本还是原封不动的
    # user_query，大概率检索到跟第一次相同的 chunk，查不到新信息——router_reason
    # 是 router 这次为什么又派过来的理由，structured_result 是已经查到的结构化数据，
    # 两者拼进检索文本能让 query 实际携带"这次具体还缺什么"的信息，而不是重复第一次的
    # 原始问题。见 docs/CODE_REVIEW_FINDINGS.md 第 3 条。
    parts = [state.get("user_query", "")]
    if state.get("router_reason"):
        parts.append(state["router_reason"])
    if state.get("structured_result"):
        parts.append(state["structured_result"])
    return "\n".join(parts)


def unstructured_agent_node(state: AgentState) -> AgentState:
    query = _build_query(state)
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
