"""router 节点：每一步判断"继续查结构化 / 继续查非结构化 / 结束"。

这是整个图里唯一做路由决策的节点——不做成"dispatcher 节点 + summarizer 节点"两段式，
分派下一步和判断信息是否齐全是同一个决策动作的反复发生，用这一个节点 + 循环边实现。

next_step 走强制结构化输出（Pydantic + with_structured_output），不解析自由文本，
避免路由结果解析出错导致状态机行为不可预测。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.config import settings
from src.clients.llm import get_llm
from src.graph.state import AgentState, NextStep, recent_history_text
from prompts.loader import render_prompt

_SYSTEM_PROMPT = render_prompt("router")


class RouterDecision(BaseModel):
    next_step: NextStep = Field(description="下一步动作: structured | unstructured | finalize")
    reason: str = Field(description="做出该判断的简要理由，控制在一句话以内")


# 底层 LLM 在 reason 字段较长时，偶尔会在强制 tool-calling 格式里生成格式非法的输出
# （多余的右括号导致 JSON 解析失败），这是模型生成质量问题，不是我们代码的 bug。
# 用小重试兜底；重试全部失败就安全降级为 finalize，而不是让整个请求崩溃——
# 这跟 loop_count 超限时的兜底是同一种"宁可提前结束，也不让状态机行为失控"的原则。
_MAX_DECISION_ATTEMPTS = 3


def router_node(state: AgentState) -> AgentState:
    loop_count = state.get("loop_count", 0)

    if loop_count >= settings.max_router_loops:
        reason = f"已达到路由循环上限 ({settings.max_router_loops})，强制结束。"
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
        except Exception as exc:  # noqa: BLE001 - 底层模型格式错误的类型不固定，广接更稳妥
            last_error = exc

    if decision is None:
        reason = f"路由模型连续 {_MAX_DECISION_ATTEMPTS} 次输出格式错误，安全降级为 finalize: {last_error}"
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
    parts.append(f"用户问题: {state.get('user_query', '')}")
    if state.get("credit_info"):
        parts.append(f"已检索到的政策/规则原文: {state['credit_info']}")
    if state.get("business_rule_result"):
        parts.append(f"已计算的业务规则结果: {state['business_rule_result']}")
    if state.get("structured_result"):
        parts.append(f"已查询到的结构化数据结果: {state['structured_result']}")
    parts.append(f"当前循环次数: {state.get('loop_count', 0)} / 上限 {settings.max_router_loops}")
    return "\n".join(parts)


def route_after_router(state: AgentState) -> NextStep:
    return state.get("next_step", "finalize")
