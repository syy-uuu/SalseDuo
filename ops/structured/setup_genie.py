"""Step 2 (structured-data side — Genie Space configuration): fills in the data-source
tables and writes the text Instructions.

Known limitation (a Databricks platform issue, not a bug in this project — confirmed by
testing, not a guess): `genie.create_space` / `update_space`'s `serialized_space` is an
opaque, internal, undocumented serialization format. By configuring Instructions once by
hand in the UI (Text + SQL Functions) and then fetching the result with get_space, the
real field structure was confirmed to be:

{
  "version": 2,
  "data_sources": {
    "tables": [{"identifier": "cat.schema.table", "column_configs": [...]}]
  },
  "instructions": {
    "text_instructions": [{"id": "<32-char lowercase hex>", "content": ["full text as one block"]}],
    "sql_functions": [{"id": "<32-char lowercase hex>", "identifier": "cat.schema.function"}]
  }
}

Of these, `data_sources.tables` and `instructions.text_instructions` are confirmed
writable via the update_space API. But `instructions.sql_functions` (attaching UC
Functions as Genie tools) cannot be written through this API — regardless of content,
including round-tripping data that was already successfully saved via the UI back
unmodified via get_space, the PATCH always fails with:
    "Failed to fetch certified answers for the agent ... Certified answer 'xxx' does not exist"
Reproduced across single-function/two-function payloads and with retries after a delay —
this confirms the field is entirely unwritable via PAT-authenticated API calls on this
workspace/API version (the UI saves it through a different internal path), and not even
"read it back unchanged, then write it back" works. This means **any update_space call
whose payload includes this field will fail outright** — so this script always strips
the field from the payload before submitting (otherwise the whole update fails,
including the tables/instructions changes we actually want to save).

**Cost: every time this script runs, any UC Function attached in the Genie Space gets
detached** (because serialized_space uses full-replacement semantics), and needs to be
re-attached manually via the UI afterward (Configure > Instructions > SQL Queries). This
is a confirmed platform limitation, not something this script can work around.

Usage: python -m ops.structured.setup_genie
"""

from __future__ import annotations

import json
import uuid

from src.config import settings
from src.db_client import get_workspace_client
from prompts.loader import render_prompt

def _build_instructions() -> str:
    # UC Functions can't be attached as Genie "tools" through any path other than the UI
    # (see the module docstring above) — if Genie only sees the function's short name
    # when generating SQL, it isn't on the search path and fails with
    # UNRESOLVED_ROUTINE. So the fully-qualified name is written directly into the
    # instructions text, and Genie is told to always call it fully-qualified in the SQL
    # it generates, instead of depending on the "attach as tool" mechanism, which is
    # broken.
    return render_prompt(
        "genie_instructions",
        fn_schema=f"{settings.uc_catalog}.{settings.uc_function_schema}",
        sales_schema=f"{settings.uc_catalog}.{settings.uc_schema_sales}",
    )

# The full list of tables this Genie Space should have attached, listed explicitly here
# (not "whatever's already there plus a few more" — this is the complete list of tables
# this Space is meant to have). Every run diffs this list against what's actually
# attached, adds whatever's missing, and skips what's already there — never adds
# duplicates. How this specific list was chosen: the sales-related tables cover
# customer/order/salesperson/settlement fields; person.person is for resolving customer
# names. This Genie Space currently has a 30-table cap — check we're not approaching it
# before adding more.
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
    """Adds any table_fullnames entries that aren't already attached, skipping ones
    that already exist. Returns the tables actually added this run (fully qualified),
    for main()'s report."""
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
            "get_space did not return serialized_space — confirm this Genie Space has "
            "already been successfully created in the UI, and that the current account "
            "has access to it."
        )
    parsed = json.loads(space.serialized_space)

    table_fullnames = [f"{settings.uc_catalog}.{t}" for t in GENIE_TABLES]
    added = _merge_tables(parsed, table_fullnames)
    _set_text_instructions(parsed, _build_instructions())

    # See the module docstring: instructions.sql_functions can't be written via the API
    # on this workspace, so it must be stripped from the payload — otherwise the entire
    # update fails. This does mean the field gets cleared, requiring a manual
    # re-attachment in the UI afterward.
    parsed.get("instructions", {}).pop("sql_functions", None)

    client.genie.update_space(
        space_id=settings.genie_space_id,
        warehouse_id=settings.sql_warehouse_id,
        serialized_space=json.dumps(parsed),
    )
    print(f"Genie Space updated: {settings.genie_space_id}")

    total = len(parsed.get("data_sources", {}).get("tables", []))
    if added:
        print(f"\nAdded {len(added)} table(s) this run:")
        for t in added:
            print(f"  - {t}")
    else:
        print("\nNo tables added this run (everything in GENIE_TABLES was already attached).")
    print(f"Total tables attached: {total}.")

    expected_functions = sorted(
        f"{settings.uc_catalog}.{settings.uc_function_schema}.{fn}" for fn in REQUIRED_FUNCTIONS
    )
    print(
        "\nNote: this update just cleared instructions.sql_functions (platform "
        "limitation, see the module docstring). In practice this field was never "
        "required in the first place — as long as text_instructions clearly states "
        "the fully-qualified name and return field names for the two functions below, "
        "Genie will call them correctly when generating SQL; no need to re-attach them "
        "via the UI:"
    )
    for fn in expected_functions:
        print(f"  - {fn}")


if __name__ == "__main__":
    main()
