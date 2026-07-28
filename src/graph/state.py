"""LangGraph StateGraph 的共享状态定义。"""

from __future__ import annotations

import operator
from typing import Literal, TypedDict
from typing_extensions import Annotated

from prompts.loader import render_prompt

NextStep = Literal["structured", "unstructured", "finalize"]


class TraceStep(TypedDict, total=False):
    """白盒追踪用的单步记录。每个节点执行完追加一条，不覆盖之前的记录——
    这样多跳场景（同一种节点被访问多次）也能看到每一跳各自的中间结果，
    而不只是最后一次覆盖后的状态。"""

    step: str  # "router" | "structured_agent" | "unstructured_agent" | "finalize"
    loop_index: int
    reasoning: str | None  # router 的判断理由，或该节点这一步在做什么的简短说明
    question_sent: str | None  # 发给 Genie 的问题原文（仅 structured_agent）
    sql_queries: list[str] | None  # Genie 实际生成并执行的 SQL（仅 structured_agent）
    retrieved_chunks: list[dict] | None  # 检索到的文档片段（仅 unstructured_agent）
    output_summary: str | None  # 该步产出的文本结果
    error: str | None  # 该步失败时的详细错误信息（如 Genie SQL 执行失败的原因）


class AgentState(TypedDict, total=False):
    messages: list[dict]  # 完整对话历史，[{"role": ..., "content": ...}, ...]
    user_query: str

    credit_info: str | None  # 非结构化检索得到的、与本次问题相关的政策/规则原文摘要
    business_rule_result: dict | None  # structured_agent 命中业务规则 UC Function 时的结构化结果
    structured_result: str | None  # 最近一次 Genie 结构化查询的文本答案

    genie_conversation_id: str | None

    loop_count: int
    next_step: NextStep | None
    router_reason: str | None

    # Annotated + operator.add：每个节点返回只包含"自己这一步"的单元素列表，
    # LangGraph 会自动累加合并，不需要每个节点手动读出历史再拼接。
    trace: Annotated[list[TraceStep], operator.add]


# router/finalize 都要把"之前聊过什么"纳入 LLM 上下文，逻辑抽成共享函数放这里，避免
# 两个节点各写一份、容易改一处漏一处。
_MAX_HISTORY_MESSAGES = 10  # 最近 5 轮问答（不含当前这一句），控制 prompt 不无限变长
# 这个数字本身没有严格推导过，是个合理默认值，不是调出来的最优值——见
# docs/CODE_REVIEW_FINDINGS.md 第 10 条：跟 Genie 自己的会话记忆窗口（多大、能记多久
# 完全不透明，看不到也控制不了）不是同一个尺度，两层记忆"能记多远"不一致的风险依然存在，
# 从 3 轮调到 5 轮只是缓解，不是解决。


def recent_history_text(state: AgentState) -> str:
    """把 messages 里"当前这句之前"的历史，格式化成一段可以直接拼进 prompt 的文本。
    没有历史（第一轮提问）时返回空字符串。

    约定：调用方（agent.py::_run_graph / chat.py）负责把当前这句用户提问作为
    messages 的最后一条再传进 initial_state，这里固定只取 messages[:-1]，不用自己判断
    "哪条是当前这句"。"""
    messages = state.get("messages") or []
    history = messages[:-1][-_MAX_HISTORY_MESSAGES:]
    if not history:
        return ""
    lines = [f"{m['role']}: {m['content']}" for m in history]
    return render_prompt("history_framing") + "\n" + "\n".join(lines)
