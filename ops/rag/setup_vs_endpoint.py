"""Vector Search endpoint provisioning: ensures VECTOR_SEARCH_ENDPOINT exists, creating
it if it doesn't.

Split out of ingest_docs.py because the endpoint is long-lived shared infrastructure
(one endpoint can host multiple indexes), while an index is an asset bound one-to-one to
a specific source table — the two have different lifecycles, so splitting them into two
scripts keeps responsibilities clearer. Run order: run this script first to create the
endpoint, then run ingest_docs.py to build the index.

Uses databricks-sdk's own `client.vector_search_endpoints`, not the
`databricks-ai-search` package — the latter's auth only accepts a static
PAT/Service Principal token, doesn't support the dynamic Azure CLI (`az login`) token
auth this project uses, and fails immediately at client-construction time.

The first time a Vector Search endpoint is created in a given workspace it can take
anywhere from ten to several tens of minutes (mostly stuck in the PROVISIONING_ENDPOINT
phase, not slow index syncing) — this is normal, not a sign that it's hung.

Usage: python -m ops.rag.setup_vs_endpoint
"""

from __future__ import annotations

from databricks.sdk.service.vectorsearch import EndpointType

from src.config import settings
from src.db_client import get_workspace_client


def ensure_endpoint_exists() -> None:
    client = get_workspace_client()
    endpoints = {e.name for e in client.vector_search_endpoints.list_endpoints()}
    if settings.vector_search_endpoint in endpoints:
        print(f"Vector Search endpoint already exists: {settings.vector_search_endpoint}")
        return
    client.vector_search_endpoints.create_endpoint(
        name=settings.vector_search_endpoint, endpoint_type=EndpointType.STANDARD
    )
    print(f"Created Vector Search endpoint: {settings.vector_search_endpoint} (provisioning in the background, may take ten to several tens of minutes)")


def main() -> None:
    settings.require("vector_search_endpoint")
    ensure_endpoint_exists()


if __name__ == "__main__":
    main()
