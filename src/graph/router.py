"""The router node: decides, at every step, whether to "keep querying structured data /
keep querying unstructured data / finish".

This is the only node in the whole graph that makes routing decisions — the design is
deliberately *not* a two-stage "dispatcher node + summarizer node": deciding what to do
next and judging whether the information gathered so far is sufficient are the same
decision, happening repeatedly, so it's implemented as this one node plus a looping
edge, not two roles calling each other.

next_step goes through forced structured output (Pydantic + with_structured_output)
rather than parsing free text, to avoid a routing-result parse failure making the state
machine's behavior unpredictable.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.config import settings
from src.clients.llm import get_llm
from src.graph.state import AgentState, NextStep, recent_history_text
from prompts.loader import render_prompt

_SYSTEM_PROMPT = render_prompt("router")


class RouterDecision(BaseModel):
    next_step: NextStep = Field(description="Next action: structured | unstructured | finalize")
    reason: str = Field(description="A brief, one-sentence rationale for this decision")


# The underlying LLM occasionally emits malformed tool-calling-format JSON (a stray
# trailing ")") when the `reason` field is longer, under forced structured output — this
# is a model generation-quality issue, not a bug in our code, and it's reproducible, not
# flaky. Guarded with a small retry; if all retries fail, safely degrade to finalize
# rather than crashing the whole request — the same "better to end early than let the
# state machine run out of control" principle used for the loop_count cap.
_MAX_DECISION_ATTEMPTS = 3


def router_node(state: AgentState) -> AgentState:
    loop_count = state.get("loop_count", 0)

    if loop_count >= settings.max_router_loops:
        reason = f"Reached the router loop cap ({settings.max_router_loops}); forcing finish."
        return {
            **state,
            "next_step": "finalize",
            "router_reason": reason,
            "trace": [
                {"step": "router", "loop_index": loop_count, "reasoning": reason, "output_summary": "next_step=finalize (loop cap)"}
            ],
        }

    context = _render_context(state)
    llm = get_llm().with_structured_output(RouterDecision)
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": context},
    ]

    decision: RouterDecision | None = None
    last_error: Exception | None = None
    for _ in range(_MAX_DECISION_ATTEMPTS):
        try:
            decision = llm.invoke(messages)
            break
        except Exception as exc:  # noqa: BLE001 - the underlying model's format-error type isn't fixed, so catching broadly is more robust here
            last_error = exc

    if decision is None:
        reason = f"The routing model produced malformed output {_MAX_DECISION_ATTEMPTS} times in a row; safely degrading to finalize: {last_error}"
        return {
            **state,
            "next_step": "finalize",
            "router_reason": reason,
            "trace": [
                {"step": "router", "loop_index": loop_count, "reasoning": reason, "output_summary": "next_step=finalize (LLM format failure)"}
            ],
        }

    next_loop_count = loop_count if decision.next_step == "finalize" else loop_count + 1
    return {
        **state,
        "next_step": decision.next_step,
        "router_reason": decision.reason,
        "loop_count": next_loop_count,
        "trace": [
            {
                "step": "router",
                "loop_index": loop_count,
                "reasoning": decision.reason,
                "output_summary": f"next_step={decision.next_step}",
            }
        ],
    }


def _render_context(state: AgentState) -> str:
    parts = []
    history = recent_history_text(state)
    if history:
        parts.append(history)
    parts.append(f"User question: {state.get('user_query', '')}")
    if state.get("credit_info"):
        parts.append(f"Policy/rule text retrieved so far: {state['credit_info']}")
    if state.get("business_rule_result"):
        parts.append(f"Business rule result computed so far: {state['business_rule_result']}")
    if state.get("structured_result"):
        parts.append(f"Structured data query result so far: {state['structured_result']}")
    parts.append(f"Current loop count: {state.get('loop_count', 0)} / cap {settings.max_router_loops}")
    return "\n".join(parts)


def route_after_router(state: AgentState) -> NextStep:
    return state.get("next_step", "finalize")
