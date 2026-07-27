"""给 Databricks App 的 service principal 授予访问结构化数据（Genie 底层表）所需的
Unity Catalog 权限。

背景：Serving Endpoint 侧的 structured_agent 调用 Genie 时报
`PERMISSION_DENIED: ... No access to 'adventureworks_dataagent.sales.customer' ...`
（完整现象见 docs/DEVELOPMENT_JOURNAL.md 案例 11、docs/VERIFICATION_2026-07-27.md
Step 7e）。这是 Databricks Apps 部署后自动生成的 service principal 还没有被授予底层表
权限导致的——App 本身有独立的 service principal（`client.apps.get(app_name).
service_principal_client_id`），Genie 生成 SQL 后实际执行查询时，走的是调用方的权限，
调用方如果没有 SELECT/USE CATALOG/USE SCHEMA，Genie 会拒绝。

**要授权哪些表/函数，直接从 `ops/structured/setup_genie.py` 的 `GENIE_TABLES`/
`REQUIRED_FUNCTIONS` 导入，不在这个文件里维护第二份清单**——这两个脚本一个负责"Genie
Space 里挂哪些表"，一个负责"给谁授权能查这些表"，如果各自维护一份清单，改的时候很容易
漏改一边（比如 `setup_genie.py` 加了张新表，这边忘了同步加 GRANT，线上会报权限错误但
本地看代码看不出哪里漏了）。现在的关系是：`setup_genie.py` 改了 `GENIE_TABLES`，这个
脚本自动跟着变，不需要手动同步。

**授权粒度是逐表 SELECT（不是 schema 级），逐函数 EXECUTE（不是 schema 级）**——比之前
版本（直接 `GRANT SELECT ON SCHEMA sales`）更精确，只授权 Genie 真正会用到的那些表/
函数，不会因为 schema 级授权而意外多给了 `GENIE_TABLES` 里没列出的表的访问权限。
`USE CATALOG`/`USE SCHEMA` 这两类是访问路径必需的前提条件，仍然按 schema 级授予（这两
类本身不暴露数据，只是"看得到这个目录/schema 存在"，没必要也做成逐表）。

这个脚本只授权，不撤销、不重新赋权其他身份——如果之后发现权限还不够（比如实际生效的
身份不是 App 的 service principal，而是 Serving Endpoint 自己的运行时身份，目前没有
官方 API 能直接查到这个身份是什么），需要另外排查，不在这个脚本的范围内。

用法: python -m ops.grant_app_permissions
"""

from __future__ import annotations

from src.config import settings
from src.db_client import get_workspace_client
from ops.sql_utils import run_statement
from ops.structured.setup_genie import GENIE_TABLES, REQUIRED_FUNCTIONS


def _grant_statements(principal: str) -> list[str]:
    statements = [f"GRANT USE CATALOG ON CATALOG {settings.uc_catalog} TO `{principal}`"]

    # GENIE_TABLES 里的表分属不同 schema（目前是 sales/person），USE SCHEMA 按用到的
    # schema 去重后各授一次，SELECT 逐表授予。
    schemas = sorted({table.split(".", 1)[0] for table in GENIE_TABLES})
    for schema in schemas:
        statements.append(
            f"GRANT USE SCHEMA ON SCHEMA {settings.uc_catalog}.{schema} TO `{principal}`"
        )
    for table in GENIE_TABLES:
        statements.append(
            f"GRANT SELECT ON TABLE {settings.uc_catalog}.{table} TO `{principal}`"
        )

    # Genie 生成的 SQL 会调用业务规则函数，需要 EXECUTE（不是 SELECT），逐函数授予。
    statements.append(
        f"GRANT USE SCHEMA ON SCHEMA {settings.uc_catalog}.{settings.uc_function_schema} TO `{principal}`"
    )
    for fn in REQUIRED_FUNCTIONS:
        statements.append(
            f"GRANT EXECUTE ON FUNCTION {settings.uc_catalog}.{settings.uc_function_schema}.{fn} TO `{principal}`"
        )

    return statements


def main() -> None:
    settings.require("sql_warehouse_id", "databricks_app_name", "uc_catalog")
    client = get_workspace_client()

    app = client.apps.get(settings.databricks_app_name)
    principal = app.service_principal_client_id
    if not principal:
        raise RuntimeError(
            f"App {settings.databricks_app_name} 没有查到 service_principal_client_id，"
            "确认 App 是否已经成功创建过（首次部署完成后才会生成 service principal）。"
        )
    print(f"App service principal: {app.service_principal_name} ({principal})")
    print(f"本次授权覆盖 {len(GENIE_TABLES)} 张表 + {len(REQUIRED_FUNCTIONS)} 个函数"
          f"（来源: ops/structured/setup_genie.py 的 GENIE_TABLES/REQUIRED_FUNCTIONS）\n")

    for stmt in _grant_statements(principal):
        run_statement(client, stmt)
        print(f"已执行: {stmt}")

    print("\n授权完成。")


if __name__ == "__main__":
    main()
