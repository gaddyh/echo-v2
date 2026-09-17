"""Evaluation harness for the LLM-as-judge (AnalysisJudge).

Tests the judge's alignment with our golden labels — NOT the analyzer's
accuracy. For each eval case:

1. Build a WaitingForMeResult with the **golden expected decision**.
2. Ask the judge: "is this decision correct given the conversation?"
3. Score agreement: 1.0 = judge agrees with golden, 0.0 = judge disagrees.

The analyzer is not involved. We are testing the judge, not the analyzer.

This is NOT part of the normal test suite. It only runs when:
  - ``OPENAI_API_KEY`` is set, AND
  - the ``eval`` marker is selected: ``pytest -m eval -k judge``

Usage::

    # Run the judge eval (requires API access)
    pytest -m eval -k judge -v -s

    # Override judge agreement threshold (default 70%)
    EVAL_JUDGE_MIN_AGREEMENT=0.8 pytest -m eval -k judge -v -s

    # Use a different judge model
    JUDGE_MODEL_NAME=gpt-5.4 pytest -m eval -k judge -v -s
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from echo_v2.domain.waiting_for_me import WaitingForMeDecision, WaitingForMeResult
from echo_v2.services.analysis_judge import AnalysisJudge
from echo_v2.services.chat_analysis_worker import ConversationInput
from tests.evaluation.waiting_for_me_cases import (
    SANITY_CASES,
    SOC_CASES,
    EvalCase,
)
from tests.evaluation.waiting_for_me_soc2508_cases import (
    SOC2508_DEV_CASES,
    SOC2508_TEST_CASES,
)

pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(
        not os.environ.get("OPENAI_API_KEY"),
        reason="OPENAI_API_KEY not set — eval requires real API access",
    ),
]

_SHORT = {
    WaitingForMeDecision.WAITING_FOR_ME: "WFM",
    WaitingForMeDecision.NOT_WAITING_FOR_ME: "NWM",
    WaitingForMeDecision.UNCERTAIN: "UNC",
}


def _build_conversation(case: EvalCase) -> ConversationInput:
    """Convert an EvalCase to a ConversationInput with synthetic timestamps."""
    base = datetime(2026, 9, 5, 10, 0, 0, tzinfo=timezone.utc)
    return ConversationInput(
        user_id="eval-user",
        chat_id="eval-chat@c.us",
        target_version=1,
        messages=[
            (direction, text, base + timedelta(minutes=i))
            for i, (direction, text) in enumerate(case.messages)
        ],
    )


def _golden_result(case: EvalCase) -> WaitingForMeResult:
    """Build a WaitingForMeResult with the golden expected decision."""
    return WaitingForMeResult(
        decision=case.expected,
        confidence=0.95,
        reason=f"(golden) {case.notes or case.description}",
        target_version=1,
    )


@pytest.fixture
def judge():
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
    return AnalysisJudge(
        client=client,
        model=os.environ.get("JUDGE_MODEL_NAME", "gpt-5.4"),
    )


@dataclass
class JudgeCaseResult:
    """Result of running a single case through the judge."""

    case: EvalCase
    judge_score: float | None
    judge_explanation: str
    judge_error: str | None
    latency_ms: float


def _judge_agrees(judge_score: float | None) -> bool:
    """Did the judge agree with the golden label?

    - 1.0 = judge agrees the golden decision is correct.
    - 0.0 = judge disagrees (thinks the golden decision is wrong).
    - 0.5 = neutral (neither agree nor disagree).
    """
    if judge_score is None:
        return False
    return judge_score >= 0.75


def _print_judge_report(
    results: list[JudgeCaseResult],
    label: str,
    judge_agreement: float,
) -> None:
    """Print a rich report for the judge eval.

    Mismatches (judge disagrees with golden) are shown first.
    """
    total = len(results)
    judge_agree = sum(1 for r in results if _judge_agrees(r.judge_score))
    judge_errors = sum(1 for r in results if r.judge_error)
    disagreements = [r for r in results if r.judge_score is not None and r.judge_score <= 0.25]
    neutral = [r for r in results if r.judge_score is not None and 0.25 < r.judge_score < 0.75]

    print("\n" + "=" * 140)
    print(f"  Judge Evaluation Report — {label}")
    print("=" * 140)
    print(f"\n  Total cases:          {total}")
    print(f"  Judge agreement:      {judge_agree}/{total} ({judge_agreement:.1%})")
    print(f"  Judge errors:         {judge_errors}")
    print(f"  Disagreements (0.0):  {len(disagreements)}")
    print(f"  Neutral (0.5):        {len(neutral)}")
    print()

    # Split: mismatches first, then matches.
    mismatches = [r for r in results if not _judge_agrees(r.judge_score)]
    matches = [r for r in results if _judge_agrees(r.judge_score)]

    # Per-case table — mismatches first.
    col_widths = [10, 8, 30, 6, 40]
    headers = ["ID", "Golden", "Description", "Score", "Judge Reason"]
    total_width = sum(col_widths) + len(col_widths) * 3 + 1

    def _print_section(title: str, rows: list[JudgeCaseResult]) -> None:
        if not rows:
            return
        print(f"\n  {title} ({len(rows)})")
        print("  " + "-" * total_width)
        cells = []
        for h, w in zip(headers, col_widths):
            cells.append(f" {h:<{w}} ")
        print("  |" + "|".join(cells) + "|")
        print("  " + "-" * total_width)
        for r in rows:
            golden = _SHORT.get(r.case.expected, "?")
            score = f"{r.judge_score:.1f}" if r.judge_score is not None else "ERR"
            desc = r.case.description[:col_widths[2]]
            j_reason = (r.judge_explanation or r.judge_error or "")[:col_widths[4]]
            cells = [
                f" {r.case.id:<{col_widths[0]}} ",
                f" {golden:<{col_widths[1]}} ",
                f" {desc:<{col_widths[2]}} ",
                f" {score:<{col_widths[3]}} ",
                f" {j_reason:<{col_widths[4]}} ",
            ]
            print("  |" + "|".join(cells) + "|")
        print("  " + "-" * total_width)

    _print_section("MISMATCHES (judge disagrees or neutral)", mismatches)
    _print_section("MATCHES (judge agrees with golden)", matches)
    print()


def _save_judge_run(
    label: str,
    judge_model: str,
    results: list[JudgeCaseResult],
) -> str:
    """Save judge eval results as JSON + Markdown."""
    results_dir = Path(__file__).resolve().parent / "results"
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_label = label.lower().replace(" ", "_")
    run_dir = results_dir / f"{run_id}_judge_{safe_label}"
    run_dir.mkdir(parents=True, exist_ok=True)

    total = len(results)
    judge_agree = sum(1 for r in results if _judge_agrees(r.judge_score))
    disagreements = [r for r in results if r.judge_score is not None and r.judge_score <= 0.25]
    neutral = [r for r in results if r.judge_score is not None and 0.25 < r.judge_score < 0.75]

    json_output = {
        "run_id": run_id,
        "type": "judge_eval",
        "label": label,
        "judge_model": judge_model,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total": total,
            "judge_agreement": judge_agree,
            "judge_agreement_rate": round(judge_agree / total, 4) if total else 0.0,
            "disagreements": len(disagreements),
            "neutral": len(neutral),
        },
        "cases": [
            {
                "id": r.case.id,
                "description": r.case.description,
                "golden": r.case.expected.value,
                "judge_score": r.judge_score,
                "judge_explanation": r.judge_explanation,
                "judge_error": r.judge_error,
                "judge_agrees": _judge_agrees(r.judge_score),
                "latency_ms": r.latency_ms,
                "messages": [
                    {"direction": d, "text": t} for d, t in r.case.messages
                ],
            }
            for r in results
        ],
    }

    json_path = run_dir / "results.json"
    json_path.write_text(
        json.dumps(json_output, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    # Markdown report
    mismatches = [r for r in results if not _judge_agrees(r.judge_score)]
    matches = [r for r in results if _judge_agrees(r.judge_score)]

    md_lines = [
        f"# Judge Evaluation Report — {label}",
        "",
        f"- **Run ID:** `{run_id}`",
        f"- **Judge model:** `{judge_model}`",
        f"- **Timestamp:** {datetime.now(timezone.utc).isoformat()}",
        f"- **Total cases:** {total}",
        f"- **Judge agreement:** {judge_agree}/{total} ({judge_agree/total:.1%})" if total else "- **Judge agreement:** N/A",
        f"- **Disagreements:** {len(disagreements)}",
        f"- **Neutral:** {len(neutral)}",
        "",
    ]

    def _case_table_md(cases: list[JudgeCaseResult]) -> list[str]:
        lines = [
            "| ID | Golden | Description | Score | Judge Reason |",
            "|----|--------|-------------|-------|--------------|",
        ]
        for r in cases:
            golden = _SHORT.get(r.case.expected, "?")
            score = f"{r.judge_score:.1f}" if r.judge_score is not None else "ERR"
            desc = r.case.description.replace("|", "\\|")
            j_reason = (r.judge_explanation or r.judge_error or "").replace("|", "\\|")
            lines.append(f"| {r.case.id} | {golden} | {desc} | {score} | {j_reason} |")
        return lines

    md_lines += ["## Mismatches (judge disagrees or neutral)", ""]
    if mismatches:
        md_lines += _case_table_md(mismatches)
    else:
        md_lines.append("No mismatches — judge agreed with all golden labels.")
    md_lines.append("")

    md_lines += ["## Matches (judge agrees with golden)", ""]
    if matches:
        md_lines += _case_table_md(matches)
    else:
        md_lines.append("No matches.")
    md_lines.append("")

    # Detailed disagreements with full messages.
    md_lines += ["## Disagreements Detail", ""]
    if not disagreements:
        md_lines.append("No disagreements — judge agreed with all golden labels.")
    else:
        for r in disagreements:
            md_lines += [
                f"### {r.case.id} — {r.case.description}",
                f"- Golden: {_SHORT.get(r.case.expected, '?')}",
                f"- Judge score: {r.judge_score}",
                f"- Judge explanation: {r.judge_explanation}",
                "",
                "**Messages:**",
                "",
            ]
            for direction, text in r.case.messages:
                label_d = "them" if direction == "inbound" else "me"
                md_lines.append(f"> **{label_d}:** {text}")
            md_lines.append("")

    (run_dir / "report.md").write_text("\n".join(md_lines), encoding="utf-8")
    return run_id


async def _run_judge_suite(
    judge: AnalysisJudge,
    cases: list[EvalCase],
    label: str,
    min_agreement: float,
) -> None:
    """Run judge on all cases with golden labels, report, save, and assert."""
    results: list[JudgeCaseResult] = []
    for case in cases:
        conv = _build_conversation(case)
        golden = _golden_result(case)
        t0 = time.perf_counter()
        try:
            jr = await judge.judge(conv, golden)
            judge_score = jr.score
            judge_explanation = jr.explanation
            judge_error = None
        except Exception as exc:  # noqa: BLE001 - eval harness must not crash
            judge_score = None
            judge_explanation = ""
            judge_error = str(exc)
        latency_ms = (time.perf_counter() - t0) * 1000
        results.append(JudgeCaseResult(
            case=case,
            judge_score=judge_score,
            judge_explanation=judge_explanation,
            judge_error=judge_error,
            latency_ms=latency_ms,
        ))

    total = len(results)
    judge_agree = sum(1 for r in results if _judge_agrees(r.judge_score))
    judge_agreement = judge_agree / total if total > 0 else 0.0

    _print_judge_report(results, label, judge_agreement)

    judge_model = os.environ.get("JUDGE_MODEL_NAME", "gpt-5.4")
    run_id = _save_judge_run(label, judge_model, results)
    print(f"\n  Results saved: run_id={run_id}")
    print(f"  → tests/evaluation/results/{run_id}_judge_{label.lower().replace(' ', '_')}/")

    assert judge_agreement >= min_agreement, (
        f"{label} judge agreement {judge_agreement:.1%} below threshold {min_agreement:.1%}"
    )


@pytest.mark.eval
async def test_judge_sanity_eval(judge):
    """Run the sanity cases through the judge with golden labels.

    Measures how well the judge agrees with our golden labels on the
    baseline cases. Threshold: 70% (default).
    Override with ``EVAL_JUDGE_MIN_AGREEMENT``.
    """
    min_agreement = float(os.environ.get("EVAL_JUDGE_MIN_AGREEMENT", "0.7"))
    await _run_judge_suite(judge, SANITY_CASES, "Sanity", min_agreement)


@pytest.mark.eval
async def test_judge_soc_eval(judge):
    """Run the SOC cases through the judge with golden labels.

    These are the harder state-transition cases. Threshold: 70%.
    """
    min_agreement = float(os.environ.get("EVAL_JUDGE_SOC_MIN_AGREEMENT", "0.7"))
    await _run_judge_suite(judge, SOC_CASES, "SOC", min_agreement)


@pytest.mark.eval
async def test_judge_all_eval(judge):
    """Run all cases through the judge with golden labels (sanity + SOC)."""
    min_agreement = float(os.environ.get("EVAL_JUDGE_MIN_AGREEMENT", "0.7"))
    all_cases = SANITY_CASES + SOC_CASES
    await _run_judge_suite(judge, all_cases, "All", min_agreement)


@pytest.mark.eval
async def test_judge_soc2508_dev_eval(judge):
    """Run the SOC-2508 dev cases through the judge with golden labels.

    Grounded in the SOC-2508 dataset: long noisy windows, buried
    obligations, base-rate banter negatives, UNCERTAIN labels.
    """
    min_agreement = float(os.environ.get("EVAL_JUDGE_SOC2508_MIN_AGREEMENT", "0.7"))
    await _run_judge_suite(judge, SOC2508_DEV_CASES, "SOC2508-dev", min_agreement)


@pytest.mark.eval
async def test_judge_soc2508_test_eval(judge):
    """Run the SOC-2508 test cases through the judge with golden labels."""
    min_agreement = float(os.environ.get("EVAL_JUDGE_SOC2508_MIN_AGREEMENT", "0.7"))
    await _run_judge_suite(judge, SOC2508_TEST_CASES, "SOC2508-test", min_agreement)


@pytest.mark.eval
async def test_judge_soc2508_all_eval(judge):
    """Run all SOC-2508 cases (dev + test) through the judge."""
    min_agreement = float(os.environ.get("EVAL_JUDGE_SOC2508_MIN_AGREEMENT", "0.7"))
    all_cases = SOC2508_DEV_CASES + SOC2508_TEST_CASES
    await _run_judge_suite(judge, all_cases, "SOC2508-all", min_agreement)
