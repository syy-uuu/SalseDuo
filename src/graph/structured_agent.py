"""structured_agent 节点：调用 Genie Space 查询 AdventureWorksLT 结构化数据，
必要时 Genie 会调用挂载的 UC Function（calculate_credit_terms /
check_large_transaction_compliance）完成业务规则计算。

Genie 有状态多轮对话：复用 state 里的 genie_conversation_id，不重新开会话。
"""

from __future__ import annotations

from src.graph.state import AgentState
from src.clients.genie_client import ask_genie

_BUSINESS_RULE_FUNCTIONS = ["calculate_credit_terms", "check_large_transaction_compliance"]


def _build_question(state: AgentState) -> str:
    parts = [state.get("user_query", "")]
    if state.get("credit_info"):
        parts.append(
            "补充背景（来自公司政策文档，供计算业务规则时参考）:\n" + state["credit_info"]
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
