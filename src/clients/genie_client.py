"""structured_agent 节点调用 Genie Space 的封装。

Genie 是有状态的多轮对话（靠 conversation_id 维持上下文）：同一个用户请求如果需要
第二次调用 Genie，必须复用同一个 conversation_id，不要每次开新会话——这里通过
`conversation_id` 参数是否为 None 来决定开新会话还是在原会话里追加消息。

这里没有用 SDK 的 start_conversation_and_wait / create_message_and_wait 这两个便捷方法——
它们在 Genie 返回 FAILED 状态时只会抛一个不带详情的 OperationFailed 异常，把 Genie 实际
生成/执行失败的 SQL 和报错原因都丢掉了。改成手动轮询 get_message()，这样无论成功还是
失败都能拿到完整的 message 对象，把失败原因也记录进白盒追踪里，而不是让整个请求崩溃、
什么诊断信息都留不下。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from databricks.sdk.service.dashboards import MessageStatus

from src.config import settings
from src.db_client import get_workspace_client

_POLL_INTERVAL_SECONDS = 2
_POLL_TIMEOUT_SECONDS = 300


@dataclass
class GenieAnswer:
    text: str
    conversation_id: str
    sql_queries: list[str] = field(default_factory=list)
    error: str | None = None


def _extract_answer(client, message) -> GenieAnswer:
    text_parts: list[str] = []
    sql_queries: list[str] = []
    for attachment in message.attachments or []:
        if attachment.text and attachment.text.content:
            text_parts.append(attachment.text.content)
        if attachment.query:
            if attachment.query.description:
                text_parts.append(attachment.query.description)
            if attachment.query.query:
                sql_queries.append(attachment.query.query)
            result = client.genie.get_message_query_result(
                space_id=message.space_id,
                conversation_id=message.conversation_id,
                message_id=message.message_id,
            )
            if result and result.statement_response and result.statement_response.result:
                rows = result.statement_response.result.data_array or []
                if rows:
                    text_parts.append(f"查询结果（前 {len(rows)} 行）: {rows}")
    if not text_parts:
        text_parts.append("(Genie 未返回可解析的文本或查询结果)")
    return GenieAnswer(
        text="\n".join(text_parts),
        conversation_id=message.conversation_id,
        sql_queries=sql_queries,
    )


def _poll_until_done(client, space_id: str, conversation_id: str, message_id: str):
    deadline = time.time() + _POLL_TIMEOUT_SECONDS
    while time.time() < deadline:
        message = client.genie.get_message(
            space_id=space_id, conversation_id=conversation_id, message_id=message_id
        )
        if message.status in (MessageStatus.COMPLETED, MessageStatus.FAILED):
            return message
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise TimeoutError(f"Genie 消息 {message_id} 在 {_POLL_TIMEOUT_SECONDS} 秒内未完成")


def ask_genie(question: str, conversation_id: str | None = None) -> GenieAnswer:
    settings.require("genie_space_id")
    client = get_workspace_client()

    if conversation_id:
        wait = client.genie.create_message(
            space_id=settings.genie_space_id, conversation_id=conversation_id, content=question
        )
    else:
        wait = client.genie.start_conversation(space_id=settings.genie_space_id, content=question)

    bound = wait.bind()
    message = _poll_until_done(
        client, space_id=bound["space_id"], conversation_id=bound["conversation_id"], message_id=bound["message_id"]
    )

    if message.status == MessageStatus.FAILED:
        error_detail = str(message.error) if message.error else "未知错误（Genie 未返回 error 详情）"
        return GenieAnswer(
            text=f"Genie 查询执行失败: {error_detail}",
            conversation_id=message.conversation_id,
            sql_queries=[a.query.query for a in (message.attachments or []) if a.query and a.query.query],
            error=error_detail,
        )

    return _extract_answer(client, message)
