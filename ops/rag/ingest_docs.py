"""Unstructured-data-side data pipeline:
1. Upload the raw docx files under documents_generated/ to UC_VOLUME_PATH, preserving
   the originals.
2. Parse + chunk them, writing the result into DELTA_TABLE_DOCS_CHUNKS.
3. Create a Delta Sync Index backed by that Delta table (if it doesn't already exist).

Prerequisite: VECTOR_SEARCH_ENDPOINT must already exist — run
`python -m ops.rag.setup_vs_endpoint` to create the endpoint first, then run this
script; this script does not create the endpoint itself (see the module docstring in
`setup_vs_endpoint.py` — the endpoint's and the index's lifecycles are managed
separately).

Usage: python -m ops.rag.ingest_docs
"""

from __future__ import annotations

from pathlib import Path

from databricks.sdk.service.catalog import VolumeType
from databricks.sdk.service.vectorsearch import (
    DeltaSyncVectorIndexSpecRequest,
    EmbeddingSourceColumn,
    PipelineType,
    VectorIndexType,
)

from src.config import settings
from src.db_client import get_workspace_client
from ops.rag.chunk_docs import Chunk, chunk_all
from ops.sql_utils import run_statement

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def ensure_volume_exists(client) -> None:
    # UC_VOLUME_PATH looks like /Volumes/<catalog>/<schema>/<volume_name>
    parts = settings.uc_volume_path.strip("/").split("/")
    if len(parts) != 4 or parts[0] != "Volumes":
        raise RuntimeError(f"UC_VOLUME_PATH has the wrong format, expected /Volumes/<catalog>/<schema>/<volume>: {settings.uc_volume_path}")
    _, catalog, schema, volume_name = parts
    full_name = f"{catalog}.{schema}.{volume_name}"
    try:
        client.volumes.read(full_name)
        print(f"UC Volume already exists: {full_name}")
    except Exception:
        client.volumes.create(
            catalog_name=catalog, schema_name=schema, name=volume_name, volume_type=VolumeType.MANAGED
        )
        print(f"Created UC Volume: {full_name}")


def upload_raw_docs(client) -> None:
    docs_dir = _PROJECT_ROOT / settings.docs_source_dir
    for path in sorted(docs_dir.glob("*.docx")):
        dest = f"{settings.uc_volume_path.rstrip('/')}/{path.name}"
        with open(path, "rb") as f:
            client.files.upload(dest, f, overwrite=True)
        print(f"Uploaded raw document: {dest}")


def _escape(value: str) -> str:
    return value.replace("'", "''")


def create_and_populate_delta_table(client, chunks: list[Chunk]) -> None:
    table = settings.delta_table_docs_chunks
    run_statement(
        client,
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
          chunk_id STRING NOT NULL,
          source_file STRING,
          chunk_seq INT,
          section_title STRING,
          chunk_type STRING,
          content STRING
        )
        USING DELTA
        TBLPROPERTIES (delta.enableChangeDataFeed = true)
        """,
    )
    run_statement(client, f"DELETE FROM {table}")

    batch_size = 50
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i : i + batch_size]
        values = ",\n".join(
            "('{}', '{}', {}, '{}', '{}', '{}')".format(
                _escape(c.chunk_id),
                _escape(c.source_file),
                c.chunk_seq,
                _escape(c.section_title),
                _escape(c.chunk_type),
                _escape(c.content),
            )
            for c in batch
        )
        run_statement(
            client,
            f"INSERT INTO {table} "
            "(chunk_id, source_file, chunk_seq, section_title, chunk_type, content) "
            f"VALUES {values}",
        )
    print(f"Wrote {len(chunks)} chunks to {table}")


def create_delta_sync_index(client) -> None:
    """Create the Delta Sync Index. Assumes VECTOR_SEARCH_ENDPOINT already exists (see
    `ops/rag/setup_vs_endpoint.py`) — this does not create/check the endpoint itself.

    Uses databricks-sdk's own `client.vector_search_indexes`, not the
    `databricks-ai-search` package — same reason as `setup_vs_endpoint.py`: that
    package's auth only accepts a static PAT/Service Principal token, and doesn't
    support the dynamic Azure CLI (`az login`) token this project uses.
    """
    try:
        client.vector_search_indexes.get_index(index_name=settings.vector_search_index)
        print(f"Vector Search index already exists: {settings.vector_search_index}")
        return
    except Exception:
        pass

    client.vector_search_indexes.create_index(
        name=settings.vector_search_index,
        endpoint_name=settings.vector_search_endpoint,
        primary_key="chunk_id",
        index_type=VectorIndexType.DELTA_SYNC,
        delta_sync_index_spec=DeltaSyncVectorIndexSpecRequest(
            source_table=settings.delta_table_docs_chunks,
            pipeline_type=PipelineType.TRIGGERED,
            embedding_source_columns=[
                EmbeddingSourceColumn(
                    name="content",
                    embedding_model_endpoint_name=settings.embedding_model_endpoint,
                )
            ],
        ),
    )
    print(f"Created Vector Search index: {settings.vector_search_index}")


def main() -> None:
    settings.require(
        "uc_volume_path",
        "delta_table_docs_chunks",
        "vector_search_endpoint",
        "vector_search_index",
        "embedding_model_endpoint",
        "sql_warehouse_id",
    )
    client = get_workspace_client()
    ensure_volume_exists(client)
    upload_raw_docs(client)

    docs_dir = _PROJECT_ROOT / settings.docs_source_dir
    chunks = chunk_all(docs_dir)
    create_and_populate_delta_table(client, chunks)

    create_delta_sync_index(client)
    print("Document parsing and index provisioning complete.")


if __name__ == "__main__":
    main()
