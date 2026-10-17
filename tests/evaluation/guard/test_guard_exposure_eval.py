"""Contract tests for the Exposure Guard product-value Gold set."""

from __future__ import annotations

from tests.evaluation.guard.guard_exposure_cases import CASES, validate_cases


def test_exposure_gold_cases_are_valid() -> None:
    validate_cases()
    assert {case.case_id for case in CASES} == {
        "ordinary_group_chat",
        "bullying_other_child",
        "threat_monitored_child",
        "real_cherries_appearance_insults",
        "critical_media_missing",
    }


def test_exposure_gold_covers_child_exposure_separation() -> None:
    separation = next(case for case in CASES if case.case_id == "bullying_other_child")
    assert separation.child_expected is False
    assert separation.exposure_expected is True


def test_real_cherries_case_pins_chat_and_core_evidence() -> None:
    case = next(case for case in CASES if case.case_id == "real_cherries_appearance_insults")
    assert case.chat_id == "120363405047486307@g.us"
    assert case.exposure_expected is True
    assert case.expected_evidence_message_ids == (
        "7da04049-7325-40dc-b46a-9d6bd5d2942b",
        "c0862b1e-8455-4252-a8f2-b2f20e1a0d8b",
        "d7103375-924d-488c-9f50-48b1440efaae",
        "e952773d-dc2d-4cf2-a3e5-5310123b52fd",
    )
    assert "4f766ab4-70ee-48d3-996f-02eccc7d5283" not in case.expected_evidence_message_ids
