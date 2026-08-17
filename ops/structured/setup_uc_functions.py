"""Step 2 (structured-data side — business rules):
1. Ensures UC_FUNCTION_SCHEMA exists (creates it if not).
2. Creates/replaces two UC SQL Functions, whose rules come from the two policy
   documents under documents_generated/:
   - calculate_credit_terms: credit limit and payment term calculation (AW-FIN-POL-003)
   - check_large_transaction_compliance: large-transaction settlement compliance check
     (AW-COMP-REG-014)

Both are implemented as SQL Functions (not Python UC Functions): the Genie Space
invokes UC Function tools through its bound SQL Warehouse, and a SQL Function doesn't
depend on serverless generic compute, sidestepping the permission risk noted as item 1
in CLAUDE.md section 7.

Usage: python -m ops.structured.setup_uc_functions
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
        print(f"Schema already exists: {settings.uc_catalog}.{settings.uc_function_schema}")
        return
    client.schemas.create(name=settings.uc_function_schema, catalog_name=settings.uc_catalog)
    print(f"Created schema: {settings.uc_catalog}.{settings.uc_function_schema}")


def create_functions(client) -> None:
    for sql_file in sorted(_SQL_DIR.glob("*.sql")):
        template = sql_file.read_text()
        statement = template.format(catalog=settings.uc_catalog, schema=settings.uc_function_schema)
        run_statement(client, statement)
        print(f"Created/replaced function: {sql_file.stem}")


def main() -> None:
    settings.require("uc_catalog", "uc_function_schema", "sql_warehouse_id")
    client = get_workspace_client()
    ensure_schema_exists(client)
    create_functions(client)
    print("Step 2 UC Function provisioning complete.")


if __name__ == "__main__":
    main()
