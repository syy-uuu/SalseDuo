"""自动跑评测集：对 tests/eval/eval_set.json 里的每个问题跑一遍 agent，
记录完整白盒追踪（router 判断、Genie 生成的 SQL、检索到的文档片段）+ 最终回答，
再用 LLM 作为裁判对照 ground_truth 打分，把结果存到 tests/eval/results/ 下。

用法: python -m tests.eval.run_eval
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from src.graph.build_graph import build_graph
from src.graph.llm import get_llm

_EVAL_DIR = Path(__file__).parent
_EVAL_SET_PATH = _EVAL_DIR / "eval_set.json"
_RESULTS_DIR = _EVAL_DIR / "results"

_GRADER_SYSTEM_PROMPT = """\
你是一个严格的评测裁判。给你一个问题、标准答案(ground truth)、评分要点(grading notes)，
以及 agent 的实际回答，请判断 agent 的回答是否正确。

判断标准:
- CORRECT: 关键事实/数字/结论都对，允许合理的措辞、格式、四舍五入差异。
- PARTIALLY_CORRECT: 部分关键点对，但遗漏或搞错了至少一个评分要点里提到的核心信息。
- INCORRECT: 关键结论/数字明显错误，或完全没有回答到点子上。
"""


class Grade(BaseModel):
    verdict: Literal["CORRECT", "PARTIALLY_CORRECT", "INCORRECT"] = Field(description="评分结论")
    reasoning: str = Field(description="打分理由，指出具体哪里对/哪里错")


def _grade(question: str, ground_truth: str, grading_notes: str, agent_answer: str) -> Grade:
    llm = get_llm().with_structured_output(Grade)
    content = (
        f"问题: {question}\n\n"
        f"标准答案: {ground_truth}\n\n"
        f"评分要点: {grading_notes}\n\n"
        f"Agent 实际回答: {agent_answer}"
    )
    return llm.invoke(
        [
            {"role": "system", "content": _GRADER_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ]
    )


def _run_one(graph, case: dict) -> dict:
    question = case["question"]
    try:
        result = graph.invoke(
            {
                "messages": [{"role": "user", "content": question}],
                "user_query": question,
                "loop_count": 0,
            }
        )
        agent_answer = result["messages"][-1]["content"]
        trace = result.get("trace", [])
        loop_count_used = result.get("loop_count", 0)
        error = None
    except Exception as exc:  # noqa: BLE001 - 评测脚本不能因为单个用例崩溃就整体中断
        agent_answer = ""
        trace = []
        loop_count_used = None
        error = f"{type(exc).__name__}: {exc}"

    grade = None
    if error is None:
        try:
            grade_result = _grade(question, case["ground_truth"], case["grading_notes"], agent_answer)
            grade = {"verdict": grade_result.verdict, "reasoning": grade_result.reasoning}
        except Exception as exc:  # noqa: BLE001
            grade = {"verdict": "ERROR", "reasoning": f"评分失败: {exc}"}

    return {
        "id": case["id"],
        "category": case["category"],
        "question": question,
        "ground_truth": case["ground_truth"],
        "grading_notes": case["grading_notes"],
        "agent_answer": agent_answer,
        "loop_count_used": loop_count_used,
        "trace": trace,
        "grade": grade,
        "error": error,
    }


def _summarize(results: list[dict]) -> dict:
    by_category: dict[str, dict[str, int]] = {}
    verdict_counts = {"CORRECT": 0, "PARTIALLY_CORRECT": 0, "INCORRECT": 0, "ERROR": 0}
    for r in results:
        category = r["category"]
        by_category.setdefault(category, {"CORRECT": 0, "PARTIALLY_CORRECT": 0, "INCORRECT": 0, "ERROR": 0})
        verdict = r["grade"]["verdict"] if r["grade"] else "ERROR"
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
        by_category[category][verdict] = by_category[category].get(verdict, 0) + 1
    return {"total": len(results), "overall": verdict_counts, "by_category": by_category}


def main() -> None:
    cases = json.loads(_EVAL_SET_PATH.read_text())
    graph = build_graph()

    results = []
    for i, case in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] 跑 {case['id']}: {case['question'][:40]}...")
        result = _run_one(graph, case)
        verdict = result["grade"]["verdict"] if result["grade"] else "ERROR"
        print(f"    -> {verdict}")
        results.append(result)

    summary = _summarize(results)

    _RESULTS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = _RESULTS_DIR / f"eval_run_{timestamp}.json"
    out_path.write_text(
        json.dumps(
            {"run_timestamp": timestamp, "summary": summary, "results": results},
            indent=2,
            ensure_ascii=False,
        )
    )

    print("\n=== 汇总 ===")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\n完整结果已存到: {out_path}")


if __name__ == "__main__":
    main()
