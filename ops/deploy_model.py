"""Step 5/7 衔接：把 src/agent.py 的 ResponsesAgent 记录为 MLflow Model，
注册到 Unity Catalog，并部署为 Model Serving Endpoint（供 Databricks App 调用）。

通过 mlflow.pyfunc.log_model(resources=[...]) 声明该 agent 运行时依赖的
Genie Space / Vector Search Index / LLM Serving Endpoint / SQL Warehouse，
Model Serving 会据此自动为 serving endpoint 的 service principal 授权访问这些资源，
不需要手工在这些资源上单独配置权限。

用法: python -m ops.deploy_model
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
# 只传运行时实际需要的依赖（见 docs/CODE_REVIEW_FINDINGS.md 第2条）：根目录 requirements.txt
# 混了 databricks-connect/azure-*/python-docx/pytest 这些建仓脚本专用的重依赖，如果整份传给
# log_model，会被原样打进 serving 容器镜像，运行时代码从来用不到，纯粹拖慢冷启动。
_REQUIREMENTS_FILE = str(_PROJECT_ROOT / "requirements-runtime.txt")
# agent.py 里 `from src.xxx import yyy` 这种写法，在 mlflow 把模型加载到隔离的 Serving
# 容器里时也要能 resolve——code_paths 把整个 src/ 包一起打进模型artifact，mlflow 加载时会把
# code_paths 的父目录（不是 code_paths 自己）加进 sys.path，所以 `src` 包能正常 import。
_CODE_PATHS = [str(_PROJECT_ROOT / "src")]


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
    # 不显式指定的话，本地跑（不在 Databricks notebook/job 里）时 mlflow 会默认写到本地
    # SQLite（项目目录下的 mlflow.db + mlruns/），而不是真正写到 Databricks workspace——
    # 之前踩过这个坑：本地"注册成功"，但 Databricks 那边 serving_endpoints.create 一查
    # 说模型不存在，因为压根不在同一个地方。
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
    print(f"已注册模型: {registered_model_name} version {model_info.registered_model_version}")
    return model_info.registered_model_version


def _serving_environment_vars() -> dict:
    # 被 serve 的模型跑在隔离容器里，没有本地的 .env 文件——config.py 读的这些非密钥配置项
    # 必须显式作为 serving endpoint 的环境变量传进去，模型运行时才能读到。
    # 不传 DATABRICKS_HOST/DATABRICKS_TOKEN：认证走 log_model 时声明的 resources 自动鉴权，
    # 不需要（也不应该）把 token 明文塞进 serving 配置里。
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
        workload_size="Small",
        scale_to_zero_enabled=True,
        environment_vars=_serving_environment_vars(),
    )
    endpoint_name = settings.model_serving_endpoint_name

    existing = [e.name for e in client.serving_endpoints.list()]
    if endpoint_name in existing:
        client.serving_endpoints.update_config_and_wait(
            name=endpoint_name, served_entities=[served_entity]
        )
        print(f"已更新 serving endpoint: {endpoint_name} -> version {model_version}")
    else:
        client.serving_endpoints.create_and_wait(
            name=endpoint_name,
            config=EndpointCoreConfigInput(name=endpoint_name, served_entities=[served_entity]),
        )
        print(f"已创建 serving endpoint: {endpoint_name} -> version {model_version}")


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
    print("Step 7 模型注册与 Serving Endpoint 部署完成。")


if __name__ == "__main__":
    main()
