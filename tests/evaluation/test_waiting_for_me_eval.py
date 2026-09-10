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
from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.domain.waiting_for_me import WaitingForMeDecision
from echo_v2.services.chat_analysis_worker import ConversationInput
from echo_v2.services.waiting_for_me_analyzer import (
    AnalysisError,
    LLMWaitingForMeAnalyzer,
)
from tests.evaluation.waiting_for_me_cases import EVAL_CASES, EvalCase

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
    return LLMWaitingForMeAnalyzer(
        api_key=os.environ.get("OPENAI_API_KEY", ""),
        model=os.environ.get("LLM_MODEL_NAME", "gpt-4.1"),
    )


async def _run_case(
    analyzer: LLMWaitingForMeAnalyzer, case: EvalCase
) -> tuple[WaitingForMeDecision | None, str | None]:
    """Run a single case, return (actual_decision, error_message)."""
    conv = _build_conversation(case)
    try:
        result = await analyzer.analyze(conv)
        return result.decision, None
    except AnalysisError as exc:
        return None, str(exc)


@pytest.mark.eval
async def test_evaluation_summary(analyzer):
    """Run all eval cases and report accuracy + confusion matrix.

    This is a single test that runs all cases, prints a summary, and
    asserts a minimum accuracy threshold (default 70%).
    """
    results: list[tuple[EvalCase, WaitingForMeDecision | None, str | None]] = []
    for case in EVAL_CASES:
        actual, error = await _run_case(analyzer, case)
        results.append((case, actual, error))

    # Build confusion matrix.
    matrix: dict[tuple[WaitingForMeDecision, WaitingForMeDecision], int] = {}
    correct = 0
    errors = 0
    for case, actual, error in results:
        if error or actual is None:
            errors += 1
            continue
        key = (case.expected, actual)
        matrix[key] = matrix.get(key, 0) + 1
        if actual == case.expected:
            correct += 1

    total = len(results)
    accuracy = correct / total if total > 0 else 0.0

    # Print full report.
    print("\n" + "=" * 70)
    print("WaitingForMe Evaluation Report")
    print("=" * 70)
    print(f"Cases: {total} | Correct: {correct} | Errors: {errors}")
    print(f"Accuracy: {accuracy:.1%}")
    print()

    # Confusion matrix.
    print("Confusion matrix (rows=expected, cols=actual):")
    header = "              " + "  ".join(f"{d.value[:8]:>14s}" for d in _LABELS)
    print(header)
    for expected in _LABELS:
        row = f"{expected.value[:14]:>14s}"
        for actual in _LABELS:
            count = matrix.get((expected, actual), 0)
            row += f"  {count:>14d}"
        print(row)
    print()

    # Per-case details.
    print("Per-case results:")
    print("-" * 70)
    for case, actual, error in results:
        if error:
            status = "E"
            actual_str = f"ERROR: {error}"
        elif actual == case.expected:
            status = "PASS"
            actual_str = actual.value if actual else "?"
        else:
            status = "FAIL"
            actual_str = actual.value if actual else "?"
        print(
            f"  {status:4s} {case.id}  {case.expected.value:>20s} -> {actual_str:>20s}  {case.description}"
        )
    print("-" * 70)

    # Assert minimum accuracy.
    min_accuracy = float(os.environ.get("EVAL_MIN_ACCURACY", "0.7"))
    assert accuracy >= min_accuracy, (
        f"Eval accuracy {accuracy:.1%} below threshold {min_accuracy:.1%}"
    )
