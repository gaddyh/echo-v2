"""Contract tests for the Exposure Guard product-value Gold set."""

from __future__ import annotations

from tests.evaluation.guard.guard_exposure_cases import CASES, validate_cases


def test_exposure_gold_cases_are_valid() -> None:
    validate_cases()
    assert {case.case_id for case in CASES} == {
        "ordinary_group_chat",
        "bullying_other_child",
        "threat_monitored_child",
        "critical_media_missing",
    }


def test_exposure_gold_covers_child_exposure_separation() -> None:
    separation = next(case for case in CASES if case.case_id == "bullying_other_child")
    assert separation.child_expected is False
    assert separation.exposure_expected is True
