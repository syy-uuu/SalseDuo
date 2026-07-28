"""验证 docs/CODE_REVIEW_FINDINGS.md 第 8 条修复：app/app.py 后端调用失败时，
错误信息不能污染 st.session_state.history（history 会被完整发回后端当上下文，一条
"调用后端出错: ConnectionError(...)"的 assistant 消息混进去会干扰后续几轮的判断）。

app/app.py 是 Streamlit 脚本，模块级代码直接顶格执行，不是可以反复调用的函数——真实
Streamlit 每次用户交互都会把整个脚本重新跑一遍，session_state 在多次重跑之间持续存在。
这里没有安装真的 streamlit 包（app/ 有自己独立的轻量 requirements.txt，主项目 venv 不需要
为了测一个 UI 脚本额外装一个重依赖，也不需要真的连 Databricks）——改用一个只实现了
app.py 用到的那几个接口的假 streamlit 模块（假 session_state + 空动作的 UI 组件），
注入进 sys.modules，然后用 importlib 按文件路径把 app.py 当脚本重新执行，模拟"重跑"；
同理把 databricks.sdk.WorkspaceClient 也换成假的，用来控制"这次后端调用成功还是抛异常"，
不需要真实网络连接。
"""

from __future__ import annotations

import importlib.util
import sys
import types
from contextlib import contextmanager
from pathlib import Path

_APP_PATH = Path(__file__).resolve().parent.parent / "app" / "app.py"


class _FakeSessionState(dict):
    """最简版 st.session_state 替身：既支持 `"x" not in st.session_state`（dict 的
    __contains__），也支持 `st.session_state.x` / `st.session_state.x = ...`（属性
    读写），跟真实 Streamlit 的 SessionStateProxy 用法一致。"""

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
    st.cache_resource = lambda fn: fn  # 测试不需要真的缓存，恒等装饰器即可
    st.chat_message = _noop_ctx
    st.spinner = _noop_ctx
    st.markdown = lambda *a, **kw: None
    st.error = lambda *a, **kw: None
    st.chat_input = lambda *a, **kw: chat_input_value
    return st


class _FailingClient:
    """模拟 WorkspaceClient()：api_client.do(...) 直接抛异常，对应 ask() 失败的情况。"""

    def __init__(self):
        self.api_client = types.SimpleNamespace(do=self._raise)

    @staticmethod
    def _raise(*args, **kwargs):
        raise RuntimeError("模拟后端调用失败")


class _SucceedingClient:
    """模拟 WorkspaceClient()：api_client.do(...) 返回一个正常的 ResponsesAgent 响应。"""

    def __init__(self):
        self.api_client = types.SimpleNamespace(do=self._succeed)

    @staticmethod
    def _succeed(*args, **kwargs):
        return {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "这是正常回答"}],
                }
            ],
            "custom_outputs": {"genie_conversation_id": "conv-123"},
        }


def _run_one_turn(question: str, fake_client_factory, session_state: _FakeSessionState, monkeypatch) -> None:
    """模拟一次 Streamlit 重跑：question 是这一轮用户在 chat_input 里"输入"的内容，
    session_state 从上一轮延续过来（同一个对象，不是每轮新建）。"""
    fake_st = _make_fake_streamlit(question, session_state)
    monkeypatch.setitem(sys.modules, "streamlit", fake_st)
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", fake_client_factory)
    monkeypatch.setenv("MODEL_SERVING_ENDPOINT_NAME", "fake-endpoint")

    spec = importlib.util.spec_from_file_location(f"app_under_test_{id(session_state)}", _APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


def test_failed_call_does_not_pollute_history(monkeypatch):
    session_state = _FakeSessionState()

    _run_one_turn("问一个会失败的问题", _FailingClient, session_state, monkeypatch)

    assert session_state.history == [
        {"role": "user", "content": "问一个会失败的问题"}
    ], "失败时不应该往 history 里 append 任何 assistant 消息（更不能是错误堆栈文本）"


def test_history_stays_clean_across_failure_then_success(monkeypatch):
    """先失败一轮，再成功一轮——确认失败没有留下任何痕迹，第二轮的 history 干净地
    只多了这一问一答，不会把第一轮的错误信息当成"之前 agent 说过的话"带给后端。"""
    session_state = _FakeSessionState()

    _run_one_turn("第一句，会失败", _FailingClient, session_state, monkeypatch)
    _run_one_turn("第二句，会成功", _SucceedingClient, session_state, monkeypatch)

    roles_and_content = [(m["role"], m["content"]) for m in session_state.history]
    assert roles_and_content == [
        ("user", "第一句，会失败"),
        ("user", "第二句，会成功"),
        ("assistant", "这是正常回答"),
    ]


def test_successful_call_still_writes_history_and_conversation_id(monkeypatch):
    """反向检查：确认这次修复(try/except/else)没有连成功路径也一起改坏——成功时
    历史和 genie_conversation_id 该写还是要写。"""
    session_state = _FakeSessionState()

    _run_one_turn("正常问题", _SucceedingClient, session_state, monkeypatch)

    assert session_state.history == [
        {"role": "user", "content": "正常问题"},
        {"role": "assistant", "content": "这是正常回答"},
    ]
    assert session_state.genie_conversation_id == "conv-123"
