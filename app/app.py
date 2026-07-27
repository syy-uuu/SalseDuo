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

st.set_page_config(page_title="SalesDuo", page_icon="🚲")
st.title("SalesDuo · AdventureWorks 业务问答助手")

if "history" not in st.session_state:
    st.session_state.history = []


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


def ask(question: str) -> str:
    # 用底层 REST 调用而不是 SDK 的 serving_endpoints.query()：后者是给通用
    # chat/completions/embeddings 端点设计的类型化封装，不认识我们这个自定义
    # ResponsesAgent 返回的 "output" 字段，.as_dict() 会把它丢掉。直接打
    # /invocations 拿原始 JSON，才能拿到完整的 output/content/output_text。
    client = _client()
    raw = client.api_client.do(
        "POST",
        f"/serving-endpoints/{MODEL_SERVING_ENDPOINT_NAME}/invocations",
        body={"input": [{"role": "user", "content": question}]},
    )
    return _extract_text(raw)


for turn in st.session_state.history:
    with st.chat_message(turn["role"]):
        st.markdown(turn["content"])

question = st.chat_input("问点什么，比如：Tier 2 客户的信用额度上限是多少？")
if question:
    st.session_state.history.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        with st.spinner("正在查询…"):
            try:
                answer = ask(question)
            except Exception as exc:  # noqa: BLE001 - 聊天框需要把后端错误直接展示给用户
                answer = f"调用后端出错: {exc}"
        st.markdown(answer)
    st.session_state.history.append({"role": "assistant", "content": answer})
