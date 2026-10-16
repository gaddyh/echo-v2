"""API-free contract checks for the Baseline B Gold prefixes."""

from __future__ import annotations

from echo_v2.domain.guard_simple import ChildRole, SafetyStatus
from tests.evaluation.guard.guard_simple_cases import CASES, validate_cases


def test_baseline_b_gold_cases_are_valid() -> None:
    validate_cases()


def test_gold_cases_cover_prefixes_and_expected_statuses() -> None:
    assert len(CASES) == 6
    assert all(len(case.checkpoints) >= 2 for case in CASES)
    assert all(
        checkpoint.expected_statuses
        for case in CASES
        for checkpoint in case.checkpoints
    )
    assert all(
        isinstance(status, SafetyStatus)
        for case in CASES
        for checkpoint in case.checkpoints
        for status in checkpoint.expected_statuses
    )
    assert any(
        ChildRole.TARGET in checkpoint.expected_child_roles
        for case in CASES
        for checkpoint in case.checkpoints
    )
