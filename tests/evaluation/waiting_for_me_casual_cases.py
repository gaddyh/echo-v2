"""Casual-question evaluation cases for the WaitingForMe analyzer.

A focused suite that probes the **actionability / responsibility threshold**
boundary — the next eval family after owner-inversion was addressed.

Unlike the SOC suites (which test classification correctness under the
existing spec), this suite encodes **desired product behavior**: a casual
social question that technically awaits an answer is labeled
``NOT_WAITING_FOR_ME`` when answering it does not create an actionable
interpersonal responsibility worth tracking in Echo. Actionable asks and
choices the other person is blocked on are labeled ``WAITING_FOR_ME``.

The suite is organized as 6 contrast pairs (12 cases). Each pair shares a
``family`` but each case has a unique ``source_group``, and the two sides
go in different splits (dev / test) so ``validate_no_leakage`` passes.

Cases are grounded in the 2026-09-18 production snapshots where the model
returned ``WAITING_FOR_ME`` 99% for casual questions like "מה שלומך?" and
"מה מצבו של אבא?". The suite is a measurement instrument: it is expected
to show a gap vs current model behavior, and that gap is exactly the
signal we want to collect before deciding whether to change the prompt.

These cases are run against the real LLM API by the evaluation harness
(``tests/evaluation/test_casual_eval.py``). They are NOT part of the
normal test suite — the ``eval_casual`` marker prevents them from running
by default.
"""

from __future__ import annotations

from echo_v2.domain.waiting_for_me import WaitingForMeDecision

from .waiting_for_me_cases import EvalCase
from .waiting_for_me_soc_cases import validate_no_leakage

__all__ = [
    "CASUAL_ALL_CASES",
    "CASUAL_DEV_CASES",
    "CASUAL_TEST_CASES",
]

_WFM = WaitingForMeDecision.WAITING_FOR_ME
_NWM = WaitingForMeDecision.NOT_WAITING_FOR_ME
_UNC = WaitingForMeDecision.UNCERTAIN


CASUAL_ALL_CASES: list[EvalCase] = [

    # ======================================================================
    # FAMILY 1 — CASUAL SOCIAL GREETING
    # Pure social questions that technically await a reply but should NOT
    # become responsibility items in Echo. Contrast with actionable asks.
    # ======================================================================

    EvalCase(
        id="cql-01",
        description="Pure social wellbeing question",
        messages=[("inbound", "מה שלומך?")],
        expected=_NWM,
        notes=(
            "Desired product behavior: a conversational social reply "
            "is not a responsibility Echo should track. Grounded in "
            "the סיון לביב snapshot (v3 returns WFM 99%)."
        ),
        family="casual_social_greeting",
        source_group="social_wellbeing",
        split="dev",
    ),

    EvalCase(
        id="cql-02",
        description="Social catch-up after a gap",
        messages=[("inbound", "מה נשמע? נשמע לי שעבר זמן 😊")],
        expected=_NWM,
        notes=(
            "A social catch-up opener. No action is blocked on the reply; "
            "answering is conversational, not a responsibility."
        ),
        family="casual_social_greeting",
        source_group="social_catchup",
        split="test",
    ),

    # ======================================================================
    # FAMILY 2 — STATUS INQUIRY WITHOUT DEPENDENCY
    # A status question about a third party with NO stated dependency on
    # the answer for the other person's action. Borderline but NWM.
    # ======================================================================

    EvalCase(
        id="cql-03",
        description="Status check about a third party, no action blocked",
        messages=[("inbound", "מה מצבו של אבא?")],
        expected=_NWM,
        notes=(
            "Desired product behavior: a pure status check with no stated "
            "decision blocked on the answer is not a responsibility. "
            "Grounded in the יעל אוחתי snapshot (v3 returns WFM 99%). "
            "Contrast with cql-05 where the same question is paired with "
            "an explicit dependency."
        ),
        family="status_inquiry_no_dependency",
        source_group="father_status_check",
        split="dev",
    ),

    EvalCase(
        id="cql-04",
        description="Wellbeing inquiry about a child, no action blocked",
        messages=[("inbound", "איך הקטנה מרגישה היום?")],
        expected=_NWM,
        notes=(
            "Same pattern as cql-03: a wellbeing inquiry with no action "
            "blocked on the reply."
        ),
        family="status_inquiry_no_dependency",
        source_group="child_status_check",
        split="test",
    ),

    # ======================================================================
    # FAMILY 3 — STATUS INQUIRY WITH DEPENDENCY
    # The same status question, but the other person explicitly states
    # their next action depends on the answer. Contrast pair with family 2.
    # ======================================================================

    EvalCase(
        id="cql-05",
        description="Status check with explicit decision dependency",
        messages=[
            ("inbound", "מה מצבו של אבא? עדכן אותי, אני צריך להחליט אם לנסוע מחר."),
        ],
        expected=_WFM,
        notes=(
            "The other person's decision (whether to travel) is blocked on "
            "the user's answer. This is the contrast to cql-03: the same "
            "question becomes a responsibility when an action depends on it."
        ),
        family="status_inquiry_with_dependency",
        source_group="father_status_decision",
        split="dev",
    ),

    EvalCase(
        id="cql-06",
        description="Wellbeing inquiry with a flight-cancellation dependency",
        messages=[
            ("inbound", "איך הקטנה מרגישה? תגידי לי כדי שאדע אם לבטל את הטיסה."),
        ],
        expected=_WFM,
        notes=(
            "The answer blocks a concrete decision (cancel a flight). "
            "Contrast to cql-04: the dependency makes it a responsibility."
        ),
        family="status_inquiry_with_dependency",
        source_group="child_status_flight",
        split="test",
    ),

    # ======================================================================
    # FAMILY 4 — ACTIONABLE REQUEST
    # Concrete asks where the other person needs the user to do something.
    # The "definitely responsibility" anchors.
    # ======================================================================

    EvalCase(
        id="cql-07",
        description="Concrete request to pick up the girls from school",
        messages=[("inbound", "אתה יכול לקחת את הילדות מבית הספר?")],
        expected=_WFM,
        notes=(
            "A concrete actionable request. Grounded in spirit by the "
            "מחמוד מטפל אבא snapshot (an actionable timing question)."
        ),
        family="actionable_request",
        source_group="school_pickup_request",
        split="dev",
    ),

    EvalCase(
        id="cql-08",
        description="Concrete request to call back when done",
        messages=[("inbound", "תתקשר אליי כשתסיים?")],
        expected=_WFM,
        notes="A concrete request for an action (a callback).",
        family="actionable_request",
        source_group="callback_request",
        split="test",
    ),

    # ======================================================================
    # FAMILY 5 — CONCRETE CHOICE BLOCKED
    # A yes/no or multi-option choice the other person is explicitly
    # waiting on to proceed. Casual tone ≠ casual consequence. Borderline
    # but leaning WFM: if someone needs your decision to act, it is a
    # responsibility even when the sentence is short and familial.
    # ======================================================================

    EvalCase(
        id="cql-09",
        description="Short yes/no choice the other person is blocked on",
        messages=[("inbound", "את רוצה את מוטי?")],
        expected=_WFM,
        notes=(
            "The other person needs a yes/no to proceed. Casual tone does "
            "not imply casual consequence. Grounded in the מתוקי שלי "
            "snapshot (v3 returns WFM 99%)."
        ),
        family="concrete_choice_blocked",
        source_group="moti_choice",
        split="dev",
    ),

    EvalCase(
        id="cql-10",
        description="Multi-option choice that blocks a booking",
        messages=[("inbound", "אני אזמין כרטיסים לשלישי או רביעי, תגידי.")],
        expected=_WFM,
        notes=(
            "The other person cannot book until the user chooses. The "
            "choice blocks a concrete action."
        ),
        family="concrete_choice_blocked",
        source_group="ticket_choice",
        split="test",
    ),

    # ======================================================================
    # FAMILY 6 — RHETORICAL / SOCIAL QUESTION
    # A question whose answer has no action consequence — pure social or
    # rhetorical. Contrast with families 4 and 5.
    # ======================================================================

    EvalCase(
        id="cql-11",
        description="Social sports chat, no action blocked",
        messages=[("inbound", "ראית את המשחק אתמול?")],
        expected=_NWM,
        notes=(
            "A social sports question. Answering is conversational; no "
            "action is blocked on the reply."
        ),
        family="rhetorical_social_question",
        source_group="game_chat",
        split="dev",
    ),

    EvalCase(
        id="cql-12",
        description="Catch-up question about a trip, no action blocked",
        messages=[("inbound", "איך היה הטיול בסוף?")],
        expected=_NWM,
        notes=(
            "A catch-up question. Answering is social, not a responsibility."
        ),
        family="rhetorical_social_question",
        source_group="trip_chat",
        split="test",
    ),
]


CASUAL_DEV_CASES: list[EvalCase] = [c for c in CASUAL_ALL_CASES if c.split == "dev"]
CASUAL_TEST_CASES: list[EvalCase] = [c for c in CASUAL_ALL_CASES if c.split == "test"]

# Validate at import time — fail fast if leakage is introduced.
# CRITICAL: pass CASUAL_ALL_CASES explicitly; without the argument the
# function defaults to SOC_ALL_CASES and would silently validate the
# wrong suite.
validate_no_leakage(CASUAL_ALL_CASES)
