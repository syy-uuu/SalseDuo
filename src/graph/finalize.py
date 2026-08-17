"""The finalize node: synthesizes all intermediate results into a final answer."""

from __future__ import annotations

from src.config import settings
from src.clients.llm import get_llm
from src.graph.state import AgentState, recent_history_text
from prompts.loader import render_prompt

_SYSTEM_PROMPT = render_prompt("finalize")


def finalize_node(state: AgentState) -> AgentState:
    loop_count = state.get("loop_count", 0)
    hit_loop_limit = loop_count >= settings.max_router_loops

    context_parts = []
    history = recent_history_text(state)
    if history:
        context_parts.append(history)
    context_parts.append(f"User question: {state.get('user_query', '')}")
    if state.get("credit_info"):
        context_parts.append(f"Policy document retrieval result: {state['credit_info']}")
    if state.get("business_rule_result"):
        context_parts.append(f"Business rule computation result: {state['business_rule_result']}")
    if state.get("structured_result"):
        context_parts.append(f"Structured data query result: {state['structured_result']}")
    if hit_loop_limit:
        context_parts.append(
            f"Note: routing has reached the loop cap ({settings.max_router_loops}); "
            "the following answer is based on the information gathered so far and may be incomplete."
        )

    llm = get_llm()
    response = llm.invoke(
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(context_parts)},
        ]
    )
    final_text = response.content
    if hit_loop_limit and "may be incomplete" not in final_text:
        final_text += "\n\n(Note: this answer was forced out after hitting the routing loop cap and may be incomplete.)"

    messages = list(state.get("messages", []))
    messages.append({"role": "assistant", "content": final_text})
    return {
        **state,
        "messages": messages,
        "trace": [
            {
                "step": "finalize",
                "loop_index": loop_count,
                "reasoning": "hit_loop_limit" if hit_loop_limit else "sufficient_info",
                "output_summary": final_text,
            }
        ],
    }
