"""Step 6 要求的三类端到端测试用例：
1. 只需结构化数据的问题
2. 只需非结构化数据的问题
3. 多跳问题（非结构化 -> 计算 -> 结构化 -> 可能再非结构化）

这些用例需要真实连通的 Databricks workspace（Genie Space、Vector Search Index、
LLM serving endpoint 都已建好），在 .env 未配置真实凭据前会被跳过——一旦补上凭据，
直接 `pytest tests/test_integration_cases.py -v` 即可跑通，不需要改代码。
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
    reason="需要真实 Databricks 凭据 + 已建好的 GENIE_SPACE_ID / VECTOR_SEARCH_INDEX，暂不满足，跳过端到端测试。",
)


def _run(question: str) -> dict:
    graph = build_graph()
    return graph.invoke(
        {"messages": [{"role": "user", "content": question}], "user_query": question, "loop_count": 0}
    )


def test_structured_only_question():
    result = _run("2013 年销售额最高的前 5 个产品是什么？")
    assert result.get("structured_result")
    assert result["messages"][-1]["role"] == "assistant"


def test_unstructured_only_question():
    result = _run("Tier 2 Preferred Account 客户的账期和信用额度上限是多少？")
    assert result.get("credit_info")
    assert result["messages"][-1]["role"] == "assistant"


def test_multi_hop_credit_then_structured_question():
    result = _run(
        "客户 Bike World 目前的年采购额和合作年限是多少？"
        "按公司信用政策，他们能申请到的最高信用额度和账期是多少，"
        "如果他们申请 100 万美元的信用额度需要谁审批？"
    )
    assert result.get("credit_info")
    assert result.get("structured_result")
    assert result["messages"][-1]["role"] == "assistant"


def test_loop_count_never_exceeds_configured_max():
    result = _run("一个故意模糊、可能导致反复判断信息不够的问题")
    assert result.get("loop_count", 0) <= settings.max_router_loops


def test_multi_turn_memory_reuses_genie_conversation_and_resolves_pronouns():
    """2026-07-28 补的多轮记忆：同一个 genie_conversation_id 要跨轮复用，且第二轮
    里的代词（"他们"）要能借助历史正确指代第一轮问的客户，不需要用户重新报一遍
    客户名。设计方法论见 docs/AGENT_MEMORY_DESIGN.md。"""
    graph = build_graph()

    q1 = "客户 Bike World 的年采购额是多少？"
    result1 = graph.invoke(
        {"messages": [{"role": "user", "content": q1}], "user_query": q1, "loop_count": 0}
    )
    genie_cid_1 = result1.get("genie_conversation_id")
    assert genie_cid_1, "第一轮应该已经产生一个 genie_conversation_id"

    q2 = "那他们的信用额度上限是多少？"
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
        "第二轮应该复用同一个 genie_conversation_id，不应该开新的 Genie 会话"
    )
    # 第二轮问题里没有再提"Bike World"——如果历史没生效，router/finalize 拿到的只是
    # 一句孤立的"那他们的信用额度上限是多少"，大概率会直接说不知道是哪个客户，走不到
    # 结构化查询这一步；这里断言确实产生了结构化查询/业务规则结果，作为"理解了指代"
    # 的间接证据（不断言具体金额，LLM 生成的自然语言措辞不适合做精确字符串匹配）。
    assert result2.get("structured_result") or result2.get("business_rule_result")
