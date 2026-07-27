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

def _build_instructions() -> str:
    # UC Function 无法通过 UI 之外的方式挂载为 Genie "工具"（见模块顶部说明），
    # Genie 生成 SQL 时如果只写函数短名会因为不在 search path 里而报
    # UNRESOLVED_ROUTINE。这里直接把全限定名写进 instructions文本，让 Genie
    # 在生成的 SQL 里总是用全限定名调用，不依赖"挂载为工具"这个失效的机制。
    fn_schema = f"{settings.uc_catalog}.{settings.uc_function_schema}"
    sales_schema = f"{settings.uc_catalog}.{settings.uc_schema_sales}"
    return f"""\
你可以查询 AdventureWorksLT 的销售(sales)与客户(person)相关表来回答问题。

重要的表关联路径（不要跳过 customer 表直接把 store 和 salesorderheader 关联起来，
这是一个常见的关联错误，会导致查出空结果或算错采购额/合作年限）：
{sales_schema}.store.businessentityid = {sales_schema}.customer.storeid
{sales_schema}.customer.customerid = {sales_schema}.salesorderheader.customerid
也就是说，从店铺名找订单必须经过 customer 表做中间关联，正确路径是：
store -> customer (ON customer.storeid = store.businessentityid) -> salesorderheader
(ON salesorderheader.customerid = customer.customerid)。

当问题涉及信用额度、账期、大额交易结算合规等业务规则计算时，
不要自己拼 SQL 硬算规则，而是在生成的 SQL 里调用以下 UC Function
（必须使用完整的三段式全限定名，不要只写函数名，否则会报 UNRESOLVED_ROUTINE；
两个函数都返回一个 STRUCT，字段名必须严格按下面列出的拼写，不要猜测或改写字段名）：

- {fn_schema}.calculate_credit_terms(relationship_years DOUBLE, annual_purchase_volume_usd DOUBLE, board_approved_strategic_partner BOOLEAN DEFAULT FALSE, requested_credit_amount_usd DOUBLE DEFAULT 0, requested_term_days INT DEFAULT NULL)
  用途：计算客户信用分级、账期、信用额度上限及超限审批要求。
  这是标量函数（SCALAR，不是表函数/TABLE FUNCTION），只能出现在 SELECT 的列表达式里
  （比如 SELECT {fn_schema}.calculate_credit_terms(...).tier），不能写在 FROM/LATERAL
  子句里当表来用，否则会报 NOT_A_TABLE_FUNCTION。
  返回 STRUCT 字段：tier, advance_payment_min_pct, advance_payment_max_pct, max_credit_term_days, max_credit_limit_usd, exceeds_credit_limit, overage_pct, requires_net90_escalation, required_approval

- {fn_schema}.check_large_transaction_compliance(settlement_method STRING, transaction_amount_usd DOUBLE, settlement_currency STRING DEFAULT 'USD', contract_duration_years DOUBLE DEFAULT 0)
  用途：校验大额交易结算方式的合规状态与外汇对冲要求。settlement_method 取值只能是 WIRE_TRANSFER / LETTER_OF_CREDIT / BANK_ACCEPTANCE_DRAFT / CORPORATE_CHEQUE 之一。
  返回 STRUCT 字段：compliance_status, settlement_allowed, required_controls, currency_approved, fx_hedging_clause_required

写 SQL 时间函数注意，这两个函数的时间单位写法不一样，不要混用：
- DATEDIFF 的时间单位要用不加引号的关键字：DATEDIFF(YEAR, start, end)
- DATE_TRUNC 的时间单位要用加引号的字符串：DATE_TRUNC('YEAR', some_date)
"""

# sales schema 的 19 张表在这个 Genie Space 里已经预先全量挂上了（不是本脚本挂的）。
# Genie Space 最多挂 30 张表，所以这里不整体挂 person schema，只按实际需要（客户姓名解析）
# 补一张 person.person，避免逼近上限。
_EXTRA_TABLES = ["person.person"]

REQUIRED_FUNCTIONS = ["calculate_credit_terms", "check_large_transaction_compliance"]


def _merge_tables(parsed: dict, table_fullnames: list[str]) -> None:
    data_sources = parsed.setdefault("data_sources", {})
    existing_tables = data_sources.setdefault("tables", [])
    existing_identifiers = {t["identifier"] for t in existing_tables}
    for fullname in table_fullnames:
        if fullname not in existing_identifiers:
            existing_tables.append({"identifier": fullname})
    existing_tables.sort(key=lambda t: t["identifier"])


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
    print("更新前 tables 数量:", len(parsed.get("data_sources", {}).get("tables", [])))

    table_fullnames = [f"{settings.uc_catalog}.{t}" for t in _EXTRA_TABLES]
    _merge_tables(parsed, table_fullnames)
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
    print("更新后 tables 数量:", len(parsed.get("data_sources", {}).get("tables", [])))

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
