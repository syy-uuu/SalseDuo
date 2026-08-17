"""Small shared SQL-execution helper used by the provisioning scripts (runs statements
via a SQL Warehouse). structured_agent / unstructured_agent's business logic doesn't
share this layer — this is just the "submit SQL and wait for the result" pattern that
comes up repeatedly during provisioning, factored out so each setup script doesn't
reimplement its own polling logic."""

from __future__ import annotations

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

from src.config import settings


def run_statement(client: WorkspaceClient, statement: str) -> None:
    """Executes a single SQL statement synchronously, raising an exception containing
    the Databricks error details on failure."""
    settings.require("sql_warehouse_id")
    resp = client.statement_execution.execute_statement(
        warehouse_id=settings.sql_warehouse_id,
        statement=statement,
        wait_timeout="50s",
    )
    status = resp.status
    if status is None or status.state != StatementState.SUCCEEDED:
        error = status.error if status else None
        raise RuntimeError(f"SQL execution failed: {error}\nStatement:\n{statement}")
