"""Evaluation harness for the casual-question cases.

Runs the labeled cases in
:mod:`tests.evaluation.waiting_for_me_casual_cases` against the real LLM
API and reports accuracy, confusion matrix, and per-case results — same
rich output as the other eval harnesses.

This is NOT part of the normal test suite. It only runs when:
  - ``OPENAI_API_KEY`` is set, AND
  - the ``eval_casual`` marker is selected: ``pytest -m eval_casual -v -s``

This suite encodes **desired product behavior** (casual social questions
→ NOT_WAITING_FOR_ME, actionable asks → WAITING_FOR_ME) that the current
prompt does NOT implement. Both splits run without an accuracy threshold:
this is a measurement instrument, not a regression gate. Once we adopt
the semantics and change the prompt, the TEST split can become a gate.

Usage::

    # Run the casual-question evaluation
    pytest -m eval_casual -v -s

    # Run only the DEV split
    pytest -m eval_casual -v -s -k casual_dev

    # Run only the TEST split
    pytest -m eval_casual -v -s -k casual_test
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
from tests.evaluation.waiting_for_me_cases import EvalCase
from tests.evaluation.waiting_for_me_casual_cases import (
    CASUAL_DEV_CASES,
    CASUAL_TEST_CASES,
)

pytestmark = [
    pytest.mark.eval_casual,
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

    client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
    return LLMWaitingForMeAnalyzer(
        client=client,
        model=os.environ.get("LLM_MODEL_NAME", "gpt-4.1"),
        prompt_version=os.environ.get("WFM_PROMPT_VERSION", DEFAULT_PROMPT_VERSION),
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


async def _run_split(
    analyzer: LLMWaitingForMeAnalyzer,
    cases: list[EvalCase],
    label: str,
    min_accuracy: float | None,
) -> None:
    """Run a split of casual-question cases, print a rich report, save
    results, and optionally assert accuracy.

    Args:
        analyzer: The LLM analyzer fixture.
        cases: The cases to run.
        label: Human-readable label for the report and saved files.
        min_accuracy: If not ``None``, assert accuracy >= this threshold.
            Use ``None`` for measurement-only splits (no assertion).
    """
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

    tuple_results = [(cr.case, cr.actual, cr.error) for cr in case_results]
    _print_summary_table(tuple_results, correct, errors, accuracy)
    _print_confusion_matrix(matrix)
    _print_per_case_table(tuple_results)
    _print_failures_table(tuple_results)

    # Save results to disk.
    model = os.environ.get("LLM_MODEL_NAME", "gpt-4.1")
    prompt_version = os.environ.get("WFM_PROMPT_VERSION", DEFAULT_PROMPT_VERSION)
    safe_label = label.lower().replace(" ", "_")
    run_id = save_eval_run(
        label, model, case_results, prompt_version=prompt_version,
    )
    print(f"\n  Results saved: run_id={run_id} (prompt={prompt_version})")
    print(f"  → tests/evaluation/results/{run_id}_{safe_label}/")

    if min_accuracy is not None:
        assert accuracy >= min_accuracy, (
            f"{label} accuracy {accuracy:.1%} below threshold {min_accuracy:.1%}"
        )


@pytest.mark.eval_casual
async def test_casual_dev_eval(analyzer):
    """Run the casual-question DEV split (6 cases).

    Measurement instrument for the actionability / responsibility threshold
    boundary. No accuracy threshold is asserted — this suite encodes desired
    product behavior the current prompt does not implement yet. The
    expected gap (casual questions labeled WFM by the model, NWM by this
    suite) is the signal we want to collect.

    Run with::

        pytest -m eval_casual -v -s -k casual_dev
    """
    await _run_split(analyzer, CASUAL_DEV_CASES, "Casual Dev", min_accuracy=None)


@pytest.mark.eval_casual
async def test_casual_test_eval(analyzer):
    """Run the casual-question TEST split (6 cases).

    Measurement instrument — no accuracy threshold is asserted. Once we
    adopt the actionability semantics and change the prompt, this split
    can become a gate.

    Run with::

        pytest -m eval_casual -v -s -k casual_test
    """
    await _run_split(analyzer, CASUAL_TEST_CASES, "Casual Test", min_accuracy=None)
