"""Shared state definition for the LangGraph StateGraph."""

from __future__ import annotations

import operator
from typing import Literal, TypedDict
from typing_extensions import Annotated

from prompts.loader import render_prompt

NextStep = Literal["structured", "unstructured", "finalize"]


class TraceStep(TypedDict, total=False):
    """A single step in the white-box trace. Each node appends one entry when it
    finishes, never overwriting earlier entries — this way multi-hop scenarios
    (the same node visited more than once) still show each hop's own intermediate
    result, not just the state after the last overwrite."""

    step: str  # "router" | "structured_agent" | "unstructured_agent" | "finalize"
    loop_index: int
    reasoning: str | None  # router's reasoning, or a short note on what this node did
    question_sent: str | None  # the raw question sent to Genie (structured_agent only)
    sql_queries: list[str] | None  # SQL Genie actually generated and ran (structured_agent only)
    retrieved_chunks: list[dict] | None  # retrieved document chunks (unstructured_agent only)
    output_summary: str | None  # the text output produced by this step
    error: str | None  # detailed error info if this step failed (e.g. why Genie SQL failed)


class AgentState(TypedDict, total=False):
    messages: list[dict]  # full conversation history, [{"role": ..., "content": ...}, ...]
    user_query: str

    credit_info: str | None  # policy/rule text retrieved from unstructured search, relevant to this question
    business_rule_result: dict | None  # structured result when structured_agent hits a business-rule UC Function
    structured_result: str | None  # text answer from the most recent Genie structured query

    genie_conversation_id: str | None

    loop_count: int
    next_step: NextStep | None
    router_reason: str | None

    # Annotated + operator.add: each node returns a single-element list containing only
    # "its own step" — LangGraph accumulates/merges these automatically, so no node has
    # to read the existing history back out and append to it manually.
    trace: Annotated[list[TraceStep], operator.add]


# router and finalize both need "what's been said so far" in the LLM context; that logic
# is factored into this shared function so the two nodes don't each maintain their own
# copy and risk drifting out of sync.
_MAX_HISTORY_MESSAGES = 10  # the last 5 turns (excluding the current message), to keep the prompt from growing unbounded
# This number wasn't rigorously derived — it's a reasonable default, not a tuned optimum.
# See docs/CODE_REVIEW_FINDINGS.md item 10: it's not on the same scale as Genie's own
# conversation memory window (its size/duration is opaque — we can't see or control it),
# so the risk of the two memory layers having mismatched "how far back can it remember"
# still exists. Going from 3 turns to 5 only mitigates it, it doesn't resolve it.


def recent_history_text(state: AgentState) -> str:
    """Format the history in `messages` (everything before the current message) into a
    block of text that can be dropped straight into a prompt. Returns an empty string
    when there's no history (the first turn).

    Convention: the caller (agent.py::_run_graph / chat.py) is responsible for including
    the current user message as the last item of `messages` passed into initial_state;
    this function always takes messages[:-1] and doesn't need to figure out "which one is
    the current message" itself."""
    messages = state.get("messages") or []
    history = messages[:-1][-_MAX_HISTORY_MESSAGES:]
    if not history:
        return ""
    lines = [f"{m['role']}: {m['content']}" for m in history]
    return render_prompt("history_framing") + "\n" + "\n".join(lines)
