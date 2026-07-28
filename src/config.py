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
    # Databricks 认证与连接（Azure 原生认证：不显式传 PAT，靠本机 `az login` 的 Azure AD
    # 凭据 + 下面三个 Azure 资源坐标，由 databricks-sdk 通过 Azure Resource Manager 解析出
    # workspace host 并鉴权）
    azure_subscription_id: str | None = field(
        default_factory=lambda: _env("AZURE_SUBSCRIPTION_ID")
    )
    azure_resource_group_name: str | None = field(
        default_factory=lambda: _env("RESOURCE_GROUP_NAME")
    )
    azure_databricks_workspace_name: str | None = field(
        default_factory=lambda: _env("DATABRICKS_WORKSPACE_NAME")
    )
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

    def __post_init__(self) -> None:
        # 不是所有代码路径都经过 src/db_client.py 拿 WorkspaceClient——比如 mlflow 自己的
        # mlflow.utils.databricks_utils.get_databricks_host_creds()（tracking/registry
        # 相关调用内部会走到这里）会新建一个不带任何参数的 WorkspaceClient()，根本不知道
        # AZURE_SUBSCRIPTION_ID/RESOURCE_GROUP_NAME/DATABRICKS_WORKSPACE_NAME 这三个自定义
        # 变量的存在。databricks-sdk 官方认的是 DATABRICKS_AZURE_RESOURCE_ID 这个环境变量
        # （任何裸 WorkspaceClient() 都会自动读它），这里把拼好的资源 ID 写回这个环境变量，
        # 让所有代码路径（不管是不是经过我们自己的封装）都能用同一套 Azure 认证。
        if (
            not os.environ.get("DATABRICKS_AZURE_RESOURCE_ID")
            and self.azure_subscription_id
            and self.azure_resource_group_name
            and self.azure_databricks_workspace_name
        ):
            os.environ["DATABRICKS_AZURE_RESOURCE_ID"] = (
                f"/subscriptions/{self.azure_subscription_id}"
                f"/resourceGroups/{self.azure_resource_group_name}"
                f"/providers/Microsoft.Databricks/workspaces/{self.azure_databricks_workspace_name}"
            )


settings = Settings()
