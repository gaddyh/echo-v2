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
Those are too brittle. We check analyzer semantics via
``required_categories``, ``required_signals``, evidence quality, and
``forbidden_categories``/``forbidden_signals``. Severity is derived separately
by deterministic policy.

Cases are run against the real LLM API by a dedicated eval harness. They
are NOT part of the normal test suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from echo_v2.services.guard_taxonomy import (
    CATEGORY_SIGNALS,
    GuardCategory,
    GuardDecision,
    GuardSignal,
)


@dataclass(frozen=True)
class EvalMessage:
    id: str
    sender: str  # child / other / other_2...
    text: str


@dataclass(frozen=True)
class ExpectedSnapshot:
    after_message_id: str
    # Usually one value; can allow two on genuinely borderline cases.
    required_categories: tuple[str, ...] = ()
    required_signals: tuple[str, ...] = ()
    required_signal_any_of: tuple[str, ...] = ()
    forbidden_categories: tuple[str, ...] = ()
    forbidden_signals: tuple[str, ...] = ()
    required_evidence_message_ids: tuple[str, ...] = ()
    expected_decision: str | None = None
    expect_clean: bool = False


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


def validate_guard_case(case: GuardEvalCase) -> None:
    """Validate snapshot gold before an API-backed evaluation runs."""
    message_ids = tuple(message.id for message in case.messages)
    if len(message_ids) != len(set(message_ids)):
        raise ValueError(f"{case.case_id}: message IDs must be unique")
    message_positions = {message_id: index for index, message_id in enumerate(message_ids)}
    seen_snapshots: set[str] = set()
    previous_position = -1

    for snapshot in case.snapshots:
        after_message_id = snapshot.after_message_id
        if after_message_id not in message_positions:
            raise ValueError(
                f"{case.case_id}: unknown snapshot message ID {after_message_id!r}"
            )
        if after_message_id in seen_snapshots:
            raise ValueError(
                f"{case.case_id}: duplicate snapshot for {after_message_id!r}"
            )
        position = message_positions[after_message_id]
        if position <= previous_position:
            raise ValueError(
                f"{case.case_id}: snapshots must be in chronological order"
            )
        seen_snapshots.add(after_message_id)
        previous_position = position

        required_signals = set(snapshot.required_signals)
        forbidden_signals = set(snapshot.forbidden_signals)
        if required_signals & forbidden_signals:
            raise ValueError(
                f"{case.case_id} @ {after_message_id}: required and forbidden signals overlap"
            )
        if snapshot.required_signal_any_of:
            if len(snapshot.required_signal_any_of) != len(
                set(snapshot.required_signal_any_of)
            ):
                raise ValueError(
                    f"{case.case_id} @ {after_message_id}: any-of signals must be unique"
                )
            if set(snapshot.required_signal_any_of) & forbidden_signals:
                raise ValueError(
                    f"{case.case_id} @ {after_message_id}: any-of signal is forbidden"
                )

        required_categories = set(snapshot.required_categories)
        forbidden_categories = set(snapshot.forbidden_categories)
        if required_categories & forbidden_categories:
            raise ValueError(
                f"{case.case_id} @ {after_message_id}: required and forbidden categories overlap"
            )
        if snapshot.expected_decision is not None:
            try:
                GuardDecision(snapshot.expected_decision)
            except ValueError as exc:
                raise ValueError(
                    f"{case.case_id} @ {after_message_id}: unknown decision "
                    f"{snapshot.expected_decision!r}"
                ) from exc
        if snapshot.expect_clean and (
            required_categories
            or required_signals
            or snapshot.required_signal_any_of
            or snapshot.required_evidence_message_ids
            or (
                snapshot.expected_decision is not None
                and GuardDecision(snapshot.expected_decision) is not GuardDecision.NONE
            )
        ):
            raise ValueError(
                f"{case.case_id} @ {after_message_id}: clean snapshot has positive requirements"
            )

        if not set(snapshot.required_evidence_message_ids) <= set(message_ids):
            raise ValueError(
                f"{case.case_id} @ {after_message_id}: required evidence ID is unknown"
            )

        possible_support = required_signals | set(snapshot.required_signal_any_of)
        for category in required_categories:
            try:
                category_signals = CATEGORY_SIGNALS[GuardCategory(category)]
            except ValueError as exc:
                raise ValueError(
                    f"{case.case_id} @ {after_message_id}: unknown category {category!r}"
                ) from exc
            if not category_signals & {
                GuardSignal(signal) for signal in possible_support
            }:
                raise ValueError(
                    f"{case.case_id} @ {after_message_id}: category {category!r} "
                    "has no required or acceptable supporting signal"
                )

        for signal in required_signals | forbidden_signals | set(
            snapshot.required_signal_any_of
        ):
            try:
                GuardSignal(signal)
            except ValueError as exc:
                raise ValueError(
                    f"{case.case_id} @ {after_message_id}: unknown signal {signal!r}"
                ) from exc


def validate_guard_cases(cases: tuple[GuardEvalCase, ...]) -> None:
    """Validate all cases and reject duplicate case IDs."""
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("Guard case IDs must be unique")
    for case in cases:
        validate_guard_case(case)


__all__ = [
    "BULLYING",
    "GUARD_CASES",
    "TEASING",
    "UNKNOWN_CONTACT_ESCALATION",
    "EvalMessage",
    "ExpectedSnapshot",
    "GuardEvalCase",
    "validate_guard_case",
    "validate_guard_cases",
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
            expect_clean=True,
        ),
        ExpectedSnapshot(
            after_message_id="m3",
            required_signals=(GuardSignal.OFFLINE_KNOWLEDGE,),
        ),
        ExpectedSnapshot(
            after_message_id="m5",
            required_categories=(GuardCategory.SUSPICIOUS_CONTACT,),
            required_signals=(
                GuardSignal.OFFLINE_KNOWLEDGE,
                GuardSignal.ROUTINE_PROBING,
            ),
        ),
        ExpectedSnapshot(
            after_message_id="m7",
            required_signals=(GuardSignal.SECRECY_REQUEST,),
        ),
        ExpectedSnapshot(
            after_message_id="m8",
            required_signals=(GuardSignal.SECRECY_REQUEST, GuardSignal.MEETING_REQUEST),
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
            expect_clean=True,
            forbidden_signals=(
                GuardSignal.REPEATED_TARGETING,
                GuardSignal.GROUP_PILE_ON,
                GuardSignal.THREAT,
            ),
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
        ),
        ExpectedSnapshot(
            after_message_id="m5",
            required_signals=(GuardSignal.INSULT_OR_HUMILIATION,),
            required_signal_any_of=(
                GuardSignal.REPEATED_TARGETING,
                GuardSignal.GROUP_PILE_ON,
            ),
        ),
        ExpectedSnapshot(
            after_message_id="m7",
            required_categories=(GuardCategory.BULLYING,),
            required_signals=(
                GuardSignal.INSULT_OR_HUMILIATION,
                GuardSignal.EXCLUSION,
            ),
            required_signal_any_of=(
                GuardSignal.REPEATED_TARGETING,
                GuardSignal.GROUP_PILE_ON,
            ),
        ),
    ),
    source="SynBullying-style",
)


GUARD_CASES: tuple[GuardEvalCase, ...] = (
    UNKNOWN_CONTACT_ESCALATION,
    TEASING,
    BULLYING,
)
