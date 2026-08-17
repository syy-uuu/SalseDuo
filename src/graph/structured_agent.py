"""The structured_agent node: calls the Genie Space to query AdventureWorksLT
structured data; when needed, Genie invokes the attached UC Functions
(calculate_credit_terms / check_large_transaction_compliance) to compute business
rules.

Genie is a stateful multi-turn conversation: reuses genie_conversation_id from state
instead of starting a new conversation each time.
"""

from __future__ import annotations

from src.graph.state import AgentState
from src.clients.genie_client import ask_genie

_BUSINESS_RULE_FUNCTIONS = ["calculate_credit_terms", "check_large_transaction_compliance"]


def _build_question(state: AgentState) -> str:
    parts = [state.get("user_query", "")]
    if state.get("credit_info"):
        parts.append(
            "Additional context (from company policy documents, for reference when computing business rules):\n" + state["credit_info"]
        )
    return "\n\n".join(parts)


def structured_agent_node(state: AgentState) -> AgentState:
    question = _build_question(state)
    answer = ask_genie(question, conversation_id=state.get("genie_conversation_id"))

    business_rule_result = None
    for fn_name in _BUSINESS_RULE_FUNCTIONS:
        matched = [q for q in answer.sql_queries if fn_name in q]
        if matched:
            business_rule_result = {"invoked_function": fn_name, "raw_query": matched[0]}
            break

    update: AgentState = {
        **state,
        "structured_result": answer.text,
        "genie_conversation_id": answer.conversation_id,
        "trace": [
            {
                "step": "structured_agent",
                "loop_index": state.get("loop_count", 0),
                "question_sent": question,
                "sql_queries": answer.sql_queries,
                "output_summary": answer.text,
                "error": answer.error,
            }
        ],
    }
    if business_rule_result:
        update["business_rule_result"] = business_rule_result
    return update
