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
    2) Azure 原生认证：AZURE_SUBSCRIPTION_ID + RESOURCE_GROUP_NAME +
       DATABRICKS_WORKSPACE_NAME 拼成 azure_workspace_resource_id（ARM 资源 ID），
       databricks-sdk 用它向 Azure Resource Manager 解析出 workspace host，鉴权走本机
       `az login` 留下的 Azure AD 会话（SDK 内部走 Azure CLI token source），不需要显式传
       Databricks PAT token。前提：本机已安装 Azure CLI 且执行过 `az login`。
    两者都未配置时，交由 databricks-sdk 的默认凭据链处理（会抛出清晰的 SDK 报错）。
    """
    if settings.databricks_config_profile:
        return WorkspaceClient(profile=settings.databricks_config_profile)
    if (
        settings.azure_subscription_id
        and settings.azure_resource_group_name
        and settings.azure_databricks_workspace_name
    ):
        azure_workspace_resource_id = (
            f"/subscriptions/{settings.azure_subscription_id}"
            f"/resourceGroups/{settings.azure_resource_group_name}"
            f"/providers/Microsoft.Databricks/workspaces/{settings.azure_databricks_workspace_name}"
        )
        return WorkspaceClient(azure_workspace_resource_id=azure_workspace_resource_id)
    return WorkspaceClient()
