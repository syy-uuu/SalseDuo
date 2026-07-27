"""组装 LangGraph StateGraph: router 节点带循环的条件边，串联
structured_agent / unstructured_agent / finalize 四个节点。"""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from src.graph.finalize import finalize_node
from src.graph.router import route_after_router, router_node
from src.graph.state import AgentState
from src.graph.structured_agent import structured_agent_node
from src.graph.unstructured_agent import unstructured_agent_node


def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("router", router_node)
    graph.add_node("structured_agent", structured_agent_node)
    graph.add_node("unstructured_agent", unstructured_agent_node)
    graph.add_node("finalize", finalize_node)

    graph.set_entry_point("router")
    graph.add_conditional_edges(
        "router",
        route_after_router,
        {
            "structured": "structured_agent",
            "unstructured": "unstructured_agent",
            "finalize": "finalize",
        },
    )
    graph.add_edge("structured_agent", "router")
    graph.add_edge("unstructured_agent", "router")
    graph.add_edge("finalize", END)

    return graph.compile()
