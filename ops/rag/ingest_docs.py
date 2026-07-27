"""非结构化数据侧数据管线：
1. 把 documents_generated/ 原始 docx 上传到 UC_VOLUME_PATH，留存原始文件。
2. 解析+切块，写入 DELTA_TABLE_DOCS_CHUNKS。
3. 创建（如不存在）基于该 Delta 表的 Delta Sync Index。

前置条件：VECTOR_SEARCH_ENDPOINT 必须已存在——先跑
`python -m ops.rag.setup_vs_endpoint` 建 endpoint，再跑这个脚本，不在这里重复创建 endpoint
（见 `setup_vs_endpoint.py` 模块顶部说明，endpoint 和 index 的生命周期分开管理）。

用法: python -m ops.rag.ingest_docs
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


def create_delta_sync_index(client) -> None:
    """建 Delta Sync Index。假定 VECTOR_SEARCH_ENDPOINT 已经存在
    （见 `ops/rag/setup_vs_endpoint.py`），这里不再创建/检查 endpoint 本身。

    用 databricks-sdk 自带的 `client.vector_search_indexes`，不用 databricks-ai-search 包——
    原因同 `setup_vs_endpoint.py`：该包的认证只认 PAT/Service Principal 静态 token，不支持
    本项目用的 Azure CLI（az login）动态令牌认证。
    """
    try:
        client.vector_search_indexes.get_index(index_name=settings.vector_search_index)
        print(f"Vector Search index 已存在: {settings.vector_search_index}")
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

    create_delta_sync_index(client)
    print("文档解析与索引建仓完成。")


if __name__ == "__main__":
    main()
