"""API-free contract tests for Guard evaluation gold and matching."""

from __future__ import annotations

from dataclasses import replace

import pytest

from echo_v2.services.guard_taxonomy import GuardCategory, GuardSignal
from tests.evaluation.guard.guard_cases import (
    EvalMessage,
    ExpectedSnapshot,
    GuardEvalCase,
    validate_guard_case,
    validate_guard_cases,
)
from tests.evaluation.guard.guard_comprehensive_baseline import ALL_CASES
from tests.evaluation.guard.guard_eval_results import SnapshotResult, _analysis_pass
from tests.evaluation.guard.test_guard_eval import _check_clean, _select_cases

_VALID_CASE = GuardEvalCase(
    case_id="contract_case",
    family="contract",
    messages=(
        EvalMessage("m1", "child", "hello"),
        EvalMessage("m2", "other", "bye"),
    ),
    snapshots=(ExpectedSnapshot(after_message_id="m1", expect_clean=True),),
)


def test_all_guard_cases_have_valid_snapshot_gold() -> None:
    validate_guard_cases(ALL_CASES)


def test_any_of_requirement_is_part_of_analysis_pass() -> None:
    result = SnapshotResult(
        after_message_id="m1",
        signals_pass=True,
        any_signal_pass=False,
        categories_pass=True,
        evidence_pass=True,
        confidence_pass=True,
        reason_pass=True,
        clean_pass=True,
    )

    assert not _analysis_pass(result)


def test_case_filter_preserves_requested_order(monkeypatch: pytest.MonkeyPatch) -> None:
    second = replace(_VALID_CASE, case_id="second_case")
    monkeypatch.setenv("GUARD_EVAL_CASE_IDS", "second_case,contract_case")

    assert _select_cases((_VALID_CASE, second)) == (second, _VALID_CASE)


def test_case_filter_rejects_unknown_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GUARD_EVAL_CASE_IDS", "missing_case")

    with pytest.raises(ValueError, match="unknown case IDs"):
        _select_cases((_VALID_CASE,))


def test_clean_result_requires_empty_detection_and_none_decision() -> None:
    assert _check_clean((), (), (), "none", _VALID_CASE.snapshots[0])
    assert not _check_clean(
        (GuardSignal.THREAT,), (), ("m1",), "watch", _VALID_CASE.snapshots[0]
    )


def test_duplicate_snapshot_is_rejected() -> None:
    invalid = replace(
        _VALID_CASE,
        snapshots=(
            ExpectedSnapshot(after_message_id="m1"),
            ExpectedSnapshot(after_message_id="m1"),
        ),
    )

    with pytest.raises(ValueError, match="duplicate snapshot"):
        validate_guard_case(invalid)


def test_out_of_order_snapshot_is_rejected() -> None:
    invalid = replace(
        _VALID_CASE,
        snapshots=(
            ExpectedSnapshot(after_message_id="m2"),
            ExpectedSnapshot(after_message_id="m1"),
        ),
    )

    with pytest.raises(ValueError, match="chronological"):
        validate_guard_case(invalid)


def test_required_and_forbidden_signals_cannot_overlap() -> None:
    invalid = replace(
        _VALID_CASE,
        snapshots=(
            ExpectedSnapshot(
                after_message_id="m1",
                required_signals=(GuardSignal.THREAT,),
                forbidden_signals=(GuardSignal.THREAT,),
            ),
        ),
    )

    with pytest.raises(ValueError, match="overlap"):
        validate_guard_case(invalid)


def test_any_of_cannot_include_forbidden_signal() -> None:
    invalid = replace(
        _VALID_CASE,
        snapshots=(
            ExpectedSnapshot(
                after_message_id="m1",
                required_signal_any_of=(GuardSignal.THREAT,),
                forbidden_signals=(GuardSignal.THREAT,),
            ),
        ),
    )

    with pytest.raises(ValueError, match="any-of signal is forbidden"):
        validate_guard_case(invalid)


def test_category_requires_supporting_signal() -> None:
    invalid = replace(
        _VALID_CASE,
        snapshots=(
            ExpectedSnapshot(
                after_message_id="m1",
                required_categories=(GuardCategory.BULLYING,),
                required_signals=(GuardSignal.THREAT,),
            ),
        ),
    )

    with pytest.raises(ValueError, match="supporting signal"):
        validate_guard_case(invalid)
