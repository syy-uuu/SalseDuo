"""Wrapper the structured_agent node uses to call Genie Space.

Genie is a stateful multi-turn conversation (context is maintained via
conversation_id): if the same user request needs to call Genie a second time, it must
reuse the same conversation_id rather than opening a new session each time — here,
whether the `conversation_id` argument is None decides whether we open a new
conversation or append to the existing one.

This deliberately doesn't use the SDK's convenience methods
start_conversation_and_wait / create_message_and_wait — when Genie returns a FAILED
status, those only raise a detail-free OperationFailed exception, discarding the SQL
Genie actually generated/tried to run and the reason it failed. Instead this polls
get_message() manually, so we get the full message object back whether it succeeded or
failed, and can record the failure reason in the white-box trace instead of the whole
request just crashing with no diagnostic information left behind.
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
                    text_parts.append(f"Query result (first {len(rows)} rows): {rows}")
    if not text_parts:
        text_parts.append("(Genie did not return any parseable text or query result)")
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
    raise TimeoutError(f"Genie message {message_id} did not finish within {_POLL_TIMEOUT_SECONDS} seconds")


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
        error_detail = str(message.error) if message.error else "Unknown error (Genie did not return error details)"
        return GenieAnswer(
            text=f"Genie query execution failed: {error_detail}",
            conversation_id=message.conversation_id,
            sql_queries=[a.query.query for a in (message.attachments or []) if a.query and a.query.query],
            error=error_detail,
        )

    return _extract_answer(client, message)
