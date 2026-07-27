"""Step 3（非结构化数据侧）：
1. 把 documents_generated/ 原始 docx 上传到 UC_VOLUME_PATH，留存原始文件。
2. 解析+切块，写入 DELTA_TABLE_DOCS_CHUNKS。
3. 创建（如不存在）Vector Search endpoint 和基于该 Delta 表的 Delta Sync Index。

用法: python -m src.setup.ingest_docs
"""

from __future__ import annotations

from pathlib import Path

from databricks.ai_search.client import VectorSearchClient
from databricks.sdk.service.catalog import VolumeType

from src.config import settings
from src.db_client import get_workspace_client
from src.setup.chunk_docs import Chunk, chunk_all
from src.setup.sql_utils import run_statement

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def ensure_volume_exists(client) -> None:
    # UC_VOLUME_PATH 形如 /Volumes/<catalog>/<schema>/<volume_name>
    parts = settings.uc_volume_path.strip("/").split("/")
    if len(parts) != 4 or parts[0] != "Volumes":
        raise RuntimeError(f"UC_VOLUME_PATH 格式不对，期望 /Volumes/<catalog>/<schema>/<volume>: {settings.uc_volume_path}")
    _, catalog, schema, volume_name = parts
    full_name = f"{catalog}.{schema}.{volume_name}"
    try:
        client.volumes.read(full_name)
        print(f"UC Volume 已存在: {full_name}")
    except Exception:
        client.volumes.create(
            catalog_name=catalog, schema_name=schema, name=volume_name, volume_type=VolumeType.MANAGED
        )
        print(f"已创建 UC Volume: {full_name}")


def upload_raw_docs(client) -> None:
    docs_dir = _PROJECT_ROOT / settings.docs_source_dir
    for path in sorted(docs_dir.glob("*.docx")):
        dest = f"{settings.uc_volume_path.rstrip('/')}/{path.name}"
        with open(path, "rb") as f:
            client.files.upload(dest, f, overwrite=True)
        print(f"已上传原始文档: {dest}")


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
    print(f"已写入 {len(chunks)} 条 chunk 到 {table}")


def create_vector_search_index() -> None:
    vsc = VectorSearchClient(disable_notice=True)
    endpoints = {e["name"] for e in vsc.list_endpoints().get("endpoints", [])}
    if settings.vector_search_endpoint not in endpoints:
        vsc.create_endpoint(name=settings.vector_search_endpoint, endpoint_type="STANDARD")
        print(f"已创建 Vector Search endpoint: {settings.vector_search_endpoint}")
    else:
        print(f"Vector Search endpoint 已存在: {settings.vector_search_endpoint}")

    try:
        vsc.get_index(endpoint_name=settings.vector_search_endpoint, index_name=settings.vector_search_index)
        print(f"Vector Search index 已存在: {settings.vector_search_index}")
        return
    except Exception:
        pass

    vsc.create_delta_sync_index(
        endpoint_name=settings.vector_search_endpoint,
        index_name=settings.vector_search_index,
        primary_key="chunk_id",
        source_table_name=settings.delta_table_docs_chunks,
        pipeline_type="TRIGGERED",
        embedding_source_column="content",
        embedding_model_endpoint_name=settings.embedding_model_endpoint,
    )
    print(f"已创建 Vector Search index: {settings.vector_search_index}")


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

    create_vector_search_index()
    print("Step 3 文档解析与索引建仓完成。")


if __name__ == "__main__":
    main()
