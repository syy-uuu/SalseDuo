"""LangGraph StateGraph 的共享状态定义。"""

from __future__ import annotations

import operator
from typing import Literal, TypedDict
from typing_extensions import Annotated

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
