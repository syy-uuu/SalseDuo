"""finalize 节点：综合所有中间结果，生成最终回答。"""

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
    context_parts.append(f"用户问题: {state.get('user_query', '')}")
    if state.get("credit_info"):
        context_parts.append(f"政策文档检索结果: {state['credit_info']}")
    if state.get("business_rule_result"):
        context_parts.append(f"业务规则计算结果: {state['business_rule_result']}")
    if state.get("structured_result"):
        context_parts.append(f"结构化数据查询结果: {state['structured_result']}")
    if hit_loop_limit:
        context_parts.append(
            f"注意: 路由已达到循环上限 ({settings.max_router_loops})，"
            "以下回答基于已收集到的信息，可能不完整。"
        )

    llm = get_llm()
    response = llm.invoke(
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(context_parts)},
        ]
    )
    final_text = response.content
    if hit_loop_limit and "可能不完整" not in final_text:
        final_text += "\n\n（提示：本次回答在达到路由循环上限后强制生成，信息可能不完整。）"

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
