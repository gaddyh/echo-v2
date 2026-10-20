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
            "ce6359e3-c6a6-47bc-b696-ee65085b8e7d",
            "7da04049-7325-40dc-b46a-9d6bd5d2942b",
            "c0862b1e-8455-4252-a8f2-b2f20e1a0d8b",
        ),
    ),
    ExposureGoldCase(
        "critical_media_missing", "Critical media unavailable", None, None,
        (SimpleConversationMessage("media-1", "other", "", enrichment_status="failed"),),
    ),
    # Child-as-aggressor family (ported from behavior_cases.json, group cases
    # only — Exposure Guard does not run on private chats). The separation is
    # the contract: harmful conduct by the monitored child is a group-level
    # exposure event while staying out of scope for parent_should_know.
    ExposureGoldCase(
        "behavior_coordinated_exclusion",
        "Monitored child organizes a group boycott",
        False,
        True,
        (
            SimpleConversationMessage("m1", "child", "פותחים קבוצה חדשה בלי נועם. אל תוסיפו אותו."),
            SimpleConversationMessage("m2", "other", "למה? מה הוא עשה?"),
            SimpleConversationMessage("m3", "child", "מי שמדבר איתו שלא יבוא לשבת איתנו בהפסקה."),
            SimpleConversationMessage("m4", "other_2", "זה כבר ממש לא יפה."),
        ),
        expected_evidence_message_ids=("m1", "m3"),
    ),
    ExposureGoldCase(
        "behavior_repeated_humiliation",
        "Monitored child repeatedly humiliates another child",
        False,
        True,
        (
            SimpleConversationMessage("m1", "child", "תראו איך דניאל נפל היום, איזה אפס 😂"),
            SimpleConversationMessage("m2", "other", "שי די זה מביך אותי."),
            SimpleConversationMessage("m3", "child", "חכו אני מעלה גם את הסרטון שלו עוד פעם."),
            SimpleConversationMessage("m4", "other_2", "די שי, הוא ביקש שתפסיק."),
            SimpleConversationMessage("m5", "child", "שכולם יראו כמה הוא פתטי."),
        ),
        expected_evidence_message_ids=("m1", "m3"),
    ),
    ExposureGoldCase(
        "behavior_single_argument_negative",
        "One-off argument",
        False,
        False,
        (
            SimpleConversationMessage("m1", "other", "שי, שכחת שוב להביא את העבודה."),
            SimpleConversationMessage("m2", "child", "די כבר לחפור לי, זה מעצבן."),
            SimpleConversationMessage("m3", "other", "טוב, נדבר מחר."),
            SimpleConversationMessage("m4", "child", "סבבה סליחה שהתעצבנתי."),
        ),
    ),
    ExposureGoldCase(
        "behavior_defensive_response_negative",
        "Monitored child defends themselves against targeting",
        True,
        True,
        (
            SimpleConversationMessage("m1", "other", "שי אף אחד לא רוצה אותך בקבוצה, תעוף."),
            SimpleConversationMessage("m2", "other_2", "נכון, אף אחד לא סובל אותו."),
            SimpleConversationMessage("m3", "child", "עזבו אותי כבר, אתם ממש מגעילים."),
            SimpleConversationMessage("m4", "child", "תפסיקו לדבר אליי ככה."),
        ),
        expected_evidence_message_ids=("m1", "m2"),
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
