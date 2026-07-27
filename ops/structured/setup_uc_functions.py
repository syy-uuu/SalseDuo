"""Step 2（结构化数据侧 - 业务规则）：
1. 确保 UC_FUNCTION_SCHEMA 存在（不存在则创建）。
2. 创建/替换两个 UC SQL Function，规则来自 documents_generated/ 下两份政策文档：
   - calculate_credit_terms: 信用额度与付款条款计算（AW-FIN-POL-003）
   - check_large_transaction_compliance: 大额交易结算合规校验（AW-COMP-REG-014）

两个函数都实现为 SQL Function（而非 Python UC Function）：Genie Space 对 UC Function
工具的调用是通过其绑定的 SQL Warehouse 执行的，SQL Function 不依赖 serverless generic
compute，可以规避 CLAUDE.md 第7节风险点1 提到的权限问题。

用法: python -m ops.structured.setup_uc_functions
"""

from __future__ import annotations

from pathlib import Path

from src.config import settings
from src.db_client import get_workspace_client
from ops.sql_utils import run_statement

_SQL_DIR = Path(__file__).parent / "sql"


def ensure_schema_exists(client) -> None:
    schemas = {s.name for s in client.schemas.list(catalog_name=settings.uc_catalog)}
    if settings.uc_function_schema in schemas:
        print(f"schema 已存在: {settings.uc_catalog}.{settings.uc_function_schema}")
        return
    client.schemas.create(name=settings.uc_function_schema, catalog_name=settings.uc_catalog)
    print(f"已创建 schema: {settings.uc_catalog}.{settings.uc_function_schema}")


def create_functions(client) -> None:
    for sql_file in sorted(_SQL_DIR.glob("*.sql")):
        template = sql_file.read_text()
        statement = template.format(catalog=settings.uc_catalog, schema=settings.uc_function_schema)
        run_statement(client, statement)
        print(f"已创建/替换函数: {sql_file.stem}")


def main() -> None:
    settings.require("uc_catalog", "uc_function_schema", "sql_warehouse_id")
    client = get_workspace_client()
    ensure_schema_exists(client)
    create_functions(client)
    print("Step 2 UC Function 建仓完成。")


if __name__ == "__main__":
    main()
