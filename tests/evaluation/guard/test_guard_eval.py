"""Evaluation harness for the Guard analyzer.

Runs the labeled cases in :mod:`tests.evaluation.guard.guard_cases`
against the real LLM API. Unlike the WaitingForMe harness, each case has
multiple ``ExpectedSnapshot`` checkpoints — the analyzer is run on the
conversation *prefix* up to each checkpoint, so we can measure *when*
the analyzer starts alerting (too early / on time / too late / missed
entirely).

This is NOT part of the normal test suite. It only runs when:
  - ``OPENAI_API_KEY`` is set, AND
  - the ``eval_guard`` marker is selected: ``pytest -m eval_guard -v -s``

Usage::

    # Run the full Guard evaluation
    pytest -m eval_guard -v -s

    # Run only unknown_contact_escalation family
    pytest -m eval_guard -v -s -k unknown_contact

    # Override snapshot accuracy threshold (default 70%)
    GUARD_MIN_ACCURACY=0.8 pytest -m eval_guard -v -s

The harness asserts a minimum snapshot accuracy threshold but does NOT
assert per-snapshot pass/fail (LLMs are non-deterministic). The printed
report shows which snapshots the LLM got right or wrong.
"""

from __future__ import annotations

import os
import time

import pytest

from echo_v2.services.guard_alert_policy import (
    ChildContext,
    ConversationContext,
    DefaultAlertPolicy,
)
from echo_v2.services.guard_analyzer import (
    GUARD_ANALYZER_VERSION,
    GUARD_PROMPT_VERSION,
    AnalysisError,
    GuardAnalysisInput,
    LLMGuardAnalyzer,
)
from tests.evaluation.guard.guard_cases import (
    GUARD_CASES,
    GuardEvalCase,
)
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
    """Return the conversation prefix up to and including after_message_id."""
    prefix: list[tuple[str, str]] = []
    for msg in case.messages:
        prefix.append((msg.sender, msg.text))
        if msg.id == after_message_id:
            break
    return prefix


def _check_signals(
    actual: tuple[str, ...],
    required: tuple[str, ...],
    forbidden: tuple[str, ...],
) -> bool:
    """True if all required signals are present and none of forbidden are."""
    actual_set = set(actual)
    return all(s in actual_set for s in required) and not (
        forbidden and actual_set & set(forbidden)
    )


def _check_categories(
    actual: tuple[str, ...], required: tuple[str, ...]
) -> bool:
    """True if all required categories are present."""
    if not required:
        return True
    actual_set = set(actual)
    return all(c in actual_set for c in required)


def _check_alert(
    actual_should_alert: bool | None, expected_should_alert: bool | None
) -> bool | None:
    """Check policy output against the policy gold label."""
    if expected_should_alert is None:
        return None
    if actual_should_alert is None:
        return False
    return actual_should_alert == expected_should_alert


def _analysis_pass(snapshot_result: SnapshotResult) -> bool:
    """Whether the detection-only analyzer assertions pass."""
    return (
        snapshot_result.error is None
        and snapshot_result.decision_pass
        and snapshot_result.signals_pass
        and snapshot_result.categories_pass
    )


def _policy_pass(snapshot_result: SnapshotResult) -> bool:
    """Whether the separate deterministic policy assertion passes."""
    return snapshot_result.alert_pass is None or snapshot_result.alert_pass


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
    """Run all snapshots for one case, return a GuardCaseResult."""
    case_result = GuardCaseResult(case=case)
    policy = DefaultAlertPolicy()
    for snapshot in case.snapshots:
        prefix = _build_prefix(case, snapshot.after_message_id)
        conv = GuardAnalysisInput(
            child_id="eval-child",
            chat_id="eval-chat",
            messages=prefix,
            context=case.context,
        )
        t0 = time.perf_counter()
        try:
            result, raw = await analyzer.analyze_with_raw(conv)
            latency_ms = (time.perf_counter() - t0) * 1000
            decision_pass = result.decision in snapshot.acceptable_decisions
            signals_pass = _check_signals(
                result.signals,
                snapshot.required_signals,
                snapshot.forbidden_signals,
            )
            categories_pass = _check_categories(
                result.categories, snapshot.required_categories
            )
            policy_alert = policy.should_alert(
                analysis=result,
                child_context=ChildContext(),
                conversation_context=ConversationContext(),
            )
            alert_pass = _check_alert(policy_alert, snapshot.should_alert)
            case_result.snapshots.append(
                SnapshotResult(
                    snapshot=snapshot,
                    actual_decision=result.decision,
                    actual_signals=result.signals,
                    actual_categories=result.categories,
                    actual_should_alert=policy_alert,
                    decision_pass=decision_pass,
                    signals_pass=signals_pass,
                    categories_pass=categories_pass,
                    alert_pass=alert_pass,
                    latency_ms=latency_ms,
                    raw_response=raw,
                )
            )
        except AnalysisError as exc:
            latency_ms = (time.perf_counter() - t0) * 1000
            case_result.snapshots.append(
                SnapshotResult(
                    snapshot=snapshot,
                    error=str(exc),
                    latency_ms=latency_ms,
                )
            )
    return case_result


def _print_report(
    case_results: list[GuardCaseResult], accuracy: float
) -> None:
    """Print a rich per-snapshot report to stdout."""
    total = sum(len(cr.snapshots) for cr in case_results)
    passed = sum(
        1
        for cr in case_results
        for sr in cr.snapshots
        if _analysis_pass(sr) and _policy_pass(sr)
    )
    analyzer_passed = sum(
        1 for cr in case_results for sr in cr.snapshots if _analysis_pass(sr)
    )
    policy_passed = sum(
        1 for cr in case_results for sr in cr.snapshots if _policy_pass(sr)
    )
    errors = sum(
        1 for cr in case_results for sr in cr.snapshots if sr.error is not None
    )

    print("\n" + "=" * 80)
    print("  Guard Evaluation Report")
    print("=" * 80)
    print(f"\n  Total snapshots: {total}")
    print(f"  Analyzer pass:   {analyzer_passed}/{total} ({analyzer_passed / total:.1%})")
    print(f"  Policy pass:     {policy_passed}/{total} ({policy_passed / total:.1%})")
    print(f"  End-to-end pass:  {passed}/{total} ({accuracy:.1%})")
    print(f"  Errors:           {errors}")
    print()

    # Per-snapshot table.
    header = (
        f"  {'Case':<40} {'After':<6} {'Expected':<22} "
        f"{'Actual':<8} {'Dec':<4} {'Sig':<4} {'Cat':<4} {'Alt':<4} {'Status':<6}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    for cr in case_results:
        for sr in cr.snapshots:
            expected_str = "|".join(sr.snapshot.acceptable_decisions)
            if sr.error:
                actual_str = "ERR"
                status = "ERR"
            else:
                actual_str = sr.actual_decision or "?"
                if _analysis_pass(sr) and _policy_pass(sr):
                    status = "PASS"
                else:
                    status = "FAIL"

            dec_mark = "✓" if sr.decision_pass else "✗"
            sig_mark = "✓" if sr.signals_pass else "✗"
            cat_mark = "✓" if sr.categories_pass else "✗"
            if sr.alert_pass is None:
                alt_mark = "—"
            else:
                alt_mark = "✓" if sr.alert_pass else "✗"

            row = (
                f"  {cr.case.case_id:<40} "
                f"{sr.snapshot.after_message_id:<6} "
                f"{expected_str:<22} "
                f"{actual_str:<8} "
                f"{dec_mark:<4} {sig_mark:<4} {cat_mark:<4} {alt_mark:<4} "
                f"{status:<6}"
            )
            print(row)

    # Failures detail.
    failures = [
        (cr, sr)
        for cr in case_results
        for sr in cr.snapshots
        if sr.error is not None
        or not (_analysis_pass(sr) and _policy_pass(sr))
    ]
    if failures:
        print("\n  --- Failures Detail ---\n")
        for cr, sr in failures:
            print(f"  {cr.case.case_id} @ {sr.snapshot.after_message_id}:")
            if sr.error:
                print(f"    ERROR: {sr.error[:120]}")
            else:
                if not sr.decision_pass:
                    print(
                        f"    decision: expected one of "
                        f"{sr.snapshot.acceptable_decisions}, "
                        f"got '{sr.actual_decision}'"
                    )
                if not sr.signals_pass:
                    missing = set(sr.snapshot.required_signals) - set(
                        sr.actual_signals
                    )
                    forbidden_found = set(sr.snapshot.forbidden_signals) & set(
                        sr.actual_signals
                    )
                    if missing:
                        print(f"    missing signals: {missing}")
                    if forbidden_found:
                        print(f"    forbidden signals found: {forbidden_found}")
                if not sr.categories_pass:
                    missing_cat = set(sr.snapshot.required_categories) - set(
                        sr.actual_categories
                    )
                    print(f"    missing categories: {missing_cat}")
                if sr.alert_pass is False:
                    print(
                        f"    alert: expected "
                        f"should_alert={sr.snapshot.should_alert}, "
                        f"got should_alert={sr.actual_should_alert}"
                    )
    else:
        print("\n  No failures! All snapshots passed.\n")
    print()


async def _run_eval_suite(
    analyzer: LLMGuardAnalyzer,
    cases: tuple[GuardEvalCase, ...],
    label: str,
    min_accuracy: float,
) -> None:
    """Run a set of Guard eval cases, print report, save results, assert accuracy."""
    case_results: list[GuardCaseResult] = []
    for case in cases:
        cr = await _run_case(analyzer, case)
        case_results.append(cr)

    total_snapshots = sum(len(cr.snapshots) for cr in case_results)
    passed_snapshots = sum(
        1 for cr in case_results for sr in cr.snapshots if _analysis_pass(sr)
    )
    end_to_end_passed = sum(
        1
        for cr in case_results
        for sr in cr.snapshots
        if _analysis_pass(sr) and _policy_pass(sr)
    )
    analyzer_accuracy = passed_snapshots / total_snapshots if total_snapshots else 0.0
    end_to_end_accuracy = (
        end_to_end_passed / total_snapshots if total_snapshots else 0.0
    )

    _print_report(case_results, end_to_end_accuracy)

    model = os.environ.get(
        "GUARD_LLM_MODEL", os.environ.get("LLM_MODEL_NAME", "gpt-4.1")
    )
    run_id = save_guard_eval_run(
        label, model, case_results, prompt_version=GUARD_PROMPT_VERSION,
    )
    print(
        f"  Results saved: run_id={run_id} "
        f"(analyzer={GUARD_ANALYZER_VERSION}, prompt={GUARD_PROMPT_VERSION})"
    )
    print(f"  → tests/evaluation/results/{run_id}_guard_{label.lower().replace(' ', '_')}/")

    assert analyzer_accuracy >= min_accuracy, (
        f"{label} analyzer accuracy {analyzer_accuracy:.1%} "
        f"below threshold {min_accuracy:.1%}"
    )


@pytest.mark.eval_guard
async def test_guard_eval(analyzer):
    """Run all Guard eval cases.

    Each case has multiple snapshot checkpoints. The analyzer is run on
    the conversation prefix at each checkpoint, so we measure both
    *whether* and *when* it detects the concern.

    Threshold: 70% (default). Override with ``GUARD_MIN_ACCURACY``.
    """
    min_accuracy = float(os.environ.get("GUARD_MIN_ACCURACY", "0.7"))
    await _run_eval_suite(analyzer, GUARD_CASES, "All", min_accuracy)
