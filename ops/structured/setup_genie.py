"""Step 2（结构化数据侧 - Genie Space 配置）：补齐数据源表 + 写入文本 Instructions。

已知限制（Databricks 平台侧，非本项目代码问题，已实测确认，不是猜测）：
`genie.create_space` / `update_space` 的 `serialized_space` 是不透明的内部序列化格式，
没有公开文档。通过"在 UI 里手动配置一次 Instructions（Text + SQL Functions）→ 用
get_space 抓取结果"确认了真实字段结构：

{
  "version": 2,
  "data_sources": {
    "tables": [{"identifier": "cat.schema.table", "column_configs": [...]}]
  },
  "instructions": {
    "text_instructions": [{"id": "<32位小写hex>", "content": ["完整文本一整段"]}],
    "sql_functions": [{"id": "<32位小写hex>", "identifier": "cat.schema.function"}]
  }
}

其中 `data_sources.tables` 和 `instructions.text_instructions` 通过 update_space API
实测可写。但 `instructions.sql_functions`（挂载 UC Function 为 Genie 工具）无法通过这条
API 写入——不管内容是什么，包括把已经通过 UI 成功保存、原样从 get_space 读出来再不做任何
修改地写回去，都会在 PATCH 时报错：
    "Failed to fetch certified answers for the agent ... Certified answer 'xxx' does not exist"
换过单函数/双函数/延迟重试都复现同样的错误，说明这个字段在这个 workspace/API 版本下对
PAT 认证的调用完全不可写（UI 保存时走的是另一条内部通路），连"原样读回去再写一次"都不行。
这意味着**任何一次 update_space 调用只要 payload 里带着这个字段就会失败**——所以本脚本
每次运行都会主动把这个字段从 payload 里摘掉再提交（否则整个 update 全部失败，tables/
instructions 的改动也保存不了）。

**代价：每次跑这个脚本，Genie Space 里已挂载的 UC Function 会被摘掉**（因为
serialized_space 是整体替换语义），跑完之后需要重新去 UI 里挂一次（Configure >
Instructions > SQL Queries，选中 calculate_credit_terms / check_large_transaction_
compliance）。这是已确认的平台限制，不是本脚本能绕开的。

用法: python -m ops.structured.setup_genie
"""

from __future__ import annotations

import json
import uuid

from src.config import settings
from src.db_client import get_workspace_client
from prompts.loader import render_prompt

def _build_instructions() -> str:
    # UC Function 无法通过 UI 之外的方式挂载为 Genie "工具"（见模块顶部说明），
    # Genie 生成 SQL 时如果只写函数短名会因为不在 search path 里而报
    # UNRESOLVED_ROUTINE。这里直接把全限定名写进 instructions文本，让 Genie
    # 在生成的 SQL 里总是用全限定名调用，不依赖"挂载为工具"这个失效的机制。
    return render_prompt(
        "genie_instructions",
        fn_schema=f"{settings.uc_catalog}.{settings.uc_function_schema}",
        sales_schema=f"{settings.uc_catalog}.{settings.uc_schema_sales}",
    )

# Genie Space 应该挂载的完整表清单，显式列在这里（不是"默认已有 + 追加几张"，是这个
# Space 应该有的全部表）——每次运行都会跟当前实际挂载的表做差集，缺的补上，已有的跳过，
# 不会重复添加。这个清单本身怎么定的：sales 相关表覆盖客户/订单/销售人员/结算所需的字段，
# person.person 用于客户姓名解析。Genie Space 目前有 30 张表的上限，加表之前先确认没有
# 逼近这个上限。
GENIE_TABLES = [
    "person.person",
    "sales.countryregioncurrency",
    "sales.creditcard",
    "sales.currency",
    "sales.currencyrate",
    "sales.customer",
    "sales.personcreditcard",
    "sales.salesorderdetail",
    "sales.salesorderheader",
    "sales.salesorderheadersalesreason",
    "sales.salesperson",
    "sales.salespersonquotahistory",
    "sales.salesreason",
    "sales.salestaxrate",
    "sales.salesterritory",
    "sales.salesterritoryhistory",
    "sales.shoppingcartitem",
    "sales.specialoffer",
    "sales.specialofferproduct",
    "sales.store",
]

REQUIRED_FUNCTIONS = ["calculate_credit_terms", "check_large_transaction_compliance"]


def _merge_tables(parsed: dict, table_fullnames: list[str]) -> list[str]:
    """把 table_fullnames 里还没挂载的表加进去，已经存在的跳过。返回本次真正新增的表
    （全限定名），供 main() 打印报告用。"""
    data_sources = parsed.setdefault("data_sources", {})
    existing_tables = data_sources.setdefault("tables", [])
    existing_identifiers = {t["identifier"] for t in existing_tables}
    added = [fullname for fullname in table_fullnames if fullname not in existing_identifiers]
    for fullname in added:
        existing_tables.append({"identifier": fullname})
    existing_tables.sort(key=lambda t: t["identifier"])
    return added


def _set_text_instructions(parsed: dict, text: str) -> None:
    instructions = parsed.setdefault("instructions", {})
    instructions["text_instructions"] = [{"id": uuid.uuid4().hex, "content": [text.strip()]}]


def main() -> None:
    settings.require("genie_space_id", "sql_warehouse_id", "uc_function_schema")
    client = get_workspace_client()

    space = client.genie.get_space(settings.genie_space_id, include_serialized_space=True)
    if not space.serialized_space:
        raise RuntimeError(
            "get_space 未返回 serialized_space，请确认该 Genie Space 已在 UI 中创建成功，"
            "且当前账号有权限访问。"
        )
    parsed = json.loads(space.serialized_space)

    table_fullnames = [f"{settings.uc_catalog}.{t}" for t in GENIE_TABLES]
    added = _merge_tables(parsed, table_fullnames)
    _set_text_instructions(parsed, _build_instructions())

    # 见模块顶部说明：instructions.sql_functions 在这个 workspace 下无法通过 API 写入，
    # 必须从 payload 里摘掉，否则整个 update 都会失败——这会导致这个字段被清空，
    # 需要之后在 UI 里重新挂一次。
    parsed.get("instructions", {}).pop("sql_functions", None)

    client.genie.update_space(
        space_id=settings.genie_space_id,
        warehouse_id=settings.sql_warehouse_id,
        serialized_space=json.dumps(parsed),
    )
    print(f"Genie Space 已更新: {settings.genie_space_id}")

    total = len(parsed.get("data_sources", {}).get("tables", []))
    if added:
        print(f"\n本次新增 {len(added)} 张表:")
        for t in added:
            print(f"  - {t}")
    else:
        print("\n本次没有新增表（GENIE_TABLES 里列出的表都已经存在）。")
    print(f"目前共 {total} 张表。")

    expected_functions = sorted(
        f"{settings.uc_catalog}.{settings.uc_function_schema}.{fn}" for fn in REQUIRED_FUNCTIONS
    )
    print(
        "\n提醒: 这次 update 已经把 instructions.sql_functions 清空了（平台限制，见模块顶部说明）。"
        "但实测这个字段本来就不是必须的——只要 text_instructions 里写清楚了下面这两个函数的"
        "全限定名和返回字段名，Genie 生成 SQL 时就能正确调用，不需要去 UI 里重新挂载:"
    )
    for fn in expected_functions:
        print(f"  - {fn}")


if __name__ == "__main__":
    main()
