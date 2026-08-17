"""Databricks Apps entry point: a minimal chat-box UI that calls the already-deployed
Model Serving Endpoint (which hosts the LangGraph ResponsesAgent from src/agent.py).

Uses Streamlit's built-in st.chat_message / st.chat_input to build the chat frame — no
extra custom frontend framework. The Databricks Apps runtime's default
WorkspaceClient() credential chain (the App's own service principal) is sufficient to
call a Serving Endpoint in the same workspace, so there's no need to handle
DATABRICKS_HOST/TOKEN explicitly here.
"""

from __future__ import annotations

import os

import streamlit as st
from databricks.sdk import WorkspaceClient

MODEL_SERVING_ENDPOINT_NAME = os.environ["MODEL_SERVING_ENDPOINT_NAME"]
# Sending the full history back on every request with no cap would make the request
# body and prompt grow without bound (also noted in DEVELOPMENT_JOURNAL Part 4 item 8) —
# do a simple truncation here, keeping only the most recent N messages.
_MAX_HISTORY_MESSAGES = 20

st.set_page_config(page_title="SalesDuo", page_icon="🚲")
st.title("SalesDuo · AdventureWorks Business Q&A Assistant")

if "history" not in st.session_state:
    st.session_state.history = []
if "genie_conversation_id" not in st.session_state:
    st.session_state.genie_conversation_id = None


@st.cache_resource
def _client() -> WorkspaceClient:
    return WorkspaceClient()


def _extract_text(raw: dict) -> str:
    for item in raw.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    return c.get("text", "")
    predictions = raw.get("predictions")
    if predictions:
        return str(predictions[0])
    return "(Could not parse an answer from the response — see logs for the raw response)"


def ask(messages: list[dict]) -> tuple[str, str | None]:
    # Uses the low-level REST call rather than the SDK's serving_endpoints.query():
    # the latter is a typed wrapper designed for generic chat/completions/embeddings
    # endpoints, and doesn't recognize the "output" field our custom ResponsesAgent
    # returns — .as_dict() silently drops it. Hitting /invocations directly returns the
    # full { "object": "response", "output": [...] } shape correctly.
    #
    # messages carries the full conversation history (including the current message,
    # already appended by the caller), not just the current message alone —
    # router/finalize rely on this history to resolve references like "this customer" /
    # "that credit limit just mentioned" (see src/graph/state.py::recent_history_text).
    # genie_conversation_id is passed separately via custom_inputs/custom_outputs, since
    # it isn't part of the standard messages format.
    client = _client()
    body = {"input": messages}
    if st.session_state.genie_conversation_id:
        body["custom_inputs"] = {
            "genie_conversation_id": st.session_state.genie_conversation_id
        }
    raw = client.api_client.do(
        "POST",
        f"/serving-endpoints/{MODEL_SERVING_ENDPOINT_NAME}/invocations",
        body=body,
    )
    text = _extract_text(raw)
    genie_conversation_id = raw.get("custom_outputs", {}).get("genie_conversation_id")
    return text, genie_conversation_id


for turn in st.session_state.history:
    with st.chat_message(turn["role"]):
        st.markdown(turn["content"])

question = st.chat_input("Ask something, e.g.: What's the credit limit cap for a Tier 2 customer?")
if question:
    st.session_state.history.append({"role": "user", "content": question})
    st.session_state.history = st.session_state.history[-_MAX_HISTORY_MESSAGES:]
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        with st.spinner("Querying…"):
            try:
                answer, genie_conversation_id = ask(st.session_state.history)
                st.session_state.genie_conversation_id = genie_conversation_id
            except Exception as exc:  # noqa: BLE001 - the chat box needs to surface backend errors directly to the user
                # Only shown in this render on failure, never appended to history —
                # history gets sent back to the backend as context, and a line like
                # "backend call failed: ConnectionError(...)" mixed in would be
                # misinterpreted by router/finalize as "something the agent said before",
                # producing unpredictable interference (see docs/CODE_REVIEW_FINDINGS.md
                # item 8).
                st.error(f"Backend call failed: {exc}")
            else:
                st.markdown(answer)
                st.session_state.history.append({"role": "assistant", "content": answer})
