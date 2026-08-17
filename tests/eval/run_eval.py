"""Runs the evaluation set automatically: runs the agent once per question in
tests/eval/eval_set.json, recording the full white-box trace (router decisions, SQL
Genie generated, retrieved document chunks) plus the final answer, then uses an LLM as
judge to grade each answer against its ground_truth, saving the results under
tests/eval/results/.

Usage: python -m tests.eval.run_eval
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from src.clients.llm import get_llm
from src.graph.build_graph import build_graph
from prompts.loader import render_prompt

_EVAL_DIR = Path(__file__).parent
_EVAL_SET_PATH = _EVAL_DIR / "eval_set.json"
_RESULTS_DIR = _EVAL_DIR / "results"

_GRADER_SYSTEM_PROMPT = render_prompt("eval_grader")


class Grade(BaseModel):
    verdict: Literal["CORRECT", "PARTIALLY_CORRECT", "INCORRECT"] = Field(description="the grading verdict")
    reasoning: str = Field(description="the rationale for this grade, pointing out specifically what's right/wrong")


def _grade(question: str, ground_truth: str, grading_notes: str, agent_answer: str) -> Grade:
    llm = get_llm().with_structured_output(Grade)
    content = (
        f"Question: {question}\n\n"
        f"Ground truth: {ground_truth}\n\n"
        f"Grading notes: {grading_notes}\n\n"
        f"Agent's actual answer: {agent_answer}"
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
    except Exception as exc:  # noqa: BLE001 - the eval script shouldn't abort entirely just because one case crashed
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
            grade = {"verdict": "ERROR", "reasoning": f"grading failed: {exc}"}

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
        print(f"[{i}/{len(cases)}] running {case['id']}: {case['question'][:40]}...")
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

    print("\n=== summary ===")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\nFull results saved to: {out_path}")


if __name__ == "__main__":
    main()
