"""统一的环境变量读取入口。全项目其他模块一律 `from src.config import settings`，
不要在别处直接调用 os.environ / os.getenv。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    # Databricks 认证与连接
    databricks_host: str | None = field(default_factory=lambda: _env("DATABRICKS_HOST"))
    databricks_token: str | None = field(default_factory=lambda: _env("DATABRICKS_TOKEN"))
    databricks_config_profile: str | None = field(
        default_factory=lambda: _env("DATABRICKS_CONFIG_PROFILE")
    )

    # Unity Catalog
    uc_catalog: str = field(default_factory=lambda: _env("UC_CATALOG", "adventureworks_dataagent"))
    uc_schema_sales: str = field(default_factory=lambda: _env("UC_SCHEMA_SALES", "sales"))
    uc_schema_person: str = field(default_factory=lambda: _env("UC_SCHEMA_PERSON", "person"))
    uc_schema_production: str = field(
        default_factory=lambda: _env("UC_SCHEMA_PRODUCTION", "production")
    )
    uc_function_schema: str = field(
        default_factory=lambda: _env("UC_FUNCTION_SCHEMA", "salesduo_agent_tools")
    )

    # SQL Warehouse
    sql_warehouse_id: str | None = field(default_factory=lambda: _env("SQL_WAREHOUSE_ID"))

    # Genie
    genie_space_id: str | None = field(default_factory=lambda: _env("GENIE_SPACE_ID"))

    # 非结构化数据 / Vector Search
    docs_source_dir: str = field(
        default_factory=lambda: _env("DOCS_SOURCE_DIR", "documents_generated")
    )
    uc_volume_path: str | None = field(default_factory=lambda: _env("UC_VOLUME_PATH"))
    delta_table_docs_chunks: str | None = field(
        default_factory=lambda: _env("DELTA_TABLE_DOCS_CHUNKS")
    )
    vector_search_endpoint: str = field(
        default_factory=lambda: _env("VECTOR_SEARCH_ENDPOINT", "salesduo-vs-endpoint")
    )
    vector_search_index: str | None = field(default_factory=lambda: _env("VECTOR_SEARCH_INDEX"))
    embedding_model_endpoint: str = field(
        default_factory=lambda: _env("EMBEDDING_MODEL_ENDPOINT", "databricks-gte-large-en")
    )

    # 编排 LLM
    llm_serving_endpoint: str = field(
        default_factory=lambda: _env(
            "LLM_SERVING_ENDPOINT", "databricks-meta-llama-3-3-70b-instruct"
        )
    )

    # MLflow / 评测 / 部署
    mlflow_experiment_path: str = field(
        default_factory=lambda: _env("MLFLOW_EXPERIMENT_PATH", "/Shared/salesduo-agent")
    )
    model_serving_endpoint_name: str = field(
        default_factory=lambda: _env("MODEL_SERVING_ENDPOINT_NAME", "salesduo-agent")
    )
    databricks_app_name: str = field(
        default_factory=lambda: _env("DATABRICKS_APP_NAME", "salesduo-agent")
    )

    # 编排安全阀
    max_router_loops: int = field(default_factory=lambda: int(_env("MAX_ROUTER_LOOPS", "5")))

    def require(self, *names: str) -> None:
        """校验指定字段非空，缺失时抛出清晰的错误信息，便于在具体 step 里提前失败。"""
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            raise RuntimeError(
                f"缺少必需的环境变量/配置项: {', '.join(missing)}。请在 .env 中补充后重试。"
            )


settings = Settings()
