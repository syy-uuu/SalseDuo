"""验证 docs/CODE_REVIEW_FINDINGS.md 第 3 条修复：unstructured_agent 在多跳场景里，
第二次检索的 query 要能带上 router_reason/structured_result，不能跟第一次一模一样。

纯离线单测，不连 Databricks——retrieve() 整个 mock 掉，只关心传给它的 query 参数对不对，
不测检索质量本身（检索质量的验证方式见 docs/VERIFICATION_2026-07-27.md Step 3.7，
需要真实连 Vector Search）。
"""

from __future__ import annotations

from unittest import mock

from src.graph.unstructured_agent import _TOP_K, _build_query, unstructured_agent_node


def test_build_query_only_user_query_when_no_extra_context():
    """第一次进这个节点时，state 里通常还没有 router_reason/structured_result，
    query 应该就是原始问题，不应该凭空多出别的内容。"""
    state = {"user_query": "Tier 2 客户的信用额度上限是多少？"}
    assert _build_query(state) == "Tier 2 客户的信用额度上限是多少？"


def test_build_query_includes_router_reason_and_structured_result():
    state = {
        "user_query": "Tier 2 客户的信用额度上限是多少？",
        "router_reason": "已查到客户采购额，还需要确认结算方式合规性",
        "structured_result": "客户 Bike World 年采购额 50万美元",
    }
    query = _build_query(state)
    assert "Tier 2 客户的信用额度上限是多少？" in query
    assert "已查到客户采购额，还需要确认结算方式合规性" in query
    assert "客户 Bike World 年采购额 50万美元" in query


def test_second_hop_query_differs_from_first_hop():
    """模拟多跳场景：第一次进 unstructured_agent 时 state 里还没有 router_reason/
    structured_result；第二次进来时 router 已经判断过一次、structured_agent 也跑过
    一次，这时候 query 应该跟第一次不一样——这是第 3 条修复要解决的核心问题（改之前
    两次的 query 永远相同，大概率检索到同一批 chunk）。"""
    first_state = {"user_query": "客户 Bike World 的信用额度上限是多少？", "loop_count": 0}
    second_state = {
        **first_state,
        "loop_count": 2,
        "router_reason": "已查到政策规则，还需要客户的具体采购数据才能算出额度",
        "structured_result": "Bike World 年采购额 50万美元，合作 3 年",
    }
    assert _build_query(first_state) != _build_query(second_state)


def test_unstructured_agent_node_passes_built_query_to_retrieve():
    """确认节点真的在用 _build_query() 的结果去检索，而不是仍然直接传 user_query
    （光有 _build_query() 函数、节点忘了切过去用就白修了）。"""
    state = {"user_query": "问题", "router_reason": "补充理由", "loop_count": 1}
    with mock.patch(
        "src.graph.unstructured_agent.retrieve", return_value=[]
    ) as mocked_retrieve:
        unstructured_agent_node(state)

    mocked_retrieve.assert_called_once()
    called_query = mocked_retrieve.call_args.args[0]
    called_k = mocked_retrieve.call_args.kwargs.get("k")
    assert called_query == _build_query(state)
    assert called_k == _TOP_K
