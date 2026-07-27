"""交互式命令行聊天工具，用于人工体验/调试 LangGraph agent。

在真正部署 Databricks App 之前，这是最快的手动体验方式：本机直接连你 .env 里配置的
Databricks workspace（Genie / Vector Search / LLM），跟部署后 App 的效果一致，只是跑在
本地终端而不是网页上。

用法: python chat.py
输入 exit / quit 退出；输入 /trace 切换是否打印每一步的白盒追踪细节。
"""

from __future__ import annotations

import json

from src.graph.build_graph import build_graph


def main() -> None:
    graph = build_graph()
    messages: list[dict] = []
    genie_conversation_id: str | None = None
    show_trace = False

    print("SalesDuo agent 已就绪。输入问题开始对话（exit 退出，/trace 切换追踪详情显示）。\n")

    while True:
        try:
            user_input = input("你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            print("再见。")
            break
        if user_input == "/trace":
            show_trace = not show_trace
            print(f"(追踪详情显示已{'开启' if show_trace else '关闭'})\n")
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
