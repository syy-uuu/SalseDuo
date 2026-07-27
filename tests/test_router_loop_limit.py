"""验证 CLAUDE.md 第7节风险点3：router 循环上限必须落地生效。

关键点：一旦 loop_count 达到上限，router_node 必须在调用 LLM 之前就强制返回
next_step="finalize"，这样即使没有真实 Databricks/LLM 连接，这个安全阀也可以在本地
纯离线跑通——不需要连到真实 workspace 就能证明"第 N 次强制走 finalize 且不报错"。
"""

from src.config import settings
from src.graph.router import router_node


def test_router_forces_finalize_when_loop_limit_reached():
    state = {
        "user_query": "一个会让 router 持续判断信息不够的问题",
        "loop_count": settings.max_router_loops,
    }

    result = router_node(state)

    assert result["next_step"] == "finalize"
    assert "循环上限" in result["router_reason"]
    # loop_count 不应该在强制终止分支里继续累加
    assert result["loop_count"] == settings.max_router_loops


def test_router_forces_finalize_when_loop_limit_exceeded():
    state = {"user_query": "任意问题", "loop_count": settings.max_router_loops + 3}
    result = router_node(state)
    assert result["next_step"] == "finalize"
