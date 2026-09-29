"""LLM evaluation harness for the Guard analyzer.

This eval tests detection only: severity, categories, and cumulative signals.
Notification behavior belongs to the deterministic AlertPolicy tests and is
not evaluated here.

Run with::

    pytest -m eval_guard -v -s
"""

from __future__ import annotations

import os
import time

import pytest

from echo_v2.services.guard_analyzer import (
    GUARD_ANALYZER_VERSION,
    GUARD_PROMPT_VERSION,
    AnalysisError,
    GuardAnalysisInput,
    LLMGuardAnalyzer,
)
from tests.evaluation.guard.guard_cases import GUARD_CASES, GuardEvalCase
from tests.evaluation.guard.guard_eval_results import (
    GuardCaseResult,
    SnapshotResult,
    save_guard_eval_run,
)

pytestmark = [
    pytest.mark.eval_guard,
    pytest.mark.skipif(
        not os.environ.get("OPENAI_API_KEY"),
        reason="OPENAI_API_KEY not set — eval requires real API access",
    ),
]


def _build_prefix(
    case: GuardEvalCase, after_message_id: str
) -> list[tuple[str, str]]:
    prefix: list[tuple[str, str]] = []
    for message in case.messages:
        prefix.append((message.sender, message.text))
        if message.id == after_message_id:
            break
    return prefix


def _check_signals(
    actual: tuple[str, ...],
    required: tuple[str, ...],
    forbidden: tuple[str, ...],
) -> bool:
    actual_set = set(actual)
    return all(signal in actual_set for signal in required) and not (
        forbidden and actual_set & set(forbidden)
    )


def _check_categories(actual: tuple[str, ...], required: tuple[str, ...]) -> bool:
    return all(category in set(actual) for category in required)


@pytest.fixture
def analyzer() -> LLMGuardAnalyzer:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
    return LLMGuardAnalyzer(
        client=client,
        model=os.environ.get("GUARD_LLM_MODEL", os.environ.get("LLM_MODEL_NAME", "gpt-4.1")),
        prompt_version=os.environ.get("GUARD_PROMPT_VERSION", GUARD_PROMPT_VERSION),
    )


async def _run_case(
    analyzer: LLMGuardAnalyzer, case: GuardEvalCase
) -> GuardCaseResult:
    case_result = GuardCaseResult(case=case)
    for snapshot in case.snapshots:
        conversation = GuardAnalysisInput(
            child_id="eval-child",
            chat_id="eval-chat",
            messages=_build_prefix(case, snapshot.after_message_id),
            context=case.context,
        )
        started = time.perf_counter()
        try:
            analysis, raw = await analyzer.analyze_with_raw(conversation)
            case_result.snapshots.append(
                SnapshotResult(
                    after_message_id=snapshot.after_message_id,
                    actual_decision=analysis.decision,
                    actual_signals=analysis.signals,
                    actual_categories=analysis.categories,
                    decision_pass=analysis.decision in snapshot.acceptable_decisions,
                    signals_pass=_check_signals(
                        analysis.signals,
                        snapshot.required_signals,
                        snapshot.forbidden_signals,
                    ),
                    categories_pass=_check_categories(
                        analysis.categories,
                        snapshot.required_categories,
                    ),
                    latency_ms=(time.perf_counter() - started) * 1000,
                    raw_response=raw,
                )
            )
        except AnalysisError as exc:
            case_result.snapshots.append(
                SnapshotResult(
                    after_message_id=snapshot.after_message_id,
                    error=str(exc),
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
            )
    return case_result


def _analysis_pass(result: SnapshotResult) -> bool:
    return (
        result.error is None
        and result.decision_pass
        and result.signals_pass
        and result.categories_pass
    )


def _print_report(case_results: list[GuardCaseResult], accuracy: float) -> None:
    snapshots = [result for case in case_results for result in case.snapshots]
    passed = sum(_analysis_pass(result) for result in snapshots)
    errors = sum(result.error is not None for result in snapshots)

    print("\n" + "=" * 88)
    print("  Guard Analyzer Evaluation Report")
    print("=" * 88)
    print(f"\n  Total snapshots: {len(snapshots)}")
    print(f"  Analyzer pass:   {passed}/{len(snapshots)} ({accuracy:.1%})")
    print(f"  Errors:          {errors}")
    print()
    header = (
        f"  {'Case':<40} {'After':<6} {'Expected':<22} {'Actual':<10} "
        f"{'Dec':<4} {'Sig':<4} {'Cat':<4} {'Status':<6}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    for case in case_results:
        for result in case.snapshots:
            expected = next(
                snapshot for snapshot in case.case.snapshots
                if snapshot.after_message_id == result.after_message_id
            )
            status = "PASS" if _analysis_pass(result) else "FAIL"
            actual = result.actual_decision or "ERR"
            print(
                f"  {case.case.case_id:<40} {result.after_message_id:<6} "
                f"{'|'.join(expected.acceptable_decisions):<22} {actual:<10} "
                f"{'✓' if result.decision_pass else '✗':<4} "
                f"{'✓' if result.signals_pass else '✗':<4} "
                f"{'✓' if result.categories_pass else '✗':<4} {status:<6}"
            )

    failures = [
        (case, result)
        for case in case_results
        for result in case.snapshots
        if not _analysis_pass(result)
    ]
    if failures:
        print("\n  --- Analyzer Failures ---\n")
        for case, result in failures:
            print(f"  {case.case.case_id} @ {result.after_message_id}:")
            if result.error:
                print(f"    ERROR: {result.error[:120]}")
            if not result.signals_pass:
                print(f"    signals: actual={result.actual_signals}")
            if not result.categories_pass:
                print(f"    categories: actual={result.actual_categories}")
    else:
        print("\n  No analyzer failures! All snapshots passed.\n")


async def _run_eval_suite(
    analyzer: LLMGuardAnalyzer,
    cases: tuple[GuardEvalCase, ...],
    label: str,
    min_accuracy: float,
) -> None:
    case_results = [await _run_case(analyzer, case) for case in cases]
    snapshots = [result for case in case_results for result in case.snapshots]
    passed = sum(_analysis_pass(result) for result in snapshots)
    accuracy = passed / len(snapshots) if snapshots else 0.0

    _print_report(case_results, accuracy)
    model = os.environ.get(
        "GUARD_LLM_MODEL", os.environ.get("LLM_MODEL_NAME", "gpt-4.1")
    )
    run_id = save_guard_eval_run(
        label, model, case_results, prompt_version=GUARD_PROMPT_VERSION
    )
    print(
        f"\n  Results saved: run_id={run_id} "
        f"(analyzer={GUARD_ANALYZER_VERSION}, prompt={GUARD_PROMPT_VERSION})"
    )
    print(f"  → tests/evaluation/results/{run_id}_guard_{label.lower().replace(' ', '_')}/")

    assert accuracy >= min_accuracy, (
        f"{label} analyzer accuracy {accuracy:.1%} "
        f"below threshold {min_accuracy:.1%}"
    )


@pytest.mark.eval_guard
async def test_guard_eval(analyzer: LLMGuardAnalyzer) -> None:
    """Run the detection-only Guard analyzer evaluation."""
    min_accuracy = float(os.environ.get("GUARD_MIN_ACCURACY", "0.7"))
    await _run_eval_suite(analyzer, GUARD_CASES, "All", min_accuracy)
