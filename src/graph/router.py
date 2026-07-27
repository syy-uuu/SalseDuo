"""router 节点：每一步判断"继续查结构化 / 继续查非结构化 / 结束"。

这是整个图里唯一做路由决策的节点——不做成"dispatcher 节点 + summarizer 节点"两段式，
分派下一步和判断信息是否齐全是同一个决策动作的反复发生，用这一个节点 + 循环边实现。

next_step 走强制结构化输出（Pydantic + with_structured_output），不解析自由文本，
避免路由结果解析出错导致状态机行为不可预测。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.config import settings
from src.graph.llm import get_llm
from src.graph.state import AgentState, NextStep

_SYSTEM_PROMPT = """\
你是一个负责路由决策的调度器。任务是判断:要回答用户的问题,当前掌握的信息是否已经足够,
如果不够,下一步应该查询"非结构化文档"(公司信用/合规政策规则)还是"结构化数据库"
(AdventureWorksLT 销售/客户数据,以及信用/合规业务规则计算的 UC Function)。

判断原则:
- 如果问题涉及信用额度、账期、大额交易结算合规等规则,但还没有检索过政策文档,先查非结构化。
- 如果已经拿到政策规则/参数,但还需要具体客户的交易数据、订单历史、金额等,查结构化数据
  (Genie 会在需要时调用挂载的 UC Function 完成规则计算)。
- 如果规则计算结果依赖的某个参数其实来自另一份政策文档(例如先算完信用条款,又发现需要
  确认结算方式合规性),可以再次回到非结构化查询。
- 如果已经有了回答用户问题所需的全部信息,选择 finalize。
- 不要无意义地重复相同的查询。
"""


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
    parts = [f"用户问题: {state.get('user_query', '')}"]
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
