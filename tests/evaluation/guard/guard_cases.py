"""Labeled evaluation cases for the Guard analyzer.

The Guard analyzer watches a child's conversation and decides, at each
point in time (after each message), how concerning the exchange is.
Unlike the WaitingForMe eval, the gold here is a sequence of
``ExpectedSnapshot`` checkpoints inside the conversation — not a single
label per case. This lets us measure *when* the analyzer starts alerting
(too early / on time / too late / missed entirely).

We deliberately do NOT pin in the gold:
- exact confidence
- exact summary
- the wording of the reason
Those are too brittle. We check semantics via ``acceptable_decisions``,
``required_categories``, ``required_signals`` and ``forbidden_signals``.

Cases are run against the real LLM API by a dedicated eval harness. They
are NOT part of the normal test suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Decision = Literal["none", "watch", "concerning", "urgent"]


@dataclass(frozen=True)
class EvalMessage:
    id: str
    sender: str  # child / other / other_2...
    text: str


@dataclass(frozen=True)
class ExpectedSnapshot:
    after_message_id: str
    # Usually one value; can allow two on genuinely borderline cases.
    acceptable_decisions: tuple[Decision, ...]
    required_categories: tuple[str, ...] = ()
    required_signals: tuple[str, ...] = ()
    forbidden_signals: tuple[str, ...] = ()
    # Later useful for evaluating AlertPolicy separately.
    should_alert: bool | None = None


@dataclass(frozen=True)
class GuardEvalCase:
    case_id: str
    family: str
    messages: tuple[EvalMessage, ...]
    snapshots: tuple[ExpectedSnapshot, ...]
    # Non-message facts the analyzer may know.
    context: dict[str, object] = field(default_factory=dict)
    # provenance
    source: str = "handwritten"
    notes: str = ""


__all__ = [
    "BULLYING",
    "GUARD_CASES",
    "TEASING",
    "UNKNOWN_CONTACT_ESCALATION",
    "Decision",
    "EvalMessage",
    "ExpectedSnapshot",
    "GuardEvalCase",
]


# ---------------------------------------------------------------------------
# Family 1 — unknown_contact_escalation
# ---------------------------------------------------------------------------
# This is a central family for Guard: the whole point is not to alert on a
# plain "hey", but to catch escalation. This case lets us measure:
#   Too early alert?      bad
#   Detected at m5?        good
#   Missed until m8?       maybe too late
#   Missed m8 entirely?    very bad

UNKNOWN_CONTACT_ESCALATION = GuardEvalCase(
    case_id="unknown_contact_escalation_001",
    family="unknown_contact_escalation",
    messages=(
        EvalMessage("m1", "other", "היי :)"),
        EvalMessage("m2", "child", "היי"),
        EvalMessage(
            "m3",
            "other",
            "עזבי, ראיתי אותך כמה פעמים יוצאת מהשער ליד המגרש :)",
        ),
        EvalMessage("m4", "child", "איך אתה יודע?"),
        EvalMessage(
            "m5",
            "other",
            "את בדרך כלל מחכה שם לבד אחרי הלימודים?",
        ),
        EvalMessage("m6", "child", "למה אתה שואל?"),
        EvalMessage(
            "m7",
            "other",
            "סתם. אל תספרי להורים עדיין, הם סתם יעשו מזה סיפור",
        ),
        EvalMessage(
            "m8",
            "other",
            "מחר אני יכול לבוא לשם לפני שאת הולכת",
        ),
    ),
    snapshots=(
        ExpectedSnapshot(
            after_message_id="m2",
            acceptable_decisions=("none",),
        ),
        ExpectedSnapshot(
            after_message_id="m3",
            acceptable_decisions=("watch",),
            required_signals=("offline_knowledge",),
        ),
        ExpectedSnapshot(
            after_message_id="m5",
            acceptable_decisions=("concerning",),
            required_categories=("suspicious_contact",),
            required_signals=("location_request", "routine_probing"),
        ),
        ExpectedSnapshot(
            after_message_id="m7",
            acceptable_decisions=("concerning", "urgent"),
            required_signals=("secrecy",),
        ),
        ExpectedSnapshot(
            after_message_id="m8",
            acceptable_decisions=("urgent",),
            required_signals=("secrecy", "meeting_request"),
        ),
    ),
)


# ---------------------------------------------------------------------------
# Family 2 — teasing_vs_bullying
# ---------------------------------------------------------------------------
# A contrast pair: one of the most important families for guarding against
# false positives. Same-ish language, but only one side is actually
# bullying.

# Case A — friends joking around.
TEASING = GuardEvalCase(
    case_id="teasing_vs_bullying_001_negative",
    family="teasing_vs_bullying",
    messages=(
        EvalMessage("m1", "other", "יא אפס 😂 שוב הפסדת"),
        EvalMessage("m2", "child", "חחחח סתום אתה יותר גרוע"),
        EvalMessage("m3", "other", "ברור ברור 😂"),
        EvalMessage("m4", "child", "מחר אני קורע אותך"),
        EvalMessage("m5", "other", "יאללה נראה אותך"),
    ),
    snapshots=(
        ExpectedSnapshot(
            after_message_id="m5",
            acceptable_decisions=("none",),
            forbidden_signals=("bullying", "threat"),
        ),
    ),
    source="SOC-adapted",
)


# Case B — same language roughly, but now it's bullying.
BULLYING = GuardEvalCase(
    case_id="teasing_vs_bullying_002_positive",
    family="teasing_vs_bullying",
    messages=(
        EvalMessage("m1", "other", "יא אפס שוב הפסדת"),
        EvalMessage("m2", "child", "די כבר"),
        EvalMessage("m3", "other", "מה די? כולם יודעים שאתה אפס"),
        EvalMessage("m4", "other_2", "חחח כן"),
        EvalMessage("m5", "child", "אמרתי לכם להפסיק"),
        EvalMessage("m6", "other", "אז פשוט אל תבוא מחר"),
        EvalMessage("m7", "other_2", "כן אף אחד לא רוצה אותך שם"),
    ),
    snapshots=(
        ExpectedSnapshot(
            after_message_id="m2",
            acceptable_decisions=("none", "watch"),
        ),
        ExpectedSnapshot(
            after_message_id="m5",
            acceptable_decisions=("watch", "concerning"),
            required_signals=("repeated_harassment",),
        ),
        ExpectedSnapshot(
            after_message_id="m7",
            acceptable_decisions=("concerning",),
            required_categories=("bullying",),
            required_signals=("repeated_harassment", "exclusion"),
        ),
    ),
    source="SynBullying-style",
)


GUARD_CASES: tuple[GuardEvalCase, ...] = (
    UNKNOWN_CONTACT_ESCALATION,
    TEASING,
    BULLYING,
)
