"""Multi-run LLM evaluation harness for the Guard analyzer.

This eval tests detection only: severity, categories, and cumulative signals.
Notification behavior belongs to the deterministic AlertPolicy tests and is
not evaluated here.

The suite runs three times by default to make model variance visible. Override
with ``GUARD_EVAL_RUNS`` when needed::

    pytest -m eval_guard -v -s
    GUARD_EVAL_RUNS=5 pytest -m eval_guard -v -s
"""

from __future__ import annotations

import os
import time
from collections import Counter
from dataclasses import dataclass

import pytest

from echo_v2.services.guard_analyzer import (
    GUARD_ANALYZER_VERSION,
    GUARD_PROMPT_VERSION,
    AnalysisError,
    GuardAnalysisInput,
    GuardMessage,
    LLMGuardAnalyzer,
)
from tests.evaluation.guard.guard_cases import GUARD_CASES, GuardEvalCase
from tests.evaluation.guard.guard_eval_results import (
    GuardCaseResult,
    SnapshotResult,
    save_guard_eval_run,
)
from tests.evaluation.guard.guard_mvp_eval_cases import GUARD_MVP_CASES

pytestmark = [
    pytest.mark.eval_guard,
    pytest.mark.skipif(
        not os.environ.get("OPENAI_API_KEY"),
        reason="OPENAI_API_KEY not set — eval requires real API access",
    ),
]


@dataclass
class AggregateSnapshot:
    case_id: str
    after_message_id: str
    expected_decisions: str
    total_runs: int = 0
    passed_runs: int = 0
    decision_passes: int = 0
    signal_passes: int = 0
    category_passes: int = 0
    errors: int = 0
    decisions: Counter[str] | None = None
    signals: Counter[str] | None = None
    evidence: Counter[str] | None = None

    def __post_init__(self) -> None:
        self.decisions = Counter()
        self.signals = Counter()
        self.evidence = Counter()


def _build_prefix(
    case: GuardEvalCase, after_message_id: str
) -> tuple[GuardMessage, ...]:
    prefix: list[GuardMessage] = []
    for message in case.messages:
        prefix.append(
            GuardMessage(id=message.id, sender=message.sender, text=message.text)
        )
        if message.id == after_message_id:
            break
    return tuple(prefix)


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


def _check_any_signal(actual: tuple[str, ...], acceptable: tuple[str, ...]) -> bool:
    return not acceptable or bool(set(actual) & set(acceptable))


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
                    actual_decision=None,
                    actual_signals=analysis.signals,
                    actual_categories=analysis.categories,
                    actual_evidence_message_ids=analysis.evidence_message_ids,
                    decision_pass=True,
                    signals_pass=_check_signals(
                        analysis.signals,
                        snapshot.required_signals,
                        snapshot.forbidden_signals,
                    ),
                    any_signal_pass=_check_any_signal(
                        analysis.signals,
                        snapshot.required_signal_any_of,
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


def _flatten(case_results: list[GuardCaseResult]) -> list[tuple[str, SnapshotResult]]:
    return [
        (case.case.case_id, result)
        for case in case_results
        for result in case.snapshots
    ]


def _print_single_run(run_number: int, case_results: list[GuardCaseResult]) -> None:
    results = [result for _, result in _flatten(case_results)]
    passed = sum(_analysis_pass(result) for result in results)
    total = len(results)
    print(f"\n  Run {run_number}: {passed}/{total} ({passed / total:.1%})")
    for case_id, result in _flatten(case_results):
        status = "PASS" if _analysis_pass(result) else "FAIL"
        print(
            f"    {case_id} @ {result.after_message_id}: "
            f"{result.actual_decision.value if result.actual_decision else 'ERR'} {status} "
            f"evidence={result.actual_evidence_message_ids} "
            f"signals={result.actual_signals}"
        )


def _aggregate_results(
    all_runs: list[list[GuardCaseResult]],
    cases: tuple[GuardEvalCase, ...],
) -> dict[tuple[str, str], AggregateSnapshot]:
    aggregate: dict[tuple[str, str], AggregateSnapshot] = {}
    for run in all_runs:
        for case_id, result in _flatten(run):
            key = (case_id, result.after_message_id)
            entry = aggregate.get(key)
            if entry is None:
                expected = next(
                    snapshot
                    for case in cases
                    if case.case_id == case_id
                    for snapshot in case.snapshots
                    if snapshot.after_message_id == result.after_message_id
                )
                entry = AggregateSnapshot(
                    case_id=case_id,
                    after_message_id=result.after_message_id,
                    expected_decisions="|".join(
                        decision.value for decision in expected.acceptable_decisions
                    ),
                )
                aggregate[key] = entry
            entry.total_runs += 1
            entry.passed_runs += _analysis_pass(result)
            entry.decision_passes += result.decision_pass
            entry.signal_passes += result.signals_pass
            entry.category_passes += result.categories_pass
            entry.errors += result.error is not None
            if result.actual_decision and entry.decisions is not None:
                entry.decisions[result.actual_decision.value] += 1
            if entry.signals is not None:
                entry.signals.update(result.actual_signals)
            if entry.evidence is not None:
                evidence_key = ",".join(result.actual_evidence_message_ids) or "-"
                entry.evidence[evidence_key] += 1
    return aggregate


def _print_aggregate(aggregate: dict[tuple[str, str], AggregateSnapshot]) -> float:
    total_runs = sum(entry.total_runs for entry in aggregate.values())
    passed_runs = sum(entry.passed_runs for entry in aggregate.values())
    accuracy = passed_runs / total_runs if total_runs else 0.0

    print("\n" + "=" * 118)
    print("  Guard Analyzer Aggregate Report")
    print("=" * 118)
    print(f"\n  Total run-snapshots: {passed_runs}/{total_runs} ({accuracy:.1%})")
    print("  Stability means how many of the runs passed each snapshot.")
    print()
    header = (
        f"  {'Case':<40} {'After':<6} {'Pass':<7} {'Decisions':<24} "
        f"{'Signal pass':<12} {'Category pass':<14} {'Evidence observed':<28} "
        f"{'Observed signals'}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for entry in aggregate.values():
        decisions = ", ".join(
            f"{decision}:{count}" for decision, count in sorted(entry.decisions.items())
        ) or "ERR"
        signals = ", ".join(sorted(entry.signals)) or "-"
        evidence = "; ".join(
            f"{ids}:{count}/{entry.total_runs}"
            for ids, count in sorted(entry.evidence.items())
        ) or "-"
        print(
            f"  {entry.case_id:<40} {entry.after_message_id:<6} "
            f"{entry.passed_runs}/{entry.total_runs:<5} {decisions:<24} "
            f"{entry.signal_passes}/{entry.total_runs:<10} "
            f"{entry.category_passes}/{entry.total_runs:<12} "
            f"{evidence:<28} {signals}"
        )
    return accuracy


async def _run_eval_suite(
    analyzer: LLMGuardAnalyzer,
    cases: tuple[GuardEvalCase, ...],
    label: str,
    min_accuracy: float,
) -> None:
    run_count = int(os.environ.get("GUARD_EVAL_RUNS", "3"))
    if run_count < 1:
        raise ValueError("GUARD_EVAL_RUNS must be at least 1")

    model = os.environ.get(
        "GUARD_LLM_MODEL", os.environ.get("LLM_MODEL_NAME", "gpt-4.1")
    )
    all_runs: list[list[GuardCaseResult]] = []
    for run_number in range(1, run_count + 1):
        case_results = [await _run_case(analyzer, case) for case in cases]
        all_runs.append(case_results)
        _print_single_run(run_number, case_results)
        run_id = save_guard_eval_run(
            f"{label}_run_{run_number}",
            model,
            case_results,
            prompt_version=GUARD_PROMPT_VERSION,
        )
        print(f"    Saved run: {run_id}")

    aggregate = _aggregate_results(all_runs, cases)
    accuracy = _print_aggregate(aggregate)
    print(
        f"\n  Analyzer version: {GUARD_ANALYZER_VERSION}"
        f"\n  Prompt version:   {GUARD_PROMPT_VERSION}"
        f"\n  Runs:             {run_count}"
    )
    assert accuracy >= min_accuracy, (
        f"{label} aggregate analyzer accuracy {accuracy:.1%} "
        f"below threshold {min_accuracy:.1%}"
    )


@pytest.mark.eval_guard
async def test_guard_eval(analyzer: LLMGuardAnalyzer) -> None:
    """Run the selected detection-only Guard analyzer evaluation three times."""
    min_accuracy = float(os.environ.get("GUARD_MIN_ACCURACY", "0.7"))
    suite = os.environ.get("GUARD_EVAL_SUITE", "baseline").lower()
    if suite == "baseline":
        cases = GUARD_CASES
    elif suite == "mvp":
        cases = GUARD_MVP_CASES
    elif suite == "all":
        cases = GUARD_CASES + GUARD_MVP_CASES
    else:
        raise ValueError("GUARD_EVAL_SUITE must be baseline, mvp, or all")
    await _run_eval_suite(analyzer, cases, suite, min_accuracy)
