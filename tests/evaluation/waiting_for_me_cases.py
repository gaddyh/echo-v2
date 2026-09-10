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

    # --- Long realistic conversations (5-10 messages) ---------------------
    # These simulate real WhatsApp threads with natural back-and-forth,
    # tangents, and the kind of messiness actual conversations have.

    EvalCase(
        id="long-01",
        description="Project negotiation ends with open question to user",
        messages=[
            ("inbound", "היי, ראיתי את ההצעה שלך לפרויקט"),
            ("outbound", "כן, שלחתי לך לפני כמה ימים"),
            ("inbound", "סליחה על העיכוב, הייתי עסוק"),
            ("outbound", "אין בעיה, תקרא ונדבר"),
            ("inbound", "קראתי, נראה לי יקר"),
            ("outbound", "זה כולל תחזוקה לשנה"),
            ("inbound", "אוקיי, ומה לגבי השלב השני?"),
            ("outbound", "שלב שני הוא אופציונלי, תלוי בך"),
            ("inbound", "כמה זה יעלה בנפרד?"),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="Ends with a direct question — the other person is waiting for a price.",
    ),
    EvalCase(
        id="long-02",
        description="Planning a meeting, user confirms, other person closes",
        messages=[
            ("inbound", "נראה לך שניפגש השבוע?"),
            ("outbound", "בטח, מה נוח לך?"),
            ("inbound", "יום שלישי או רביעי"),
            ("outbound", "שלישי בערב נראה לי"),
            ("inbound", "נהדר, באיזו שעה?"),
            ("outbound", "19:00?"),
            ("inbound", "מעולה, מקום אצלי או אצלך?"),
            ("outbound", "אצלי, אני אשלח כתובת"),
            ("inbound", "מצוין, תודה!"),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="Meeting fully planned and confirmed — the other person said thanks and closed.",
    ),
    EvalCase(
        id="long-03",
        description="User sends document, other person asks follow-up question",
        messages=[
            ("inbound", "אפשר לקבל את הדוח הכספי?"),
            ("outbound", "בטח, שולח עכשיו"),
            ("outbound", "הנה הקובץ"),
            ("inbound", "תודה, אני אבדוק"),
            ("inbound", "רגע, אתה בטוח שהמספרים מעודכנים?"),
            ("outbound", "כן, עדכנתי הכל אתמול"),
            ("inbound", "כי אני רואה פער בין חודש אוגוסט לספטמבר"),
            ("outbound", "אה, זה בגלל שהתחלנו לכלול הוצאות חד פעמיות"),
            ("inbound", "אוקיי, אבל אתה יכול לשלוח לי את הפירוט?"),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="Ends with a request for the user to send something — waiting for the user.",
    ),
    EvalCase(
        id="long-04",
        description="Casual chat with tangent, ends with other person closing",
        messages=[
            ("inbound", "מה נשמע?"),
            ("outbound", "הכל טוב, אתה?"),
            ("inbound", "גם בסדר, ראית את המשחק אתמול?"),
            ("outbound", "כן, היה משעמם"),
            ("inbound", "נכון, אני כמעט נרדמתי"),
            ("outbound", "חחח, אני צפיתי בזה רק בחצי"),
            ("inbound", "שיחתך עם דני על הנסיעה?"),
            ("outbound", "כן, דיברנו, הכל מסודר"),
            ("inbound", "מעולה, תודה על העדכון"),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="Casual chat with a tangent about a game, ends with thanks — no one is waiting.",
    ),
    EvalCase(
        id="long-05",
        description="Client chases twice, user promises but doesn't deliver",
        messages=[
            ("inbound", "היי, שלחת לי את החוזה?"),
            ("outbound", "כן, אני אשלח היום"),
            ("inbound", "אוקיי מחכה"),
            ("inbound", "היי, עדיין לא קיבלתי"),
            ("outbound", "סליחה, אני אשלח עכשיו"),
            ("inbound", "אוקיי"),
            ("inbound", "עדיין כלום..."),
            ("outbound", "רגע, יש בעיה עם המייל, אני בודק"),
            ("inbound", "תעדכן אותי בבקשה"),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="Client has been waiting and chasing — the last message is a request for the user to update them.",
    ),
    EvalCase(
        id="long-06",
        description="User asks question, gets full answer, conversation closes",
        messages=[
            ("outbound", "היי, אתה יודע מה המחיר למנוי שנתי?"),
            ("inbound", "תלוי בחבילה"),
            ("outbound", "החבילה הבסיסית"),
            ("inbound", "240 שקל לחודש או 2400 לשנה"),
            ("outbound", "ויש הנחה לתשלום שנתי?"),
            ("inbound", "כן, חיסכון של 480 שקל"),
            ("outbound", "מעולה, תודה רבה"),
            ("inbound", "בשמחה, דבר אם תרצה להתקדם"),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="User asked, got answers, said thanks. The other person offered help but the user hasn't responded — ball is with the user but no one is waiting.",
    ),
    EvalCase(
        id="long-07",
        description="Group-like thread, ends with direct question to user",
        messages=[
            ("inbound", "האם מישהו יכול לשלוח את סיכום הפגישה?"),
            ("outbound", "אני אשלח הערב"),
            ("inbound", "תודה!"),
            ("inbound", "ומה עם רשימת המשתתפים?"),
            ("outbound", "גם את זה, אני אצרף"),
            ("inbound", "מעולה"),
            ("inbound", "רק תזכור לשלוח גם את הצ'ק-ליסט"),
            ("outbound", "בטח"),
            ("inbound", "שלחת כבר?"),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The other person is asking if the user sent the documents — waiting for the user.",
    ),
    EvalCase(
        id="long-08",
        description="Negotiation back-and-forth, ends with counter-offer to user",
        messages=[
            ("inbound", "אני מציע 5000"),
            ("outbound", "זה נמוך מדי, אני חושב על 7000"),
            ("inbound", "בוא ניפגש באמצע, 6000"),
            ("outbound", "אני יכול לרדת ל6500"),
            ("inbound", "תעשה 6200 וסגור"),
            ("outbound", "עם תשלום בשני תשלומים?"),
            ("inbound", "בסדר, שני תשלומים"),
            ("outbound", "אוקיי, שולח חוזה"),
            ("inbound", "מתי תשלח?"),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The other person is asking when the user will send the contract — waiting for the user.",
    ),
    EvalCase(
        id="long-09",
        description="Friend asks for favor, user agrees, friend confirms thanks",
        messages=[
            ("inbound", "אתה יכול לאסוף אותי משדה התעופה?"),
            ("outbound", "בטח, מתי?"),
            ("inbound", "מחר בבוקר, 8:00"),
            ("outbound", "אוקיי, אני אהיה שם"),
            ("inbound", "תודה רבה! אתה מלאך"),
            ("outbound", "חחח אין בעיה"),
            ("inbound", "אני אשלח לך את מספר הטיסה"),
            ("outbound", "מעולה, מחכה"),
            ("inbound", "שלחתי, תודה שוב"),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="Favor agreed, flight number sent, friend said thanks — conversation closed.",
    ),
    EvalCase(
        id="long-10",
        description="Work thread, user delegates, other person asks for clarification",
        messages=[
            ("inbound", "מה קורה עם הלקוח החדש?"),
            ("outbound", "דיברתי איתם, הם מעוניינים"),
            ("inbound", "מעולה, מה הצעד הבא?"),
            ("outbound", "אני צריך לשלוח להם הצעת מחיר"),
            ("inbound", "תעשה את זה השבוע"),
            ("outbound", "בסדר, אני אכין מסמך"),
            ("inbound", "ותעדכן אותי מתי שלחת"),
            ("outbound", "בטח"),
            ("inbound", "ראית את המייל שלהם? הם שלחו דרישות חדשות"),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="Ends with a direct question — the other person is asking if the user saw the email.",
    ),
    EvalCase(
        id="long-11",
        description="Long thread with multiple topics, ends with thanks",
        messages=[
            ("inbound", "היי, יש לך את המצגת מהפגישה?"),
            ("outbound", "כן, אני אשלח"),
            ("inbound", "תודה"),
            ("outbound", "שלחתי למייל"),
            ("inbound", "קיבלתי, תודה רבה"),
            ("inbound", "לגבי התאריך הבא — נראה לך ה-15?"),
            ("outbound", "כן, ה-15 נראה לי טוב"),
            ("inbound", "מעולה, אני אשלח יומן"),
            ("outbound", "מצוין"),
            ("inbound", "סגור, תודה על הכל"),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="Multiple topics resolved, ends with a clear closing — no one is waiting.",
    ),
    EvalCase(
        id="long-12",
        description="User asks for advice, gets it, but then asked a follow-up",
        messages=[
            ("outbound", "מה דעתך על ההצעה הזו?"),
            ("inbound", "נראה לי שהמחיר הוגן"),
            ("outbound", "אתה חושב שכדאי לנהל משא ומתן?"),
            ("inbound", "תלוי כמה לחוץ"),
            ("outbound", "יש לי עוד שבוע"),
            ("inbound", "אז נסה להוריד 10%"),
            ("outbound", "אוקיי, אני אשלח הצעת נגד"),
            ("inbound", "שלח לי לראות לפני שאתה שולח"),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The other person asked to see the counter-offer before the user sends it — waiting for the user.",
    ),
]
