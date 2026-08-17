"""Step 1 verification script: confirms Databricks SDK auth works and lists the
schemas under UC_CATALOG.

Usage: python -m ops.verify_connection
"""

from __future__ import annotations

from src.config import settings
from src.db_client import get_workspace_client


def main() -> None:
    client = get_workspace_client()
    print(f"Target catalog: {settings.uc_catalog}")
    schemas = list(client.schemas.list(catalog_name=settings.uc_catalog))
    if not schemas:
        raise RuntimeError(f"No schemas found under catalog {settings.uc_catalog} — check permissions or the catalog name.")
    print(f"Found {len(schemas)} schema(s):")
    for s in schemas:
        print(f"  - {s.name}")


if __name__ == "__main__":
    main()
