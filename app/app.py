"""Databricks Apps 入口：一个极简聊天框 UI，调用已部署的 Model Serving Endpoint
（承载 src/agent.py 里的 LangGraph ResponsesAgent）。

用 Streamlit 自带的 st.chat_message / st.chat_input 搭聊天框骨架，不额外引入
自定义前端框架。Databricks Apps 运行时自带的 WorkspaceClient() 默认凭据链
（App 的 service principal）足以调用同一 workspace 里的 serving endpoint，
不需要在这里单独处理 DATABRICKS_HOST/TOKEN。
"""

from __future__ import annotations

import os

import streamlit as st
from databricks.sdk import WorkspaceClient

MODEL_SERVING_ENDPOINT_NAME = os.environ["MODEL_SERVING_ENDPOINT_NAME"]
# 历史消息全量带回后端，长度不设上限的话会让请求体和 prompt 无限变长（同一个问题在
# DEVELOPMENT_JOURNAL Part 4 第 8 条里也提到过），这里做个简单截断：只保留最近这么多条。
_MAX_HISTORY_MESSAGES = 20

st.set_page_config(page_title="SalesDuo", page_icon="🚲")
st.title("SalesDuo · AdventureWorks 业务问答助手")

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
    return "(未能解析出回答，原始响应见日志)"


def ask(messages: list[dict]) -> tuple[str, str | None]:
    # 用底层 REST 调用而不是 SDK 的 serving_endpoints.query()：后者是给通用
    # chat/completions/embeddings 端点设计的类型化封装，不认识我们这个自定义
    # ResponsesAgent 返回的 "output" 字段，.as_dict() 会把它丢掉。直接打
    # /invocations 拿原始 JSON，才能拿到完整的 output/content/output_text。
    #
    # messages 传的是完整对话历史（含当前这句，调用方已经 append 过了），不是只有当前
    # 这一句——router/finalize 靠这个历史理解"这个客户""刚才那个额度"之类的指代
    # （见 src/graph/state.py::recent_history_text）。genie_conversation_id 走
    # custom_inputs/custom_outputs 单独透传，不属于标准 messages 格式。
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

question = st.chat_input("问点什么，比如：Tier 2 客户的信用额度上限是多少？")
if question:
    st.session_state.history.append({"role": "user", "content": question})
    st.session_state.history = st.session_state.history[-_MAX_HISTORY_MESSAGES:]
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        with st.spinner("正在查询…"):
            try:
                answer, genie_conversation_id = ask(st.session_state.history)
                st.session_state.genie_conversation_id = genie_conversation_id
            except Exception as exc:  # noqa: BLE001 - 聊天框需要把后端错误直接展示给用户
                # 失败时只在本次渲染里提示，不写进 history——history 会被发回后端当上下文，
                # 一条"调用后端出错: ConnectionError(...)"混进去会被 router/finalize 当成
                # "之前 agent 说过的话"去理解，产生不可预测的干扰（见
                # docs/CODE_REVIEW_FINDINGS.md 第 8 条）。
                st.error(f"调用后端出错: {exc}")
            else:
                st.markdown(answer)
                st.session_state.history.append({"role": "assistant", "content": answer})
