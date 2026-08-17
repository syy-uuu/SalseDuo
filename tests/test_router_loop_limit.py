"""Verifies CLAUDE.md section 7 risk item 3: the router loop cap must actually take
effect.

Key point: once loop_count reaches the cap, router_node must force-return
next_step="finalize" *before* calling the LLM, so this safety valve can be verified
fully offline, with no real Databricks/LLM connection needed — proving "the Nth call
forces finalize with no error" doesn't require a real workspace connection.
"""

from src.config import settings
from src.graph.router import router_node


def test_router_forces_finalize_when_loop_limit_reached():
    state = {
        "user_query": "A question designed to keep making the router judge the information insufficient",
        "loop_count": settings.max_router_loops,
    }

    result = router_node(state)

    assert result["next_step"] == "finalize"
    assert "loop cap" in result["router_reason"]
    # loop_count should not keep incrementing along the forced-termination branch
    assert result["loop_count"] == settings.max_router_loops


def test_router_forces_finalize_when_loop_limit_exceeded():
    state = {"user_query": "any question", "loop_count": settings.max_router_loops + 3}
    result = router_node(state)
    assert result["next_step"] == "finalize"
