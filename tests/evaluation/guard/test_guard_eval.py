"""Multi-run LLM evaluation harness for the Guard analyzer.

This eval tests LLM detection semantics and derives severity separately through
DefaultDecisionPolicy. Notification behavior belongs to deterministic policy
tests and is not evaluated here.

The suite runs three times by default to make model variance visible. Override
with ``GUARD_EVAL_RUNS`` when needed::

    pytest -m eval_guard -v -s
    GUARD_EVAL_RUNS=5 pytest -m eval_guard -v -s
"""

from __future__ import annotations

import math
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
from echo_v2.services.guard_decision_policy import DefaultDecisionPolicy
from tests.evaluation.guard.guard_cases import (
    GUARD_CASES,
    ExpectedSnapshot,
    GuardEvalCase,
    validate_guard_cases,
)
from tests.evaluation.guard.guard_comprehensive_baseline import ALL_CASES
from tests.evaluation.guard.guard_eval_results import (
    GuardCaseResult,
    SnapshotResult,
    _analysis_pass,
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
    total_runs: int = 0
    passed_runs: int = 0
    signal_passes: int = 0
    any_signal_passes: int = 0
    category_passes: int = 0
    clean_passes: int = 0
    evidence_passes: int = 0
    decision_passes: int = 0
    errors: int = 0
    signals: Counter[str] | None = None
    evidence: Counter[str] | None = None

    def __post_init__(self) -> None:
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


def _check_categories(
    actual: tuple[str, ...],
    required: tuple[str, ...],
    forbidden: tuple[str, ...],
) -> tuple[bool, bool]:
    actual_set = set(actual)
    return (
        all(category in actual_set for category in required),
        not bool(actual_set & set(forbidden)),
    )


def _check_any_signal(actual: tuple[str, ...], acceptable: tuple[str, ...]) -> bool:
    return not acceptable or bool(set(actual) & set(acceptable))


def _check_evidence(
    actual_signals: tuple[str, ...],
    actual_categories: tuple[str, ...],
    actual_evidence: tuple[str, ...],
    expected: ExpectedSnapshot,
) -> bool:
    evidence_set = set(actual_evidence)
    return (
        set(expected.required_evidence_message_ids) <= evidence_set
        and (
            expected.expect_clean
            and not actual_evidence
            or not (actual_signals or actual_categories)
            or bool(actual_evidence)
        )
    )


def _check_clean(
    analysis_signals: tuple[str, ...],
    analysis_categories: tuple[str, ...],
    evidence_message_ids: tuple[str, ...],
    decision: str,
    expected: ExpectedSnapshot,
) -> bool:
    if not expected.expect_clean:
        return True
    return (
        decision == "none"
        and not analysis_signals
        and not analysis_categories
        and not evidence_message_ids
    )


def _check_confidence(confidence: float) -> bool:
    return math.isfinite(confidence) and 0.0 <= confidence <= 1.0


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
            actual_decision = DefaultDecisionPolicy().decide(
                signals=analysis.signals,
                categories=analysis.categories,
            )
            categories_pass, forbidden_categories_pass = _check_categories(
                analysis.categories,
                snapshot.required_categories,
                snapshot.forbidden_categories,
            )
            case_result.snapshots.append(
                SnapshotResult(
                    after_message_id=snapshot.after_message_id,
                    actual_signals=analysis.signals,
                    actual_categories=analysis.categories,
                    actual_evidence_message_ids=analysis.evidence_message_ids,
                    actual_confidence=analysis.confidence,
                    actual_reason=analysis.reason,
                    actual_decision=actual_decision.value,
                    signals_pass=_check_signals(
                        analysis.signals,
                        snapshot.required_signals,
                        snapshot.forbidden_signals,
                    ),
                    any_signal_pass=_check_any_signal(
                        analysis.signals,
                        snapshot.required_signal_any_of,
                    ),
                    categories_pass=categories_pass,
                    forbidden_categories_pass=forbidden_categories_pass,
                    evidence_pass=_check_evidence(
                        analysis.signals,
                        analysis.categories,
                        analysis.evidence_message_ids,
                        snapshot,
                    ),
                    confidence_pass=_check_confidence(analysis.confidence),
                    reason_pass=bool(analysis.reason.strip()),
                    clean_pass=_check_clean(
                        analysis.signals,
                        analysis.categories,
                        analysis.evidence_message_ids,
                        actual_decision.value,
                        snapshot,
                    ),
                    decision_pass=(
                        snapshot.expected_decision is None
                        or actual_decision.value == snapshot.expected_decision
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
            f"    {case_id} @ {result.after_message_id}: {status} "
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
                entry = AggregateSnapshot(
                    case_id=case_id,
                    after_message_id=result.after_message_id,
                )
                aggregate[key] = entry
            entry.total_runs += 1
            entry.passed_runs += _analysis_pass(result)
            entry.signal_passes += result.signals_pass
            entry.any_signal_passes += result.any_signal_pass
            entry.category_passes += result.categories_pass and result.forbidden_categories_pass
            entry.clean_passes += result.clean_pass
            entry.evidence_passes += result.evidence_pass
            entry.decision_passes += result.decision_pass
            entry.errors += result.error is not None
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
        f"  {'Case':<40} {'After':<6} {'Pass':<7} {'Any':<7} "
        f"{'Category':<10} {'Clean':<8} {'Evidence':<9} {'Decision':<10} "
        f"{'Evidence observed':<28} {'Observed signals'}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for entry in aggregate.values():
        signals = ", ".join(sorted(entry.signals)) or "-"
        evidence = "; ".join(
            f"{ids}:{count}/{entry.total_runs}"
            for ids, count in sorted(entry.evidence.items())
        ) or "-"
        print(
            f"  {entry.case_id:<40} {entry.after_message_id:<6} "
            f"{entry.passed_runs}/{entry.total_runs:<5} "
            f"{entry.any_signal_passes}/{entry.total_runs:<5} "
            f"{entry.category_passes}/{entry.total_runs:<8} "
            f"{entry.clean_passes}/{entry.total_runs:<6} "
            f"{entry.evidence_passes}/{entry.total_runs:<7} "
            f"{entry.decision_passes}/{entry.total_runs:<8} "
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
    report_only = os.environ.get("GUARD_EVAL_REPORT_ONLY", "0") == "1"
    print(
        f"\n  Analyzer version: {GUARD_ANALYZER_VERSION}"
        f"\n  Prompt version:   {GUARD_PROMPT_VERSION}"
        f"\n  Runs:             {run_count}"
        f"\n  Report only:      {report_only}"
    )
    if not report_only:
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
    elif suite == "mvp" or suite == "all":
        cases = GUARD_MVP_CASES
    elif suite == "comprehensive":
        cases = ALL_CASES
    else:
        raise ValueError(
            "GUARD_EVAL_SUITE must be baseline, mvp, comprehensive, or all"
        )
    validate_guard_cases(cases)
    await _run_eval_suite(analyzer, cases, suite, min_accuracy)
