"""API-free contract checks for the Baseline B Gold prefixes."""

from __future__ import annotations

from echo_v2.domain.guard_simple import ChildRole
from tests.evaluation.guard.guard_simple_cases import CASES, validate_cases


def test_baseline_b_gold_cases_are_valid() -> None:
    validate_cases()


def test_gold_cases_cover_prefixes_and_expected_parent_attention() -> None:
    assert len(CASES) == 6
    assert all(len(case.checkpoints) >= 2 for case in CASES)
    assert all(
        checkpoint.expected_parent_should_know
        for case in CASES
        for checkpoint in case.checkpoints
    )
    assert all(
        value in (True, False, None)
        for case in CASES
        for checkpoint in case.checkpoints
        for value in checkpoint.expected_parent_should_know
    )
    assert any(
        ChildRole.TARGET in checkpoint.expected_child_roles
        for case in CASES
        for checkpoint in case.checkpoints
    )
