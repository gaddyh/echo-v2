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
    chat_id: str | None = None
    expected_evidence_message_ids: tuple[str, ...] = ()


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
        "real_cherries_appearance_insults",
        "Real class group appearance-based insult exchange",
        None,
        True,
        (
            SimpleConversationMessage(
                "ce6359e3-c6a6-47bc-b696-ee65085b8e7d",
                "other",
                "זה אמור להיות אתה ?? כי עם כן זה ממש לא דומה לך",
            ),
            SimpleConversationMessage(
                "7da04049-7325-40dc-b46a-9d6bd5d2942b",
                "other_2",
                "איתן אתה ניראה כמו פיליפני",
            ),
            SimpleConversationMessage("e952773d-dc2d-4cf2-a3e5-5310123b52fd", "other_3", "חחחח"),
            SimpleConversationMessage("c0862b1e-8455-4252-a8f2-b2f20e1a0d8b", "other_4", "הוא נראה כמו קוף"),
            SimpleConversationMessage("d7103375-924d-488c-9f50-48b1440efaae", "other_3", "שתוק יהייטר"),
            SimpleConversationMessage("4f766ab4-70ee-48d3-996f-02eccc7d5283", "other_5", "פאק השם שלי"),
        ),
        chat_id="120363405047486307@g.us",
        expected_evidence_message_ids=(
            "7da04049-7325-40dc-b46a-9d6bd5d2942b",
            "c0862b1e-8455-4252-a8f2-b2f20e1a0d8b",
            "d7103375-924d-488c-9f50-48b1440efaae",
            "e952773d-dc2d-4cf2-a3e5-5310123b52fd",
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
        if not set(case.expected_evidence_message_ids).issubset(set(message_ids)):
            raise ValueError(f"{case.case_id}: expected evidence IDs must be present in messages")
        if len(case.expected_evidence_message_ids) != len(set(case.expected_evidence_message_ids)):
            raise ValueError(f"{case.case_id}: expected evidence IDs must be unique")


__all__ = ["CASES", "ExposureGoldCase", "validate_cases"]
