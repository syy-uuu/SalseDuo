"""Verifies the fix for docs/CODE_REVIEW_FINDINGS.md item 3: in a multi-hop scenario,
unstructured_agent's second retrieval query needs to carry router_reason/
structured_result, and must not be identical to the first.

Pure offline unit test, no Databricks connection — retrieve() is mocked out entirely;
this only cares whether the query argument passed to it is correct, not retrieval
quality itself (see docs/VERIFICATION_2026-07-27.md Step 3.7 for how retrieval quality
is verified, which does need a real Vector Search connection).
"""

from __future__ import annotations

from unittest import mock

from src.graph.unstructured_agent import _TOP_K, _build_query, unstructured_agent_node


def test_build_query_only_user_query_when_no_extra_context():
    """The first time this node runs, state usually has no router_reason/
    structured_result yet, so the query should just be the original question, with
    nothing extra tacked on."""
    state = {"user_query": "What's the credit limit cap for a Tier 2 customer?"}
    assert _build_query(state) == "What's the credit limit cap for a Tier 2 customer?"


def test_build_query_includes_router_reason_and_structured_result():
    state = {
        "user_query": "What's the credit limit cap for a Tier 2 customer?",
        "router_reason": "Already found the customer's purchase volume; still need to confirm settlement-method compliance",
        "structured_result": "Customer Bike World has an annual purchase volume of $500,000",
    }
    query = _build_query(state)
    assert "What's the credit limit cap for a Tier 2 customer?" in query
    assert "Already found the customer's purchase volume; still need to confirm settlement-method compliance" in query
    assert "Customer Bike World has an annual purchase volume of $500,000" in query


def test_second_hop_query_differs_from_first_hop():
    """Simulates a multi-hop scenario: the first time unstructured_agent runs, state
    has no router_reason/structured_result yet; the second time, router has already
    made one decision and structured_agent has already run once, so the query this time
    should differ from the first — this is the core problem item 3 fixes (before the
    fix, the two queries were always identical, which very likely retrieved the same
    chunks both times)."""
    first_state = {"user_query": "What's the credit limit cap for customer Bike World?", "loop_count": 0}
    second_state = {
        **first_state,
        "loop_count": 2,
        "router_reason": "Already found the policy rule; still need the customer's actual purchase data to compute the limit",
        "structured_result": "Bike World has an annual purchase volume of $500,000, 3 years as a customer",
    }
    assert _build_query(first_state) != _build_query(second_state)


def test_unstructured_agent_node_passes_built_query_to_retrieve():
    """Confirms the node actually uses _build_query()'s result for retrieval, rather
    than still passing user_query directly (having the _build_query() function exist
    but the node forgetting to switch over to it would make the fix a no-op)."""
    state = {"user_query": "question", "router_reason": "additional context", "loop_count": 1}
    with mock.patch(
        "src.graph.unstructured_agent.retrieve", return_value=[]
    ) as mocked_retrieve:
        unstructured_agent_node(state)

    mocked_retrieve.assert_called_once()
    called_query = mocked_retrieve.call_args.args[0]
    called_k = mocked_retrieve.call_args.kwargs.get("k")
    assert called_query == _build_query(state)
    assert called_k == _TOP_K
