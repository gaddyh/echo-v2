"""Compact product-value Gold cases for Exposure Guard."""

from __future__ import annotations

from dataclasses import dataclass

from echo_v2.domain.guard_simple import SimpleConversationMessage


@dataclass(frozen=True)
class ExposureGoldCase:
    case_id: str
    title: str
    child_expected: bool | None
    exposure_expected: bool | None
    messages: tuple[SimpleConversationMessage, ...]


CASES = (
    ExposureGoldCase(
        "ordinary_group_chat", "Ordinary group conversation", False, False,
        (SimpleConversationMessage("ordinary-1", "other", "Who is joining the game?"),),
    ),
    ExposureGoldCase(
        "bullying_other_child", "Repeated bullying of another participant", False, True,
        (
            SimpleConversationMessage("m17", "other", "Nobody wants Daniel here."),
            SimpleConversationMessage("m21", "other_2", "Daniel is a loser."),
            SimpleConversationMessage("m24", "other", "Stop asking us to stop."),
        ),
    ),
    ExposureGoldCase(
        "threat_monitored_child", "Threat to the monitored child", True, True,
        (
            SimpleConversationMessage("threat-1", "other", "I will hurt you after school."),
            SimpleConversationMessage("threat-2", "child", "Stop threatening me."),
        ),
    ),
    ExposureGoldCase(
        "critical_media_missing", "Critical media unavailable", None, None,
        (SimpleConversationMessage("media-1", "other", "", enrichment_status="failed"),),
    ),
)


def validate_cases(cases: tuple[ExposureGoldCase, ...] = CASES) -> None:
    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("Exposure Gold case IDs must be unique")
    for case in cases:
        message_ids = [message.id for message in case.messages]
        if len(message_ids) != len(set(message_ids)):
            raise ValueError(f"{case.case_id}: message IDs must be unique")
        if case.exposure_expected is True and case.child_expected is False:
            assert case.messages, f"{case.case_id}: positive exposure needs context"


__all__ = ["CASES", "ExposureGoldCase", "validate_cases"]
