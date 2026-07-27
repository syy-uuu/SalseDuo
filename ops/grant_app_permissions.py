"""给 Databricks App 的 service principal 授予访问结构化数据（Genie 底层表）所需的
Unity Catalog 权限。

背景：Serving Endpoint 侧的 structured_agent 调用 Genie 时报
`PERMISSION_DENIED: ... No access to 'adventureworks_dataagent.sales.customer' ...`
（完整现象见 docs/DEVELOPMENT_JOURNAL.md 案例 11、docs/VERIFICATION_2026-07-27.md
Step 7e）。这是 Databricks Apps 部署后自动生成的 service principal 还没有被授予底层表
权限导致的——App 本身有独立的 service principal（`client.apps.get(app_name).
service_principal_client_id`），Genie 生成 SQL 后实际执行查询时，走的是调用方的权限，
调用方如果没有 SELECT/USE CATALOG/USE SCHEMA，Genie 会拒绝。

授权范围只覆盖 `sales`/`person` 两个 schema（不是整个 catalog），因为 Genie Space 实际
挂载的表就只有这两个 schema 下的（19 张 sales 表 + person.person，见
ops/structured/setup_genie.py），`humanresources`/`purchasing` 从没被挂进 Genie，
`production`（虽然 config.py 里留了 UC_SCHEMA_PRODUCTION 这个字段）目前也没被 Genie
实际用到，不在这次授权范围内——如果以后往 Genie 里加了新 schema 的表，要记得同步在这里
加一组 GRANT。`sales`/`person` 内部用 schema 级授权（不是逐张表 GRANT），会级联到里面
所有表，不需要逐张列举维护。

除了查表需要的 SELECT，Genie 生成的 SQL 里还会调用 `salesduo_agent_tools` schema 下的
两个 UC Function（`calculate_credit_terms`/`check_large_transaction_compliance`）——
调用函数需要的是 **EXECUTE** 权限，跟查表的 SELECT 是两种不同的权限，这里一并授予，
不然就算表权限对了，走到调用信用规则函数那一步还是会报另一个权限错误。

这个脚本只授权，不撤销、不重新赋权其他身份——如果之后发现权限还不够（比如实际生效的
身份不是 App 的 service principal，而是 Serving Endpoint 自己的运行时身份，目前没有
官方 API 能直接查到这个身份是什么），需要另外排查，不在这个脚本的范围内。

用法: python -m ops.grant_app_permissions
"""

from __future__ import annotations

from src.config import settings
from src.db_client import get_workspace_client
from ops.sql_utils import run_statement


def _grant_statements(principal: str) -> list[str]:
    return [
        f"GRANT USE CATALOG ON CATALOG {settings.uc_catalog} TO `{principal}`",
        f"GRANT USE SCHEMA ON SCHEMA {settings.uc_catalog}.{settings.uc_schema_sales} TO `{principal}`",
        f"GRANT SELECT ON SCHEMA {settings.uc_catalog}.{settings.uc_schema_sales} TO `{principal}`",
        f"GRANT USE SCHEMA ON SCHEMA {settings.uc_catalog}.{settings.uc_schema_person} TO `{principal}`",
        f"GRANT SELECT ON SCHEMA {settings.uc_catalog}.{settings.uc_schema_person} TO `{principal}`",
        # Genie 生成的 SQL 会调用这两个业务规则函数，需要 EXECUTE（不是 SELECT）
        f"GRANT USE SCHEMA ON SCHEMA {settings.uc_catalog}.{settings.uc_function_schema} TO `{principal}`",
        f"GRANT EXECUTE ON SCHEMA {settings.uc_catalog}.{settings.uc_function_schema} TO `{principal}`",
    ]


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

    for stmt in _grant_statements(principal):
        run_statement(client, stmt)
        print(f"已执行: {stmt}")

    print("授权完成。")


if __name__ == "__main__":
    main()
