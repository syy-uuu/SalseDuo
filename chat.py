"""Interactive command-line chat tool for manually exercising/debugging the LangGraph
agent.

This is the fastest way to try the agent by hand before actually deploying the
Databricks App: it connects directly, from your own machine, to the Databricks
workspace configured in your .env (Genie / Vector Search / LLM) — behavior matches the
deployed App, just running in a local terminal instead of a web page.

Usage: python chat.py
Type exit / quit to leave; type /trace to toggle printing the white-box trace for each step.
"""

from __future__ import annotations

import json

from src.graph.build_graph import build_graph


def main() -> None:
    graph = build_graph()
    messages: list[dict] = []
    genie_conversation_id: str | None = None
    show_trace = False

    print("SalesDuo agent ready. Type a question to start (exit to quit, /trace to toggle trace details).\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            print("Bye.")
            break
        if user_input == "/trace":
            show_trace = not show_trace
            print(f"(trace details {'enabled' if show_trace else 'disabled'})\n")
            continue

        messages.append({"role": "user", "content": user_input})
        result = graph.invoke(
            {
                "messages": messages,
                "user_query": user_input,
                "genie_conversation_id": genie_conversation_id,
                "loop_count": 0,
            }
        )
        messages = result["messages"]
        genie_conversation_id = result.get("genie_conversation_id")

        print(f"\nAgent: {messages[-1]['content']}\n")

        if show_trace:
            print("--- trace ---")
            print(json.dumps(result.get("trace", []), indent=2, ensure_ascii=False))
            print("-------------\n")


if __name__ == "__main__":
    main()
