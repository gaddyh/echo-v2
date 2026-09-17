"""Evaluation harness for the LLM-as-judge (AnalysisJudge).

Runs the labeled cases in :mod:`tests.evaluation.waiting_for_me_cases`
through the analyzer, then through the judge, and reports:

1. **Analyzer accuracy** — how often the analyzer's decision matches
   the expected label (same as the analyzer eval).
2. **Judge agreement** — how often the judge's score (1.0=correct,
   0.0=wrong) agrees with whether the analyzer was actually correct.
3. **Judge calibration** — for cases where the analyzer was wrong, did
   the judge catch it (score=0.0)? For cases where the analyzer was
   right, did the judge confirm it (score=1.0)?

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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from echo_v2.domain.waiting_for_me import WaitingForMeDecision
from echo_v2.services.analysis_judge import AnalysisJudge
from echo_v2.services.chat_analysis_worker import ConversationInput
from echo_v2.services.waiting_for_me_analyzer import (
    AnalysisError,
    LLMWaitingForMeAnalyzer,
)
from tests.evaluation.waiting_for_me_cases import (
    SANITY_CASES,
    SOC_CASES,
    EvalCase,
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


@pytest.fixture
def analyzer():
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
    return LLMWaitingForMeAnalyzer(
        client=client,
        model=os.environ.get("LLM_MODEL_NAME", "gpt-4.1"),
        prompt_version=os.environ.get("WFM_PROMPT_VERSION", "v1"),
    )


@pytest.fixture
def judge():
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
    return AnalysisJudge(
        client=client,
        model=os.environ.get("JUDGE_MODEL_NAME", "gpt-5.4"),
    )


async def _run_judge_case(
    analyzer: LLMWaitingForMeAnalyzer,
    judge: AnalysisJudge,
    case: EvalCase,
) -> JudgeCaseResult:
    """Run a single case through analyzer + judge, return full result."""
    conv = _build_conversation(case)
    t0 = time.perf_counter()

    # Step 1: Run the analyzer.
    try:
        result, _raw = await analyzer.analyze_with_raw(conv)
        analyzer_decision = result.decision
        analyzer_correct = analyzer_decision == case.expected
    except AnalysisError as exc:
        return JudgeCaseResult(
            case=case,
            analyzer_decision=None,
            analyzer_correct=False,
            analyzer_error=str(exc),
            judge_score=None,
            judge_explanation="",
            judge_error="analyzer failed",
            latency_ms=(time.perf_counter() - t0) * 1000,
        )

    # Step 2: Run the judge on the analyzer's output.
    try:
        judge_result = await judge.judge(conv, result)
        judge_score = judge_result.score
        judge_explanation = judge_result.explanation
        judge_error = None
    except Exception as exc:  # noqa: BLE001 - eval harness must not crash
        judge_score = None
        judge_explanation = ""
        judge_error = str(exc)

    latency_ms = (time.perf_counter() - t0) * 1000
    return JudgeCaseResult(
        case=case,
        analyzer_decision=analyzer_decision,
        analyzer_correct=analyzer_correct,
        analyzer_error=None,
        judge_score=judge_score,
        judge_explanation=judge_explanation,
        judge_error=judge_error,
        latency_ms=latency_ms,
    )


from dataclasses import dataclass


@dataclass
class JudgeCaseResult:
    """Result of running a single case through analyzer + judge."""

    case: EvalCase
    analyzer_decision: WaitingForMeDecision | None
    analyzer_correct: bool
    analyzer_error: str | None
    judge_score: float | None
    judge_explanation: str
    judge_error: str | None
    latency_ms: float


def _judge_agrees(judge_score: float | None, analyzer_correct: bool) -> bool:
    """Did the judge agree with ground truth?

    - If analyzer was correct, judge should score 1.0.
    - If analyzer was wrong, judge should score 0.0.
    - 0.5 is neutral (neither agree nor disagree).
    """
    if judge_score is None:
        return False
    if analyzer_correct:
        return judge_score >= 0.75  # 1.0 = agree correct
    else:
        return judge_score <= 0.25  # 0.0 = agree wrong


def _print_judge_report(
    results: list[JudgeCaseResult],
    label: str,
    analyzer_accuracy: float,
    judge_agreement: float,
) -> None:
    """Print a rich report for the judge eval."""
    total = len(results)
    analyzer_correct = sum(1 for r in results if r.analyzer_correct)
    judge_agree = sum(1 for r in results if _judge_agrees(r.judge_score, r.analyzer_correct))
    judge_errors = sum(1 for r in results if r.judge_error)

    # Judge calibration: how many wrong analyzer decisions did the judge catch?
    wrong_cases = [r for r in results if not r.analyzer_correct]
    caught = sum(1 for r in wrong_cases if r.judge_score is not None and r.judge_score <= 0.25)

    # False alarms: judge said wrong when analyzer was actually correct.
    right_cases = [r for r in results if r.analyzer_correct]
    false_alarms = sum(1 for r in right_cases if r.judge_score is not None and r.judge_score <= 0.25)

    print("\n" + "=" * 70)
    print(f"  Judge Evaluation Report — {label}")
    print("=" * 70)
    print(f"\n  Total cases:          {total}")
    print(f"  Analyzer accuracy:    {analyzer_correct}/{total} ({analyzer_accuracy:.1%})")
    print(f"  Judge agreement:      {judge_agree}/{total} ({judge_agreement:.1%})")
    print(f"  Judge errors:         {judge_errors}")
    print()
    print(f"  Calibration (analyzer wrong → judge caught):  {caught}/{len(wrong_cases)}")
    print(f"  False alarms (analyzer right → judge said wrong): {false_alarms}/{len(right_cases)}")
    print()

    # Per-case table
    print("  " + "-" * 100)
    print(f"  {'ID':<12} {'Expected':<6} {'Actual':<6} {'Correct':<8} {'Score':<6} {'Agree':<6} {'Explanation'}")
    print("  " + "-" * 100)
    for r in results:
        expected = _SHORT.get(r.case.expected, "?")
        actual = _SHORT.get(r.analyzer_decision, "ERR")
        correct = "Y" if r.analyzer_correct else "N"
        score = f"{r.judge_score:.1f}" if r.judge_score is not None else "ERR"
        agree = "Y" if _judge_agrees(r.judge_score, r.analyzer_correct) else "N"
        expl = (r.judge_explanation or r.judge_error or "")[:50]
        print(f"  {r.case.id:<12} {expected:<6} {actual:<6} {correct:<8} {score:<6} {agree:<6} {expl}")
    print("  " + "-" * 100)
    print()


def _save_judge_run(
    label: str,
    analyzer_model: str,
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
    analyzer_correct = sum(1 for r in results if r.analyzer_correct)
    judge_agree = sum(1 for r in results if _judge_agrees(r.judge_score, r.analyzer_correct))
    wrong_cases = [r for r in results if not r.analyzer_correct]
    caught = sum(1 for r in wrong_cases if r.judge_score is not None and r.judge_score <= 0.25)
    right_cases = [r for r in results if r.analyzer_correct]
    false_alarms = sum(1 for r in right_cases if r.judge_score is not None and r.judge_score <= 0.25)

    json_output = {
        "run_id": run_id,
        "type": "judge_eval",
        "label": label,
        "analyzer_model": analyzer_model,
        "judge_model": judge_model,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total": total,
            "analyzer_correct": analyzer_correct,
            "analyzer_accuracy": round(analyzer_correct / total, 4) if total else 0.0,
            "judge_agreement": judge_agree,
            "judge_agreement_rate": round(judge_agree / total, 4) if total else 0.0,
            "wrong_cases": len(wrong_cases),
            "caught_wrong": caught,
            "false_alarms": false_alarms,
        },
        "cases": [
            {
                "id": r.case.id,
                "description": r.case.description,
                "expected": r.case.expected.value,
                "actual": r.analyzer_decision.value if r.analyzer_decision else None,
                "analyzer_correct": r.analyzer_correct,
                "analyzer_error": r.analyzer_error,
                "judge_score": r.judge_score,
                "judge_explanation": r.judge_explanation,
                "judge_error": r.judge_error,
                "judge_agrees": _judge_agrees(r.judge_score, r.analyzer_correct),
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
    md_lines = [
        f"# Judge Evaluation Report — {label}",
        "",
        f"- **Run ID:** `{run_id}`",
        f"- **Analyzer model:** `{analyzer_model}`",
        f"- **Judge model:** `{judge_model}`",
        f"- **Timestamp:** {datetime.now(timezone.utc).isoformat()}",
        f"- **Total cases:** {total}",
        f"- **Analyzer accuracy:** {analyzer_correct}/{total} ({analyzer_correct/total:.1%})" if total else "- **Analyzer accuracy:** N/A",
        f"- **Judge agreement:** {judge_agree}/{total} ({judge_agree/total:.1%})" if total else "- **Judge agreement:** N/A",
        f"- **Wrong cases caught by judge:** {caught}/{len(wrong_cases)}",
        f"- **False alarms:** {false_alarms}/{len(right_cases)}",
        "",
        "## Per-Case Results",
        "",
        "| ID | Expected | Actual | Correct | Score | Agree | Explanation |",
        "|----|----------|--------|---------|-------|-------|-------------|",
    ]
    for r in results:
        expected = _SHORT.get(r.case.expected, "?")
        actual = _SHORT.get(r.analyzer_decision, "ERR")
        correct = "Y" if r.analyzer_correct else "N"
        score = f"{r.judge_score:.1f}" if r.judge_score is not None else "ERR"
        agree = "Y" if _judge_agrees(r.judge_score, r.analyzer_correct) else "N"
        expl = (r.judge_explanation or r.judge_error or "").replace("|", "\\|")
        md_lines.append(f"| {r.case.id} | {expected} | {actual} | {correct} | {score} | {agree} | {expl} |")

    md_lines += [
        "",
        "## Disagreements (judge disagreed with ground truth)",
        "",
    ]
    disagreements = [r for r in results if not _judge_agrees(r.judge_score, r.analyzer_correct)]
    if not disagreements:
        md_lines.append("No disagreements — judge agreed with all ground truth labels.")
    else:
        for r in disagreements:
            md_lines += [
                f"### {r.case.id} — {r.case.description}",
                f"- Expected: {_SHORT.get(r.case.expected, '?')}",
                f"- Analyzer said: {_SHORT.get(r.analyzer_decision, 'ERR')}",
                f"- Analyzer correct: {r.analyzer_correct}",
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
    analyzer: LLMWaitingForMeAnalyzer,
    judge: AnalysisJudge,
    cases: list[EvalCase],
    label: str,
    min_agreement: float,
) -> None:
    """Run analyzer + judge on all cases, report, save, and assert."""
    results: list[JudgeCaseResult] = []
    for case in cases:
        r = await _run_judge_case(analyzer, judge, case)
        results.append(r)

    total = len(results)
    analyzer_correct = sum(1 for r in results if r.analyzer_correct)
    judge_agree = sum(1 for r in results if _judge_agrees(r.judge_score, r.analyzer_correct))
    analyzer_accuracy = analyzer_correct / total if total > 0 else 0.0
    judge_agreement = judge_agree / total if total > 0 else 0.0

    _print_judge_report(results, label, analyzer_accuracy, judge_agreement)

    analyzer_model = os.environ.get("LLM_MODEL_NAME", "gpt-4.1")
    judge_model = os.environ.get("JUDGE_MODEL_NAME", "gpt-5.4")
    run_id = _save_judge_run(label, analyzer_model, judge_model, results)
    print(f"\n  Results saved: run_id={run_id}")
    print(f"  → tests/evaluation/results/{run_id}_judge_{label.lower().replace(' ', '_')}/")

    assert judge_agreement >= min_agreement, (
        f"{label} judge agreement {judge_agreement:.1%} below threshold {min_agreement:.1%}"
    )


@pytest.mark.eval
async def test_judge_sanity_eval(analyzer, judge):
    """Run the sanity cases through analyzer + judge.

    Measures how well the judge agrees with ground truth on the
    baseline cases. Threshold: 70% (default).
    Override with ``EVAL_JUDGE_MIN_AGREEMENT``.
    """
    min_agreement = float(os.environ.get("EVAL_JUDGE_MIN_AGREEMENT", "0.7"))
    await _run_judge_suite(analyzer, judge, SANITY_CASES, "Sanity", min_agreement)


@pytest.mark.eval
async def test_judge_soc_eval(analyzer, judge):
    """Run the SOC cases through analyzer + judge.

    These are the harder state-transition cases. Threshold: 70%.
    """
    min_agreement = float(os.environ.get("EVAL_JUDGE_SOC_MIN_AGREEMENT", "0.7"))
    await _run_judge_suite(analyzer, judge, SOC_CASES, "SOC", min_agreement)


@pytest.mark.eval
async def test_judge_all_eval(analyzer, judge):
    """Run all cases through analyzer + judge (sanity + SOC)."""
    min_agreement = float(os.environ.get("EVAL_JUDGE_MIN_AGREEMENT", "0.7"))
    all_cases = SANITY_CASES + SOC_CASES
    await _run_judge_suite(analyzer, judge, all_cases, "All", min_agreement)
