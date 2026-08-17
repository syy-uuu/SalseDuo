"""The unstructured_agent node: calls Vector Search to retrieve policy document chunks
from documents_generated/."""

from __future__ import annotations

from src.graph.state import AgentState
from src.clients.retriever import retrieve

# In practice, the English embedding model (databricks-gte-large-en) doesn't
# cross-lingually match Chinese-language queries as strongly — some key passages (e.g.
# the Exception Handling section covering "who needs to approve a 15% overage") don't
# surface until around top-7/8, and top-5 misses them. Set conservatively to 8 rather
# than further tuning the embedding model / rewriting the query (a bigger change, out of
# scope for this fix).
_TOP_K = 8


def _build_query(state: AgentState) -> str:
    # In multi-hop scenarios, if this node is routed back to a second (or later) time
    # with the retrieval text still being the unchanged original user_query, it's likely
    # to retrieve the same chunks as the first pass and miss any new information —
    # router_reason is router's reasoning for why it dispatched here again this time,
    # and structured_result is the structured data already retrieved; folding both into
    # the retrieval text lets the query actually carry "what's specifically still
    # missing this time" instead of repeating the original question. See
    # docs/CODE_REVIEW_FINDINGS.md item 3.
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
        credit_info = "No relevant content found in the policy documents."
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
                "reasoning": f"Retrieval query: {query}",
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
