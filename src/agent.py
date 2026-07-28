"""对外唯一契约：MLflow ResponsesAgent 包装。Databricks Apps 聊天框只认这个接口。

内部调用 LangGraph 的 graph.invoke()（predict）/复用同一次 invoke 结果做伪流式输出
（predict_stream）。我们的节点（router 的 LLM 判断、Genie 查询、Vector Search 检索）
不是逐 token 产生的，所以 predict_stream 不做逐 token 真流式，而是等 graph 跑完后，
把最终文本按一次性的 delta + done 事件序列输出——这仍然符合 ResponsesAgent 的流式契约，
且不需要为了"看起来是流式"而拆分 LangGraph 内部无法真正增量产出的结果。
"""

from __future__ import annotations

import uuid
from typing import Generator

import mlflow
from mlflow.pyfunc import ResponsesAgent
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentResponse,
    ResponsesAgentStreamEvent,
)

from src.config import settings
from src.graph.build_graph import build_graph

mlflow.langchain.autolog()


class SalesDuoResponsesAgent(ResponsesAgent):
    def __init__(self) -> None:
        self._graph = build_graph()

    def _run_graph(self, request: ResponsesAgentRequest) -> dict:
        messages = [
            {"role": item.role, "content": item.content}
            for item in request.input
            if hasattr(item, "role") and hasattr(item, "content")
        ]
        user_query = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )
        # 跨轮对话记忆：messages 里"当前这句之前"的历史由调用方（app.py）在 request.input
        # 里整段发过来（见 src/graph/state.py::recent_history_text，router/finalize 从
        # messages 里取历史，不是从这里单独传）。genie_conversation_id 不属于标准的
        # messages 格式，走 custom_inputs 单独传——调用方把上一轮 custom_outputs 里收到的
        # 值原样带回来，用来复用同一个 Genie 会话，不让每句新消息都变成 Genie 的新会话。
        custom_inputs = request.custom_inputs or {}
        initial_state = {
            "messages": messages,
            "user_query": user_query,
            "loop_count": 0,
            "genie_conversation_id": custom_inputs.get("genie_conversation_id"),
        }
        return self._graph.invoke(initial_state)

    @staticmethod
    def _custom_outputs(result: dict) -> dict:
        return {
            "trace": result.get("trace", []),
            "genie_conversation_id": result.get("genie_conversation_id"),
        }

    @staticmethod
    def _final_text(result: dict) -> str:
        final_messages = result.get("messages", [])
        if final_messages and final_messages[-1]["role"] == "assistant":
            return final_messages[-1]["content"]
        return result.get("structured_result") or result.get("credit_info") or "(无回答)"

    def predict(self, request: ResponsesAgentRequest) -> ResponsesAgentResponse:
        result = self._run_graph(request)
        final_text = self._final_text(result)
        output_item = self.create_text_output_item(text=final_text, id=str(uuid.uuid4()))
        # custom_outputs 里带上完整白盒 trace（router 判断、Genie SQL、检索片段）——
        # 部署到 Serving 之后，本地就没法直接看 graph.invoke() 的返回值了，这是唯一能
        # 在线上环境追查"这次为什么没查到数据"这类问题的办法，不是可有可无的调试糖。
        # 同时带上 genie_conversation_id，供调用方下一轮通过 custom_inputs 传回来延续会话。
        return ResponsesAgentResponse(
            output=[output_item], custom_outputs=self._custom_outputs(result)
        )

    def predict_stream(
        self, request: ResponsesAgentRequest
    ) -> Generator[ResponsesAgentStreamEvent, None, None]:
        result = self._run_graph(request)
        final_text = self._final_text(result)
        item_id = str(uuid.uuid4())
        yield ResponsesAgentStreamEvent(**self.create_text_delta(final_text, item_id))
        yield ResponsesAgentStreamEvent(
            type="response.output_item.done",
            item=self.create_text_output_item(text=final_text, id=item_id),
            custom_outputs=self._custom_outputs(result),
        )


mlflow.models.set_model(SalesDuoResponsesAgent())


def get_agent() -> SalesDuoResponsesAgent:
    return SalesDuoResponsesAgent()


if __name__ == "__main__":
    settings.require("genie_space_id", "vector_search_index")
