"""The single external contract: an MLflow ResponsesAgent wrapper. This is the only
interface the Databricks Apps chat frontend knows about.

Internally calls LangGraph's graph.invoke() (predict) / reuses the same invoke() result
to produce pseudo-streaming output (predict_stream). Our nodes (the router's LLM
judgment, Genie queries, Vector Search retrieval) don't produce output token by token, so
predict_stream doesn't attempt real token-by-token streaming — instead, once the graph
finishes running, it emits the final text as a one-shot delta + done event sequence.
This still satisfies the ResponsesAgent streaming contract, and avoids artificially
chopping up a result that LangGraph's internals can't actually produce incrementally,
just to "look like" streaming.
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
        # Cross-turn conversation memory: history "before the current message" is sent
        # in full by the caller (app.py) as part of request.input (see
        # src/graph/state.py::recent_history_text — router/finalize pull history from
        # messages, it isn't passed separately here). genie_conversation_id isn't part of
        # the standard messages format, so it travels separately via custom_inputs — the
        # caller passes back whatever value it received in the previous turn's
        # custom_outputs, so the same Genie conversation is reused instead of every new
        # message starting a fresh Genie session.
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
        return result.get("structured_result") or result.get("credit_info") or "(no answer)"

    def predict(self, request: ResponsesAgentRequest) -> ResponsesAgentResponse:
        result = self._run_graph(request)
        final_text = self._final_text(result)
        output_item = self.create_text_output_item(text=final_text, id=str(uuid.uuid4()))
        # custom_outputs carries the full white-box trace (router decisions, Genie SQL,
        # retrieved chunks) — once this is deployed to Serving there's no way to
        # directly inspect graph.invoke()'s return value locally anymore, so this is the
        # only way to debug "why didn't this query find any data" once it's live. Not an
        # optional debugging nicety. Also carries genie_conversation_id, so the caller
        # can pass it back next turn via custom_inputs to continue the same conversation.
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
