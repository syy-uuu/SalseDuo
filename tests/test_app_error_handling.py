"""Verifies the fix for docs/CODE_REVIEW_FINDINGS.md item 8: when a backend call in
app/app.py fails, the error must not pollute st.session_state.history (history gets
sent back to the backend in full as context — an assistant message like "backend call
failed: ConnectionError(...)" mixed in would interfere with the judgment made in
subsequent turns).

app/app.py is a Streamlit script — its module-level code executes top-to-bottom
directly, it isn't a function you can call repeatedly. In real Streamlit, every user
interaction re-runs the entire script, with session_state persisting across those
re-runs. This test doesn't install the real streamlit package (app/ has its own
lightweight requirements.txt; the main project venv shouldn't need a heavy extra
dependency just to test one UI script, nor a real Databricks connection) — instead it
injects a fake streamlit module into sys.modules that implements only the handful of
interfaces app.py actually uses (a fake session_state + no-op UI components), then uses
importlib to re-execute app.py as a script by file path, simulating a "re-run".
Likewise, databricks.sdk.WorkspaceClient is swapped for a fake, used to control whether
the backend call succeeds or raises this time — no real network connection needed.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from contextlib import contextmanager
from pathlib import Path

_APP_PATH = Path(__file__).resolve().parent.parent / "app" / "app.py"


class _FakeSessionState(dict):
    """A minimal stand-in for st.session_state: supports both
    `"x" not in st.session_state` (dict's __contains__) and
    `st.session_state.x` / `st.session_state.x = ...` (attribute read/write), matching
    how real Streamlit's SessionStateProxy is used."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


@contextmanager
def _noop_ctx(*args, **kwargs):
    yield


def _make_fake_streamlit(chat_input_value: str, session_state: _FakeSessionState) -> types.ModuleType:
    st = types.ModuleType("streamlit")
    st.session_state = session_state
    st.set_page_config = lambda **kw: None
    st.title = lambda *a, **kw: None
    st.cache_resource = lambda fn: fn  # no real caching needed for the test, an identity decorator is enough
    st.chat_message = _noop_ctx
    st.spinner = _noop_ctx
    st.markdown = lambda *a, **kw: None
    st.error = lambda *a, **kw: None
    st.chat_input = lambda *a, **kw: chat_input_value
    return st


class _FailingClient:
    """Simulates WorkspaceClient(): api_client.do(...) raises directly, corresponding
    to ask() failing."""

    def __init__(self):
        self.api_client = types.SimpleNamespace(do=self._raise)

    @staticmethod
    def _raise(*args, **kwargs):
        raise RuntimeError("simulated backend call failure")


class _SucceedingClient:
    """Simulates WorkspaceClient(): api_client.do(...) returns a normal ResponsesAgent
    response."""

    def __init__(self):
        self.api_client = types.SimpleNamespace(do=self._succeed)

    @staticmethod
    def _succeed(*args, **kwargs):
        return {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "this is a normal answer"}],
                }
            ],
            "custom_outputs": {"genie_conversation_id": "conv-123"},
        }


def _run_one_turn(question: str, fake_client_factory, session_state: _FakeSessionState, monkeypatch) -> None:
    """Simulates one Streamlit re-run: question is what the user "typed" into
    chat_input this turn; session_state carries over from the previous turn (the same
    object, not recreated each time)."""
    fake_st = _make_fake_streamlit(question, session_state)
    monkeypatch.setitem(sys.modules, "streamlit", fake_st)
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", fake_client_factory)
    monkeypatch.setenv("MODEL_SERVING_ENDPOINT_NAME", "fake-endpoint")

    spec = importlib.util.spec_from_file_location(f"app_under_test_{id(session_state)}", _APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


def test_failed_call_does_not_pollute_history(monkeypatch):
    session_state = _FakeSessionState()

    _run_one_turn("ask a question that will fail", _FailingClient, session_state, monkeypatch)

    assert session_state.history == [
        {"role": "user", "content": "ask a question that will fail"}
    ], "no assistant message should be appended to history on failure (and certainly not an error stack trace)"


def test_history_stays_clean_across_failure_then_success(monkeypatch):
    """One failing turn, then one succeeding turn — confirms the failure left no trace
    behind, and the second turn's history cleanly gains just this one question-and-answer
    pair, without the first turn's error text being carried to the backend as "something
    the agent said before"."""
    session_state = _FakeSessionState()

    _run_one_turn("first message, will fail", _FailingClient, session_state, monkeypatch)
    _run_one_turn("second message, will succeed", _SucceedingClient, session_state, monkeypatch)

    roles_and_content = [(m["role"], m["content"]) for m in session_state.history]
    assert roles_and_content == [
        ("user", "first message, will fail"),
        ("user", "second message, will succeed"),
        ("assistant", "this is a normal answer"),
    ]


def test_successful_call_still_writes_history_and_conversation_id(monkeypatch):
    """Reverse check: confirms this fix (try/except/else) didn't also break the success
    path along the way — history and genie_conversation_id should still be written on
    success."""
    session_state = _FakeSessionState()

    _run_one_turn("a normal question", _SucceedingClient, session_state, monkeypatch)

    assert session_state.history == [
        {"role": "user", "content": "a normal question"},
        {"role": "assistant", "content": "this is a normal answer"},
    ]
    assert session_state.genie_conversation_id == "conv-123"
