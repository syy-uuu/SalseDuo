"""Vector Search endpoint 建置：确保 VECTOR_SEARCH_ENDPOINT 存在，不存在则创建。

从 ingest_docs.py 拆出来的原因：endpoint 是长期存在的共享基础设施（一个 endpoint 上可以挂
多个 index），index 才是跟某一份具体数据源表一对一绑定的资产，两者生命周期不一样，拆成两个
脚本职责更清楚。运行顺序：先跑这个脚本建 endpoint，再跑 ingest_docs.py 建索引。

第一次在这个 workspace 里创建 Vector Search endpoint 可能需要十几到几十分钟（主要卡在
PROVISIONING_ENDPOINT 阶段，不是索引同步慢），这是正常现象，不代表卡住了。

用法: python -m ops.rag.setup_vs_endpoint
"""

from __future__ import annotations

from databricks.ai_search.client import VectorSearchClient

from src.config import settings


def ensure_endpoint_exists() -> None:
    vsc = VectorSearchClient(disable_notice=True)
    endpoints = {e["name"] for e in vsc.list_endpoints().get("endpoints", [])}
    if settings.vector_search_endpoint in endpoints:
        print(f"Vector Search endpoint 已存在: {settings.vector_search_endpoint}")
        return
    vsc.create_endpoint(name=settings.vector_search_endpoint, endpoint_type="STANDARD")
    print(f"已创建 Vector Search endpoint: {settings.vector_search_endpoint}")


def main() -> None:
    settings.require("vector_search_endpoint")
    ensure_endpoint_exists()


if __name__ == "__main__":
    main()
