"""Databricks SDK 认证的唯一入口。全项目其他地方都从这里拿 WorkspaceClient，
不要在多处重复初始化认证逻辑。"""

from __future__ import annotations

from functools import lru_cache

from databricks.sdk import WorkspaceClient

from src.config import settings


@lru_cache(maxsize=1)
def get_workspace_client() -> WorkspaceClient:
    """按优先级选择认证方式：
    1) DATABRICKS_CONFIG_PROFILE（走 ~/.databrickscfg）
    2) DATABRICKS_HOST + DATABRICKS_TOKEN（PAT）
    两者都未配置时，交由 databricks-sdk 的默认凭据链处理（会抛出清晰的 SDK 报错）。
    """
    if settings.databricks_config_profile:
        return WorkspaceClient(profile=settings.databricks_config_profile)
    if settings.databricks_host and settings.databricks_token:
        return WorkspaceClient(host=settings.databricks_host, token=settings.databricks_token)
    return WorkspaceClient()
