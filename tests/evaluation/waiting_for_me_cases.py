"""Labeled evaluation cases for the WaitingForMe analyzer.

Each case is a realistic WhatsApp conversation slice with the expected
:class:`WaitingForMeDecision`. Cases are bilingual (Hebrew/English) and
cover the three decision categories plus edge cases.

These cases are run against the real LLM API by the evaluation harness
(``tests/evaluation/test_waiting_for_me_eval.py``). They are NOT part of
the normal test suite — the eval marker prevents them from running by
default.
"""

from __future__ import annotations

from dataclasses import dataclass

from echo_v2.domain.waiting_for_me import WaitingForMeDecision

__all__ = ["EVAL_CASES", "EvalCase"]


@dataclass(frozen=True)
class EvalCase:
    """A single labeled evaluation case.

    Attributes:
        id: Short identifier for the case.
        description: What this case tests.
        messages: List of (direction, text) tuples. ``inbound`` = from
            the other person, ``outbound`` = from the user.
        expected: The expected :class:`WaitingForMeDecision`.
    """

    id: str
    description: str
    messages: list[tuple[str, str]]
    expected: WaitingForMeDecision
    notes: str = ""


EVAL_CASES: list[EvalCase] = [
    # --- WAITING_FOR_ME: direct questions ---------------------------------
    EvalCase(
        id="wfm-01",
        description="Direct yes/no question in Hebrew",
        messages=[("inbound", "אתה פנוי בחמישי?")],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
    ),
    EvalCase(
        id="wfm-02",
        description="Direct yes/no question in English",
        messages=[("inbound", "Are you free on Thursday?")],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
    ),
    EvalCase(
        id="wfm-03",
        description="Request to send something",
        messages=[("inbound", "תשלח לי את ההצעה?")],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
    ),
    EvalCase(
        id="wfm-04",
        description="Request for confirmation",
        messages=[("inbound", "מאשר את המחיר?")],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
    ),
    EvalCase(
        id="wfm-05",
        description="Request for decision",
        messages=[("inbound", "מה החלטת?")],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
    ),
    EvalCase(
        id="wfm-06",
        description="Follow-up on commitment",
        messages=[
            ("outbound", "אני אעדכן את הלקוח מחר"),
            ("inbound", "עדכנת כבר את הלקוח?"),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
    ),
    EvalCase(
        id="wfm-07",
        description="Explicit waiting statement",
        messages=[("inbound", "מחכה לתשובה שלך")],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
    ),
    EvalCase(
        id="wfm-08",
        description="Question after context — ball still with user",
        messages=[
            ("inbound", "ראית את המייל?"),
            ("outbound", "כן, אני אחזור אליך"),
            ("inbound", "מתי?"),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
    ),
    EvalCase(
        id="wfm-09",
        description="Request for file in English",
        messages=[("inbound", "Can you send me the file?")],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
    ),
    EvalCase(
        id="wfm-10",
        description="Scheduling request",
        messages=[("inbound", "When can we meet?")],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
    ),

    # --- NOT_WAITING_FOR_ME: closings, info, user's turn done -------------
    EvalCase(
        id="nwm-01",
        description="Simple thanks in Hebrew",
        messages=[("inbound", "תודה רבה")],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
    ),
    EvalCase(
        id="nwm-02",
        description="Simple thanks in English",
        messages=[("inbound", "Thanks!")],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
    ),
    EvalCase(
        id="nwm-03",
        description="Acknowledgment",
        messages=[("inbound", "קיבלתי, תודה")],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
    ),
    EvalCase(
        id="nwm-04",
        description="Closing confirmation",
        messages=[("inbound", "סגור")],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
    ),
    EvalCase(
        id="nwm-05",
        description="Status update from other person",
        messages=[("inbound", "אעדכן אותך")],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
    ),
    EvalCase(
        id="nwm-06",
        description="Informational message",
        messages=[("inbound", "הפגישה בשלוש")],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
    ),
    EvalCase(
        id="nwm-07",
        description="Request fulfilled then thanks",
        messages=[
            ("inbound", "תשלח לי את הקובץ?"),
            ("outbound", "שלחתי"),
            ("inbound", "קיבלתי, תודה"),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
    ),
    EvalCase(
        id="nwm-08",
        description="Other person will act",
        messages=[("inbound", "אני אבדוק וחוזר אליך")],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
    ),
    EvalCase(
        id="nwm-09",
        description="Sent for reference, no action needed",
        messages=[("inbound", "שלחתי לך לידיעה")],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
    ),
    EvalCase(
        id="nwm-10",
        description="Great / positive closing",
        messages=[("inbound", "מעולה!")],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
    ),

    # --- UNCERTAIN: ambiguous, media, missing context ---------------------
    EvalCase(
        id="unc-01",
        description="Empty text (media-only message)",
        messages=[("inbound", "")],
        expected=WaitingForMeDecision.UNCERTAIN,
    ),
    EvalCase(
        id="unc-02",
        description="Very vague single word",
        messages=[("inbound", "אולי")],
        expected=WaitingForMeDecision.UNCERTAIN,
    ),
    EvalCase(
        id="unc-03",
        description="Single emoji",
        messages=[("inbound", "👍")],
        expected=WaitingForMeDecision.UNCERTAIN,
    ),
    EvalCase(
        id="unc-04",
        description="Ambiguous without context",
        messages=[("inbound", "נשמע טוב")],
        expected=WaitingForMeDecision.UNCERTAIN,
        notes="Could be a closing or a response to a proposal — needs context.",
    ),

    # --- Mixed: conversation with context ---------------------------------
    EvalCase(
        id="mix-01",
        description="Question answered, then new question",
        messages=[
            ("inbound", "מה קורה עם הפרויקט?"),
            ("outbound", "הכל בסדר, אני עובד על זה"),
            ("inbound", "יש לך תאריך סיום?"),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
    ),
    EvalCase(
        id="mix-02",
        description="User asked, other person answered fully",
        messages=[
            ("outbound", "מה המחיר?"),
            ("inbound", "500 שקל, כולל מע״מ"),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="User asked, got an answer — ball is back with user to decide.",
    ),
    EvalCase(
        id="mix-03",
        description="Back and forth, ends with open question",
        messages=[
            ("inbound", "יש לך פנאי השבוע?"),
            ("outbound", "תלוי באיזה יום"),
            ("inbound", "יום חמישי?"),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
    ),
    EvalCase(
        id="mix-04",
        description="User confirms, other person acknowledges",
        messages=[
            ("inbound", "נראה לך שעובד?"),
            ("outbound", "כן, נראה טוב"),
            ("inbound", "מעולה, אז סגור"),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
    ),
]
