"""Step 1 验证脚本：确认能通过 Databricks SDK 认证并列出 UC_CATALOG 下的 schema。

用法: python -m src.setup.verify_connection
"""

from __future__ import annotations

from src.config import settings
from src.db_client import get_workspace_client


def main() -> None:
    client = get_workspace_client()
    print(f"目标 catalog: {settings.uc_catalog}")
    schemas = list(client.schemas.list(catalog_name=settings.uc_catalog))
    if not schemas:
        raise RuntimeError(f"catalog {settings.uc_catalog} 下没有找到任何 schema，请检查权限或名称。")
    print(f"共找到 {len(schemas)} 个 schema:")
    for s in schemas:
        print(f"  - {s.name}")


if __name__ == "__main__":
    main()
