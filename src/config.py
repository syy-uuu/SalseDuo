"""Single entry point for reading environment variables. Every other module in the
project must use `from src.config import settings` — don't call os.environ / os.getenv
directly elsewhere."""

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
    # Databricks auth and connection (native Azure auth: no explicit PAT — relies on the
    # local machine's `az login` Azure AD credentials plus the three Azure resource
    # coordinates below, which databricks-sdk uses to resolve the workspace host via
    # Azure Resource Manager and authenticate)
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

    # Unstructured data / Vector Search
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

    # Orchestration LLM
    llm_serving_endpoint: str = field(
        default_factory=lambda: _env(
            "LLM_SERVING_ENDPOINT", "databricks-meta-llama-3-3-70b-instruct"
        )
    )

    # MLflow / evaluation / deployment
    mlflow_experiment_path: str = field(
        default_factory=lambda: _env("MLFLOW_EXPERIMENT_PATH", "/Shared/salesduo-agent")
    )
    model_serving_endpoint_name: str = field(
        default_factory=lambda: _env("MODEL_SERVING_ENDPOINT_NAME", "salesduo-agent")
    )
    databricks_app_name: str = field(
        default_factory=lambda: _env("DATABRICKS_APP_NAME", "salesduo-agent")
    )

    # Orchestration safety valve
    max_router_loops: int = field(default_factory=lambda: int(_env("MAX_ROUTER_LOOPS", "5")))

    def require(self, *names: str) -> None:
        """Validates that the given fields are non-empty, raising a clear error message
        when something is missing, so failures surface early with a useful message."""
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            raise RuntimeError(
                f"Missing required environment variable(s)/setting(s): {', '.join(missing)}. "
                "Add them to .env and try again."
            )

    def __post_init__(self) -> None:
        # Not every code path goes through src/db_client.py to get a WorkspaceClient —
        # for example mlflow's own mlflow.utils.databricks_utils.get_databricks_host_creds()
        # (used internally by tracking/registry calls) constructs a bare, argument-less
        # WorkspaceClient() with no idea our custom AZURE_SUBSCRIPTION_ID/
        # RESOURCE_GROUP_NAME/DATABRICKS_WORKSPACE_NAME variables even exist.
        # databricks-sdk officially recognizes the DATABRICKS_AZURE_RESOURCE_ID
        # environment variable (any bare WorkspaceClient() reads it automatically), so
        # this writes the assembled resource ID back into that variable, letting every
        # code path — whether or not it goes through our own wrapper — use the same
        # Azure auth.
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
