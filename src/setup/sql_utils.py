"""建仓脚本共用的 SQL 执行小工具（通过 SQL Warehouse 执行语句）。
structured_agent / unstructured_agent 的业务逻辑不共用这层——这里只是建仓阶段
反复要用到的"提交 SQL 并等待结果"，抽出来避免每个 setup 脚本重复写轮询逻辑。"""

from __future__ import annotations

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

from src.config import settings


def run_statement(client: WorkspaceClient, statement: str) -> None:
    """同步执行一条 SQL 语句，失败时抛出包含 Databricks 错误信息的异常。"""
    settings.require("sql_warehouse_id")
    resp = client.statement_execution.execute_statement(
        warehouse_id=settings.sql_warehouse_id,
        statement=statement,
        wait_timeout="50s",
    )
    status = resp.status
    if status is None or status.state != StatementState.SUCCEEDED:
        error = status.error if status else None
        raise RuntimeError(f"SQL 执行失败: {error}\n语句:\n{statement}")
