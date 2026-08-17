"""The three categories of end-to-end test cases required by Step 6:
1. Questions needing only structured data
2. Questions needing only unstructured data
3. Multi-hop questions (unstructured -> compute -> structured -> possibly unstructured again)

These cases need a real, reachable Databricks workspace (Genie Space, Vector Search
Index, and LLM serving endpoint all already provisioned) — they're skipped until real
credentials are configured in .env. Once credentials are in place, just run
`pytest tests/test_integration_cases.py -v` — no code changes needed.
"""

import pytest

from src.config import settings
from src.graph.build_graph import build_graph

_READY = bool(
    (
        settings.databricks_config_profile
        or (
            settings.azure_subscription_id
            and settings.azure_resource_group_name
            and settings.azure_databricks_workspace_name
        )
    )
    and settings.genie_space_id
    and settings.vector_search_index
)

pytestmark = pytest.mark.skipif(
    not _READY,
    reason="Needs real Databricks credentials plus a provisioned GENIE_SPACE_ID / VECTOR_SEARCH_INDEX; not satisfied, skipping end-to-end tests.",
)


def _run(question: str) -> dict:
    graph = build_graph()
    return graph.invoke(
        {"messages": [{"role": "user", "content": question}], "user_query": question, "loop_count": 0}
    )


def test_structured_only_question():
    result = _run("What were the top 5 products by 2013 sales?")
    assert result.get("structured_result")
    assert result["messages"][-1]["role"] == "assistant"


def test_unstructured_only_question():
    result = _run("What are the payment term and credit limit cap for a Tier 2 Preferred Account customer?")
    assert result.get("credit_info")
    assert result["messages"][-1]["role"] == "assistant"


def test_multi_hop_credit_then_structured_question():
    result = _run(
        "What are customer Bike World's current annual purchase volume and years as a customer? "
        "Per company credit policy, what's the maximum credit limit and payment term they can apply for, "
        "and who needs to approve it if they apply for a $1,000,000 credit limit?"
    )
    assert result.get("credit_info")
    assert result.get("structured_result")
    assert result["messages"][-1]["role"] == "assistant"


def test_loop_count_never_exceeds_configured_max():
    result = _run("A deliberately vague question likely to make the router repeatedly judge the information insufficient")
    assert result.get("loop_count", 0) <= settings.max_router_loops


def test_multi_turn_memory_reuses_genie_conversation_and_resolves_pronouns():
    """Added 2026-07-28 to cover multi-turn memory: the same genie_conversation_id must
    be reused across turns, and a pronoun in the second turn ("they") must correctly
    resolve, via history, to the customer asked about in the first turn, without the
    user having to repeat the customer's name. See docs/AGENT_MEMORY_DESIGN.md for the
    design rationale."""
    graph = build_graph()

    q1 = "What is customer Bike World's annual purchase volume?"
    result1 = graph.invoke(
        {"messages": [{"role": "user", "content": q1}], "user_query": q1, "loop_count": 0}
    )
    genie_cid_1 = result1.get("genie_conversation_id")
    assert genie_cid_1, "the first turn should already have produced a genie_conversation_id"

    q2 = "And what's their credit limit cap?"
    messages = result1["messages"] + [{"role": "user", "content": q2}]
    result2 = graph.invoke(
        {
            "messages": messages,
            "user_query": q2,
            "genie_conversation_id": genie_cid_1,
            "loop_count": 0,
        }
    )
    assert result2.get("genie_conversation_id") == genie_cid_1, (
        "the second turn should reuse the same genie_conversation_id, not open a new Genie conversation"
    )
    # The second-turn question doesn't mention "Bike World" again — if history weren't
    # taking effect, router/finalize would receive nothing but the isolated question
    # "and what's their credit limit cap", and would very likely just say it doesn't
    # know which customer is meant, never reaching the structured-query step; this
    # asserts that a structured query/business-rule result was indeed produced, as
    # indirect evidence that the reference was "understood" (not asserting a specific
    # dollar amount, since LLM-generated natural-language phrasing isn't suited to exact
    # string matching).
    assert result2.get("structured_result") or result2.get("business_rule_result")
