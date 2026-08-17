"""Single entry point for Databricks SDK authentication. Every other module in the
project must obtain its WorkspaceClient from here — don't re-initialize auth logic
in multiple places."""

from __future__ import annotations

from functools import lru_cache

from databricks.sdk import WorkspaceClient

from src.config import settings


@lru_cache(maxsize=1)
def get_workspace_client() -> WorkspaceClient:
    """Choose the auth method by priority:
    1) DATABRICKS_CONFIG_PROFILE (via ~/.databrickscfg)
    2) Native Azure auth: AZURE_SUBSCRIPTION_ID + RESOURCE_GROUP_NAME +
       DATABRICKS_WORKSPACE_NAME are combined into azure_workspace_resource_id (an ARM
       resource ID). databricks-sdk uses it to resolve the workspace host via Azure
       Resource Manager, and authenticates using the local machine's `az login` Azure AD
       session (the SDK's Azure CLI token source) — no explicit Databricks PAT token
       needed. Prerequisite: the Azure CLI must be installed locally and `az login`
       already run.
    If neither is configured, falls through to databricks-sdk's default credential
    chain (which raises a clear SDK error on its own).
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
