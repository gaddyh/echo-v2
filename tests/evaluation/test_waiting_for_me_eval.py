"""Evaluation harness for the WaitingForMe analyzer.

Runs the labeled cases in :mod:`tests.evaluation.waiting_for_me_cases`
against the real LLM API and reports accuracy, confusion matrix, and
per-case results.

This is NOT part of the normal test suite. It only runs when:
  - ``OPENAI_API_KEY`` is set, AND
  - the ``eval`` marker is selected: ``pytest -m eval``

Usage::

    # Run the full evaluation
    pytest -m eval -v -s

    # Run only WAITING_FOR_ME cases
    pytest -m eval -v -s -k wfm

    # Override accuracy threshold (default 70%)
    EVAL_MIN_ACCURACY=0.8 pytest -m eval -v -s

The harness asserts a minimum accuracy threshold but does NOT assert
per-case pass/fail (LLMs are non-deterministic). The printed report shows
which cases the LLM got right or wrong.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.domain.waiting_for_me import WaitingForMeDecision
from echo_v2.services.chat_analysis_worker import ConversationInput
from echo_v2.services.waiting_for_me_analyzer import (
    AnalysisError,
    LLMWaitingForMeAnalyzer,
)
from echo_v2.services.waiting_for_me_prompts import DEFAULT_PROMPT_VERSION
from tests.evaluation.eval_results import CaseResult, save_eval_run
from tests.evaluation.waiting_for_me_cases import (
    EVAL_CASES,
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

_LABELS = [
    WaitingForMeDecision.WAITING_FOR_ME,
    WaitingForMeDecision.NOT_WAITING_FOR_ME,
    WaitingForMeDecision.UNCERTAIN,
]

# Short labels for table display.
_SHORT = {
    WaitingForMeDecision.WAITING_FOR_ME: "WFM",
    WaitingForMeDecision.NOT_WAITING_FOR_ME: "NWM",
    WaitingForMeDecision.UNCERTAIN: "UNC",
}

# Box-drawing characters for rich tables.
_BOX = {
    "tl": "┌", "tr": "┐", "bl": "└", "br": "┘",
    "h": "─", "v": "│",
    "lt": "├", "rt": "┤", "tt": "┬", "bt": "┴",
    "cross": "┼",
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
def analyzer() -> LLMWaitingForMeAnalyzer:
    from openai import AsyncOpenAI

    from echo_v2.services.summary_rewriter import SummaryRewriter

    client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
    rewriter_model = os.environ.get(
        "SUMMARY_REWRITER_MODEL",
        os.environ.get("LLM_MODEL_NAME", "gpt-4.1"),
    )
    summary_rewriter: SummaryRewriter | None = None
    if os.environ.get("SUMMARY_REWRITER_ENABLED", "1") != "0":
        summary_rewriter = SummaryRewriter(client=client, model=rewriter_model)
    return LLMWaitingForMeAnalyzer(
        client=client,
        model=os.environ.get("LLM_MODEL_NAME", "gpt-4.1"),
        prompt_version=os.environ.get("WFM_PROMPT_VERSION", DEFAULT_PROMPT_VERSION),
        summary_rewriter=summary_rewriter,
    )


async def _run_case(
    analyzer: LLMWaitingForMeAnalyzer, case: EvalCase
) -> CaseResult:
    """Run a single case, return a CaseResult with raw LLM response."""
    conv = _build_conversation(case)
    t0 = time.perf_counter()
    try:
        result, raw = await analyzer.analyze_with_raw(conv)
        latency_ms = (time.perf_counter() - t0) * 1000
        return CaseResult(
            case=case,
            actual=result.decision,
            raw_response=raw,
            latency_ms=latency_ms,
        )
    except AnalysisError as exc:
        latency_ms = (time.perf_counter() - t0) * 1000
        return CaseResult(
            case=case,
            actual=None,
            error=str(exc),
            latency_ms=latency_ms,
        )


def _print_summary_table(results, correct, errors, accuracy):
    """Print the top-level summary table."""
    total = len(results)
    wfm_total = sum(1 for r in results if r[0].expected == WaitingForMeDecision.WAITING_FOR_ME)
    nwm_total = sum(1 for r in results if r[0].expected == WaitingForMeDecision.NOT_WAITING_FOR_ME)
    unc_total = sum(1 for r in results if r[0].expected == WaitingForMeDecision.UNCERTAIN)

    wfm_correct = sum(1 for r in results if r[0].expected == WaitingForMeDecision.WAITING_FOR_ME and r[1] == r[0].expected)
    nwm_correct = sum(1 for r in results if r[0].expected == WaitingForMeDecision.NOT_WAITING_FOR_ME and r[1] == r[0].expected)
    unc_correct = sum(1 for r in results if r[0].expected == WaitingForMeDecision.UNCERTAIN and r[1] == r[0].expected)

    print()
    _print_table_header("Evaluation Summary", ["Metric", "Value"])
    rows = [
        ("Total cases", str(total)),
        ("Correct", str(correct)),
        ("Errors", str(errors)),
        ("Accuracy", f"{accuracy:.1%}"),
        ("", ""),
        ("WAITING_FOR_ME", f"{wfm_correct}/{wfm_total} ({wfm_correct/wfm_total:.0%})" if wfm_total else "0/0"),
        ("NOT_WAITING_FOR_ME", f"{nwm_correct}/{nwm_total} ({nwm_correct/nwm_total:.0%})" if nwm_total else "0/0"),
        ("UNCERTAIN", f"{unc_correct}/{unc_total} ({unc_correct/unc_total:.0%})" if unc_total else "0/0"),
    ]
    _print_table_rows(rows)
    print()


def _print_confusion_matrix(matrix):
    """Print the confusion matrix as a rich table."""
    print()
    _print_table_header("Confusion Matrix (rows=expected, cols=actual)", ["Expected \\ Actual"] + [_SHORT[d] for d in _LABELS])
    for expected in _LABELS:
        row = [_SHORT[expected]]
        for actual in _LABELS:
            count = matrix.get((expected, actual), 0)
            row.append(str(count) if count else "·")
        _print_table_rows([row])
    print()


def _print_per_case_table(results):
    """Print per-case results as a rich table."""
    print()
    _print_table_header("Per-Case Results", ["Status", "ID", "Msgs", "Expected", "Actual", "Description"])
    rows = []
    for case, actual, error in results:
        if error:
            status = "ERR"
            actual_str = "ERROR"
        elif actual == case.expected:
            status = "PASS"
            actual_str = _SHORT.get(actual, "?")
        else:
            status = "FAIL"
            actual_str = _SHORT.get(actual, "?")
        rows.append((status, case.id, str(len(case.messages)), _SHORT[case.expected], actual_str, case.description[:50]))
    _print_table_rows(rows)
    print()


def _print_failures_table(results):
    """Print only failed/errored cases with full detail."""
    failures = [(c, a, e) for c, a, e in results if a != c.expected or e]
    if not failures:
        print("\n  No failures! All cases passed.\n")
        return

    print()
    _print_table_header("Failures Detail", ["ID", "Expected", "Actual", "Messages"])
    rows = []
    for case, actual, error in failures:
        if error:
            actual_str = f"ERROR: {error[:40]}"
        else:
            actual_str = _SHORT.get(actual, "?")
        # Show last 3 messages for context.
        last_msgs = case.messages[-3:]
        msg_summary = " | ".join(f"{d[0][:3]}:{d[1][:30]}" for d in last_msgs)
        rows.append((case.id, _SHORT[case.expected], actual_str, msg_summary[:80]))
    _print_table_rows(rows)
    print()


def _print_table_header(title, headers):
    """Print a table header with title and column headers."""
    print(f"\n  {title}")
    print()

    # Calculate column widths.
    col_widths = []
    for h in headers:
        col_widths.append(max(len(h), 8))

    # Print top border.
    parts = [_BOX["tl"]]
    for w in col_widths:
        parts.append(_BOX["h"] * (w + 2))
        parts.append(_BOX["tt"])
    parts[-1] = _BOX["tr"]
    print("  " + "".join(parts))

    # Print header row.
    cells = []
    for h, w in zip(headers, col_widths):
        cells.append(f" {h:<{w}} ")
    print("  " + _BOX["v"] + _BOX["v"].join(cells) + _BOX["v"])

    # Print separator.
    parts = [_BOX["lt"]]
    for w in col_widths:
        parts.append(_BOX["h"] * (w + 2))
        parts.append(_BOX["cross"])
    parts[-1] = _BOX["rt"]
    print("  " + "".join(parts))

    # Store col_widths for _print_table_rows.
    _print_table_header._col_widths = col_widths


def _print_table_rows(rows):
    """Print table rows with borders."""
    col_widths = _print_table_header._col_widths

    for row in rows:
        cells = []
        for val, w in zip(row, col_widths):
            cells.append(f" {val!s:<{w}} ")
        print("  " + _BOX["v"] + _BOX["v"].join(cells) + _BOX["v"])

    # Print bottom border.
    parts = [_BOX["bl"]]
    for w in col_widths:
        parts.append(_BOX["h"] * (w + 2))
        parts.append(_BOX["bt"])
    parts[-1] = _BOX["br"]
    print("  " + "".join(parts))


async def _run_eval_suite(
    analyzer: LLMWaitingForMeAnalyzer,
    cases: list[EvalCase],
    label: str,
    min_accuracy: float,
) -> None:
    """Run a set of eval cases, print a rich report, save results, and assert accuracy."""
    case_results: list[CaseResult] = []
    for case in cases:
        cr = await _run_case(analyzer, case)
        case_results.append(cr)

    # Build confusion matrix.
    matrix: dict[tuple[WaitingForMeDecision, WaitingForMeDecision], int] = {}
    correct = 0
    errors = 0
    for cr in case_results:
        if cr.error or cr.actual is None:
            errors += 1
            continue
        key = (cr.case.expected, cr.actual)
        matrix[key] = matrix.get(key, 0) + 1
        if cr.actual == cr.case.expected:
            correct += 1

    total = len(case_results)
    accuracy = correct / total if total > 0 else 0.0

    # Print rich report.
    print("\n" + "=" * 70)
    print(f"  WaitingForMe Evaluation Report — {label}")
    print("=" * 70)

    # Convert to tuples for the print functions (which expect (case, actual, error)).
    tuple_results = [(cr.case, cr.actual, cr.error) for cr in case_results]
    _print_summary_table(tuple_results, correct, errors, accuracy)
    _print_confusion_matrix(matrix)
    _print_per_case_table(tuple_results)
    _print_failures_table(tuple_results)

    # Save results to disk.
    model = os.environ.get("LLM_MODEL_NAME", "gpt-4.1")
    prompt_version = os.environ.get("WFM_PROMPT_VERSION", DEFAULT_PROMPT_VERSION)
    run_id = save_eval_run(
        label, model, case_results, prompt_version=prompt_version,
    )
    print(f"\n  Results saved: run_id={run_id} (prompt={prompt_version})")
    print(f"  → tests/evaluation/results/{run_id}_{label.lower().replace(' ', '_')}/")

    assert accuracy >= min_accuracy, (
        f"{label} accuracy {accuracy:.1%} below threshold {min_accuracy:.1%}"
    )


@pytest.mark.eval
async def test_sanity_eval(analyzer):
    """Run the sanity (baseline/regression) cases.

    These are the original 40 easy cases: direct questions, closings,
    and simple context. They serve as a regression baseline — a drop
    here signals a fundamental regression.

    Threshold: 70% (default). Override with ``EVAL_SANITY_MIN_ACCURACY``.
    """
    min_accuracy = float(os.environ.get("EVAL_SANITY_MIN_ACCURACY", "0.7"))
    await _run_eval_suite(analyzer, SANITY_CASES, "Sanity", min_accuracy)


@pytest.mark.eval
async def test_soc_eval(analyzer):
    """Run the SOC (state-transition / open-commitment) cases.

    These 9 cases test the edge that matters for Echo: tracking an
    obligation across turns rather than classifying only the final
    message. This is the harder set and the primary quality signal.

    Threshold: 70% (default). Override with ``EVAL_SOC_MIN_ACCURACY``.
    """
    min_accuracy = float(os.environ.get("EVAL_SOC_MIN_ACCURACY", "0.7"))
    await _run_eval_suite(analyzer, SOC_CASES, "SOC", min_accuracy)


@pytest.mark.eval
async def test_evaluation_summary(analyzer):
    """Run all eval cases (sanity + SOC) and report overall accuracy.

    Kept for backward compatibility. The sanity and SOC tests above
    give finer-grained signal.
    """
    min_accuracy = float(os.environ.get("EVAL_MIN_ACCURACY", "0.7"))
    await _run_eval_suite(analyzer, EVAL_CASES, "All", min_accuracy)
