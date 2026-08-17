"""Step 5/7 handoff: logs the ResponsesAgent from src/agent.py as an MLflow Model,
registers it in Unity Catalog, and deploys it as a Model Serving Endpoint (for the
Databricks App to call).

Declares the agent's runtime dependencies — Genie Space / Vector Search Index / LLM
Serving Endpoint / SQL Warehouse — via mlflow.pyfunc.log_model(resources=[...]).
Model Serving uses this to automatically grant the serving endpoint's service principal
access to these resources, with no need to configure permissions on them by hand.

Usage: python -m ops.deploy_model
"""

from __future__ import annotations

from pathlib import Path

import mlflow
from mlflow.models.resources import (
    DatabricksFunction,
    DatabricksGenieSpace,
    DatabricksServingEndpoint,
    DatabricksSQLWarehouse,
    DatabricksVectorSearchIndex,
)

from src.config import settings
from src.db_client import get_workspace_client

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_AGENT_ENTRYPOINT = str(_PROJECT_ROOT / "src" / "agent.py")
# Only passes the dependencies actually needed at runtime (see
# docs/CODE_REVIEW_FINDINGS.md item 2): the root requirements.txt mixes in heavy
# provisioning-only dependencies (databricks-connect/azure-*/python-docx/pytest) — if
# the whole file were passed to log_model, they'd get baked into the serving container
# image verbatim even though the runtime code never uses them, purely slowing down cold
# starts.
_REQUIREMENTS_FILE = str(_PROJECT_ROOT / "requirements-runtime.txt")
# agent.py's `from src.xxx import yyy` style imports also need to resolve once mlflow
# loads the model into an isolated Serving container — code_paths bundles the whole
# src/ package into the model artifact, and at load time mlflow adds code_paths'
# *parent* directory (not code_paths itself) to sys.path, so the `src` package imports
# correctly. For the same reason prompts/ also needs to be included: router.py/
# finalize.py/state.py do `from prompts.loader import render_prompt` at runtime to read
# prompts/*.prompt, and those files live outside src/, so they wouldn't be picked up by
# the first code_paths entry automatically — missing this causes a FileNotFoundError
# (can't find prompts/router.prompt) as soon as the model tries to load once deployed.
_CODE_PATHS = [str(_PROJECT_ROOT / "src"), str(_PROJECT_ROOT / "prompts")]


def _resources() -> list:
    resources = [
        DatabricksServingEndpoint(endpoint_name=settings.llm_serving_endpoint),
        DatabricksSQLWarehouse(warehouse_id=settings.sql_warehouse_id),
        DatabricksGenieSpace(genie_space_id=settings.genie_space_id),
        DatabricksVectorSearchIndex(index_name=settings.vector_search_index),
    ]
    for fn_name in ("calculate_credit_terms", "check_large_transaction_compliance"):
        resources.append(
            DatabricksFunction(
                function_name=f"{settings.uc_catalog}.{settings.uc_function_schema}.{fn_name}"
            )
        )
    return resources


def _registered_model_name() -> str:
    app_slug = settings.databricks_app_name.replace("-", "_")
    return f"{settings.uc_catalog}.{settings.uc_function_schema}.{app_slug}"


def log_and_register_model() -> str:
    # Without setting this explicitly, running locally (outside a Databricks
    # notebook/job) makes mlflow default to writing to a local SQLite DB
    # (mlflow.db + mlruns/ under the project directory) instead of the real Databricks
    # workspace — hit this exact issue before: the model "registered successfully"
    # locally, but Databricks' serving_endpoints.create then said the model didn't
    # exist, because it was never actually written there in the first place.
    mlflow.set_tracking_uri("databricks")
    mlflow.set_registry_uri("databricks-uc")
    mlflow.set_experiment(settings.mlflow_experiment_path)
    registered_model_name = _registered_model_name()
    with mlflow.start_run(run_name="salesduo-agent-log"):
        model_info = mlflow.pyfunc.log_model(
            name="agent",
            python_model=_AGENT_ENTRYPOINT,
            pip_requirements=_REQUIREMENTS_FILE,
            code_paths=_CODE_PATHS,
            resources=_resources(),
            registered_model_name=registered_model_name,
        )
    print(f"Registered model: {registered_model_name} version {model_info.registered_model_version}")
    return model_info.registered_model_version


def _serving_environment_vars() -> dict:
    # The served model runs in an isolated container with no local .env file — these
    # non-secret config values that config.py reads must be passed in explicitly as
    # serving endpoint environment variables so the model can read them at runtime.
    # DATABRICKS_HOST/DATABRICKS_TOKEN are intentionally omitted: auth is handled
    # automatically via the resources declared to log_model — there's no need (and it
    # would be a bad idea) to put a token in plaintext in the serving config.
    return {
        "UC_CATALOG": settings.uc_catalog,
        "UC_SCHEMA_SALES": settings.uc_schema_sales,
        "UC_SCHEMA_PERSON": settings.uc_schema_person,
        "UC_SCHEMA_PRODUCTION": settings.uc_schema_production,
        "UC_FUNCTION_SCHEMA": settings.uc_function_schema,
        "SQL_WAREHOUSE_ID": settings.sql_warehouse_id,
        "GENIE_SPACE_ID": settings.genie_space_id,
        "VECTOR_SEARCH_ENDPOINT": settings.vector_search_endpoint,
        "VECTOR_SEARCH_INDEX": settings.vector_search_index,
        "EMBEDDING_MODEL_ENDPOINT": settings.embedding_model_endpoint,
        "LLM_SERVING_ENDPOINT": settings.llm_serving_endpoint,
        "MAX_ROUTER_LOOPS": str(settings.max_router_loops),
    }


def deploy_serving_endpoint(model_version: str) -> None:
    from databricks.sdk.service.serving import (
        EndpointCoreConfigInput,
        ServedEntityInput,
    )

    client = get_workspace_client()
    registered_model_name = _registered_model_name()
    served_entity = ServedEntityInput(
        entity_name=registered_model_name,
        entity_version=model_version,
        workload_size="Large",
        scale_to_zero_enabled=True,
        environment_vars=_serving_environment_vars(),
    )
    endpoint_name = settings.model_serving_endpoint_name

    existing = [e.name for e in client.serving_endpoints.list()]
    if endpoint_name in existing:
        client.serving_endpoints.update_config_and_wait(
            name=endpoint_name, served_entities=[served_entity]
        )
        print(f"Updated serving endpoint: {endpoint_name} -> version {model_version}")
    else:
        client.serving_endpoints.create_and_wait(
            name=endpoint_name,
            config=EndpointCoreConfigInput(name=endpoint_name, served_entities=[served_entity]),
        )
        print(f"Created serving endpoint: {endpoint_name} -> version {model_version}")


def main() -> None:
    settings.require(
        "uc_catalog",
        "uc_function_schema",
        "genie_space_id",
        "vector_search_index",
        "sql_warehouse_id",
        "mlflow_experiment_path",
        "model_serving_endpoint_name",
    )
    model_version = log_and_register_model()
    deploy_serving_endpoint(model_version)
    print("Step 7 model registration and Serving Endpoint deployment complete.")


if __name__ == "__main__":
    main()
