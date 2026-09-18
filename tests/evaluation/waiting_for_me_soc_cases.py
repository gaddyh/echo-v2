from __future__ import annotations

from dataclasses import replace
from typing import Literal

from echo_v2.domain.waiting_for_me import WaitingForMeDecision

from .waiting_for_me_cases import EvalCase

__all__ = [
    "SOC_ALL_CASES",
    "SOC_CASE_METADATA",
    "SOC_DEV_CASES",
    "SOC_FAMILY_CASES",
    "SOC_FAMILY_CASES_2",
    "SOC_TEST_CASES",
    "validate_no_leakage",
]

Split = Literal["train", "dev", "test"]

SOC_FAMILY_CASES: list[EvalCase] = [

    # ======================================================================
    # FAMILY 1 — USER-CREATED COMMITMENT
    # No inbound request is required. The user creates an obligation.
    # ======================================================================

    EvalCase(
        id="socf-01",
        description="User creates commitment without being asked",
        messages=[
            ("inbound", "המצגת עדיין קצת מבולגנת."),
            ("outbound", "כן, ראיתי. אני אסדר את השקף הרביעי מחר."),
            ("inbound", "מעולה, תודה."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The user voluntarily created an unfulfilled future commitment.",
    ),

    EvalCase(
        id="socf-02",
        description="User volunteers to check something later",
        messages=[
            ("inbound", "משהו נראה לי מוזר במספרים של אוגוסט."),
            ("outbound", "אני אבדוק את זה בערב."),
            ("inbound", "סבבה."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="'I'll check' creates an open obligation even though nobody explicitly requested it.",
    ),


    # ======================================================================
    # FAMILY 2 — ACKNOWLEDGEMENT IS NOT FULFILLMENT
    # ======================================================================

    EvalCase(
        id="socf-03",
        description="Acknowledging a review request does not complete it",
        messages=[
            ("inbound", "תוכל לעבור על החוזה לפני שאני שולחת אותו?"),
            ("outbound", "ראיתי 👍"),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The requested review remains undone.",
    ),

    EvalCase(
        id="socf-04",
        description="Thanks after action request is not completion",
        messages=[
            ("inbound", "אני צריכה את החתימה שלך עד מחר."),
            ("outbound", "תודה, קיבלתי."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="Receipt acknowledgement must not be confused with performing the requested action.",
    ),


    # ======================================================================
    # FAMILY 3 — BALL HANDED TO THEM
    # WFM -> waiting for them.
    # ======================================================================

    EvalCase(
        id="socf-05",
        description="User needs information from other person before acting",
        messages=[
            ("inbound", "אתה יכול להכין לי הצעת מחיר?"),
            ("outbound", "כן. תשלחי לי קודם את המידות."),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="The quote is blocked; the next actionable step belongs to the other person.",
    ),

    EvalCase(
        id="socf-06",
        description="Scheduling ball explicitly handed to other person",
        messages=[
            ("inbound", "רוצה להיפגש השבוע?"),
            ("outbound", "כן, בכיף. תשלח לי יום ושעה שמתאימים לך."),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="Open conversation, but there is currently nothing actionable for the user.",
    ),


    # ======================================================================
    # FAMILY 4 — DEPENDENCY SATISFIED, OBLIGATION REACTIVATES
    # waiting for them -> WFM
    # ======================================================================

    EvalCase(
        id="socf-07",
        description="Missing information arrives and reactivates user's task",
        messages=[
            ("inbound", "אתה יכול להכין לי הצעת מחיר?"),
            ("outbound", "כן. תשלחי לי קודם את המידות."),
            ("inbound", "140 על 80."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The dependency was satisfied; the user's original quote commitment is actionable again.",
    ),

    EvalCase(
        id="socf-08",
        description="External dependency completes and user's commitment becomes active",
        messages=[
            ("outbound", "אני אסיים את הדוח כשדניאל ישלח את המספרים."),
            ("inbound", "דניאל העלה עכשיו את המספרים לתיקייה."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The condition blocking the user's commitment is now satisfied.",
    ),


    # ======================================================================
    # FAMILY 5 — CANCELLATION / SUPERSESSION
    # ======================================================================

    EvalCase(
        id="socf-09",
        description="Other person cancels an outstanding request",
        messages=[
            ("inbound", "תוכל לעבור הערב על הטיוטה?"),
            ("outbound", "כן, אעשה את זה."),
            ("inbound", "עזוב, מאיה כבר עברה עליה."),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="A later message explicitly cancels the user's obligation.",
    ),

    EvalCase(
        id="socf-10",
        description="Request becomes unnecessary",
        messages=[
            ("inbound", "תשלח לי את הקובץ כשאתה מגיע הביתה."),
            ("outbound", "בסדר."),
            ("inbound", "לא צריך בסוף, מצאתי אותו."),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="The old obligation must not remain open after it becomes unnecessary.",
    ),


    # ======================================================================
    # FAMILY 6 — OBLIGATION REPLACED
    # ======================================================================

    EvalCase(
        id="socf-11",
        description="Original task replaced by a simpler task",
        messages=[
            ("inbound", "תוכל לשלוח לי את האקסל?"),
            ("outbound", "כן, בערב."),
            ("inbound", "בעצם לא צריך את הקובץ, רק תשלח לי את הסכום הכולל."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The old obligation is superseded, but a new obligation remains.",
    ),

    EvalCase(
        id="socf-12",
        description="User replaces promised file with promised phone call",
        messages=[
            ("inbound", "תוכל להסביר לי את השינויים במסמך?"),
            ("outbound", "כן, אכתוב לך הסבר."),
            ("outbound", "בעצם יותר פשוט שאצלצל אליך בערב."),
            ("inbound", "סבבה."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="There should be one current obligation: call, not both write and call.",
    ),


    # ======================================================================
    # FAMILY 7 — MULTIPLE SIMULTANEOUS OBLIGATIONS
    # ======================================================================

    EvalCase(
        id="socf-13",
        description="One of several requested actions completed",
        messages=[
            ("inbound", "תשלח לי את המצגת."),
            ("inbound", "וגם תגיד אם יום שלישי מתאים לך."),
            ("inbound", "ואני צריכה את המחיר שסיכמנו."),
            ("outbound", "שלישי מתאים."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="Two obligations remain even though one was answered.",
    ),

    EvalCase(
        id="socf-14",
        description="Two tasks completed but one earlier task remains",
        messages=[
            ("inbound", "תשלח לי את המצגת."),
            ("inbound", "וגם תגיד אם שלישי מתאים."),
            ("inbound", "ומה היה המחיר שסיכמנו?"),
            ("outbound", "שלישי מתאים."),
            ("outbound", "המצגת מצורפת."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The pricing question is still unanswered.",
    ),


    # ======================================================================
    # FAMILY 8 — BURIED OBLIGATION / TOPIC DRIFT
    # ======================================================================

    EvalCase(
        id="socf-15",
        description="Old commitment remains open after casual topic drift",
        messages=[
            ("inbound", "תשלח לי את הכתובת כשתוכל."),
            ("outbound", "כן."),
            ("inbound", "אגב ראית את המשחק אתמול?"),
            ("outbound", "כן, איזה סוף מטורף."),
            ("inbound", "לגמרי 😂"),
            ("outbound", "לא האמנתי שהם ניצחו."),
            ("inbound", "גם אני."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="Casual conversation must not erase an unresolved earlier request.",
    ),

    EvalCase(
        id="socf-16",
        description="Old user commitment survives unrelated conversation",
        messages=[
            ("outbound", "אני אשלח לך בערב את הלינק."),
            ("inbound", "מעולה."),
            ("inbound", "איך היה הטיול בסוף?"),
            ("outbound", "אחלה, היה ממש כיף."),
            ("inbound", "איזה יופי."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The later social exchange does not fulfill the earlier promise.",
    ),


    # ======================================================================
    # FAMILY 9 — SOFT / IMPLICIT REQUESTS
    # ======================================================================

    EvalCase(
        id="socf-17",
        description="Implicit request for feedback",
        messages=[
            ("inbound", "יהיה ממש טוב לקבל את הפידבק שלך לפני הפגישה מחר."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="No explicit question, but there is a clear expectation for action.",
    ),

    EvalCase(
        id="socf-18",
        description="Implicit approval request",
        messages=[
            ("inbound", "הדבר היחיד שחסר כרגע זה האישור שלך."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="A declarative sentence can still assign the next action to the user.",
    ),


    # ======================================================================
    # FAMILY 10 — OFFER IS NOT AN OBLIGATION
    # Important precision / false-positive family.
    # ======================================================================

    EvalCase(
        id="socf-19",
        description="Optional offer does not require reply",
        messages=[
            ("inbound", "אם תרצה אני יכול לשלוח לך גם את הגרסה המלאה."),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="An optional offer should not automatically create WFM.",
    ),

    EvalCase(
        id="socf-20",
        description="'Let me know if you want it' is optional",
        messages=[
            ("inbound", "תגיד לי אם תרצה שאשלח לך את כל הפרטים."),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="No answer is actually owed unless the user wants to proceed.",
    ),


    # ======================================================================
    # FAMILY 11 — OFFER-LIKE LANGUAGE THAT DOES REQUIRE AN ANSWER
    # Contrast pair with family 10.
    # ======================================================================

    EvalCase(
        id="socf-21",
        description="'Let me know which option' requires a decision",
        messages=[
            ("inbound", "תגיד לי איזו מהאפשרויות אתה מעדיף כדי שאוכל להזמין."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The other person's next action is blocked on the user's decision.",
    ),

    EvalCase(
        id="socf-22",
        description="Other person explicitly waits for user's selection",
        messages=[
            ("inbound", "יש אדום, שחור או כחול. תחזיר לי תשובה איזה לקחת."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The choice is required, not merely offered.",
    ),


    # ======================================================================
    # FAMILY 12 — SUGGESTION IS NOT A REQUEST
    # ======================================================================

    EvalCase(
        id="socf-23",
        description="Advice to user does not create obligation",
        messages=[
            ("inbound", "נראה לי שכדאי לך לעדכן את קורות החיים."),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="A recommendation about what the user should do is not necessarily something the sender is waiting for.",
    ),

    EvalCase(
        id="socf-24",
        description="Casual suggestion does not require completion",
        messages=[
            ("inbound", "שווה לך לבדוק מתישהו את הגרסה החדשה."),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="'You should check this sometime' is different from a request to check it for the sender.",
    ),


    # ======================================================================
    # FAMILY 13 — CONDITIONAL COMMITMENT
    # ======================================================================

    EvalCase(
        id="socf-25",
        description="Commitment is not active until condition occurs",
        messages=[
            ("outbound", "אם הלקוח יאשר, אני אשלח לו את החשבונית."),
            ("inbound", "סבבה."),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="The user's obligation is conditional and not currently actionable.",
    ),

    EvalCase(
        id="socf-26",
        description="Condition becomes true and activates commitment",
        messages=[
            ("outbound", "אם הלקוח יאשר, אני אשלח לו את החשבונית."),
            ("inbound", "הוא אישר עכשיו."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The previously conditional commitment is now actionable.",
    ),


    # ======================================================================
    # FAMILY 14 — SCHEDULING OWNERSHIP SWITCHES
    # ======================================================================

    EvalCase(
        id="socf-27",
        description="Scheduling question currently requires user choice",
        messages=[
            ("inbound", "שלישי או רביעי יותר טוב לך?"),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
    ),

    EvalCase(
        id="socf-28",
        description="User answers scheduling question and other person owns next step",
        messages=[
            ("inbound", "שלישי או רביעי יותר טוב לך?"),
            ("outbound", "שלישי."),
            ("inbound", "סבבה, אני אבדוק שעה ואחזור אליך."),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="The other person explicitly owns the next scheduling step.",
    ),

    EvalCase(
        id="socf-29",
        description="Scheduling ownership returns to user",
        messages=[
            ("inbound", "שלישי או רביעי יותר טוב לך?"),
            ("outbound", "שלישי."),
            ("inbound", "סבבה, 19:00 מתאים?"),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The ball switches back to the user for confirmation.",
    ),


    # ======================================================================
    # FAMILY 15 — FOLLOW-UP / CHASING
    # ======================================================================

    EvalCase(
        id="socf-30",
        description="Promise remains open even before recipient follows up",
        messages=[
            ("inbound", "תוכל לשלוח לי את הטופס?"),
            ("outbound", "כן, אשלח הערב."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="A chase is not required for the obligation to exist.",
    ),

    EvalCase(
        id="socf-31",
        description="Recipient follows up on missed commitment",
        messages=[
            ("inbound", "תוכל לשלוח לי את הטופס?"),
            ("outbound", "כן, אשלח הערב."),
            ("inbound", "היי, מזכיר לגבי הטופס."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="Follow-up strengthens the evidence but does not create a fundamentally new obligation.",
    ),


    # ======================================================================
    # FAMILY 16 — REPLY VS ACTION
    # ======================================================================

    EvalCase(
        id="socf-32",
        description="Yes answers a question completely",
        messages=[
            ("inbound", "אתה חושב שהמחיר הזה הוגן?"),
            ("outbound", "כן."),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="A reply was requested and supplied.",
    ),

    EvalCase(
        id="socf-33",
        description="Yes does not perform requested action",
        messages=[
            ("inbound", "תוכל לעדכן את המחיר באקסל?"),
            ("outbound", "כן."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="'Yes' accepts the task; it does not complete the task.",
    ),


    # ======================================================================
    # FAMILY 17 — OLD OBLIGATION CLOSES, NEW ONE OPENS
    # ======================================================================

    EvalCase(
        id="socf-34",
        description="Question answered but new action request opens immediately",
        messages=[
            ("inbound", "קיבלת את הקובץ?"),
            ("outbound", "כן."),
            ("inbound", "מעולה. תוכל לעבור עליו עד יום שישי?"),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The first obligation is resolved; a second one is now open.",
    ),

    EvalCase(
        id="socf-35",
        description="Accepting new task does not complete it",
        messages=[
            ("inbound", "קיבלת את הקובץ?"),
            ("outbound", "כן."),
            ("inbound", "מעולה. תוכל לעבור עליו עד יום שישי?"),
            ("outbound", "בטח."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The review is still pending despite the affirmative response.",
    ),


    # ======================================================================
    # FAMILY 18 — IMPLICIT COMPLETION
    # ======================================================================

    EvalCase(
        id="socf-36",
        description="Requested information itself completes the task",
        messages=[
            ("inbound", "מה הכתובת?"),
            ("outbound", "הרצל 12, תל אביב."),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="No explicit 'done' marker is needed; the answer itself fulfills the request.",
    ),

    EvalCase(
        id="socf-37",
        description="Requested content itself fulfills the obligation",
        messages=[
            ("inbound", "תשלח לי את מספר ההזמנה."),
            ("outbound", "458921."),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="The content of the outbound message is the requested action.",
    ),


    # ======================================================================
    # FAMILY 19 — SELF-CORRECTION / RETRACTION
    # ======================================================================

    EvalCase(
        id="socf-38",
        description="User changes deadline of their own commitment",
        messages=[
            ("outbound", "אני אשלח לך את זה מחר."),
            ("outbound", "בעצם מחר אני לא במשרד — אשלח ביום שישי."),
            ("inbound", "בסדר."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The commitment survives, but its timing was updated.",
    ),

    EvalCase(
        id="socf-39",
        description="User retracts commitment and transfers task",
        messages=[
            ("outbound", "אני אדבר עם דני."),
            ("outbound", "בעצם עדיף שאתה תדבר איתו, אתה מכיר אותו יותר טוב."),
            ("inbound", "סבבה, אדבר איתו."),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="The user's original commitment was explicitly transferred away.",
    ),

    EvalCase(
        id="socf-40",
        description="Sender retracts accidental request",
        messages=[
            ("inbound", "תשלח לי בבקשה את החשבונית."),
            ("inbound", "רגע סליחה, הודעה לא נכונה 😅"),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="The second inbound message invalidates the first request.",
    ),
]


SOC_FAMILY_CASES_2: list[EvalCase] = [

    # ======================================================================
    # FAMILY 1 — USER-CREATED COMMITMENT
    # ======================================================================

    EvalCase(
        id="socf-41",
        description="User volunteers to send movie links later",
        messages=[
            ("inbound", "הסרט הזה נשמע בדיוק מה שאני צריך עכשיו."),
            ("outbound", "יש לי עוד שניים בסגנון. אשלח לך את הלינקים אחרי העבודה."),
            ("inbound", "מושלם, תודה."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="User created an obligation without being explicitly asked.",
    ),

    EvalCase(
        id="socf-42",
        description="User volunteers to send useful contact",
        messages=[
            ("inbound", "אני כבר לא יודעת למי לפנות לגבי הגינה הזאת."),
            ("outbound", "יש לי עורכת דין שעזרה במקרה דומה. אשלח לך את הפרטים."),
            ("inbound", "וואו, תודה."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The promised contact information has not yet been sent.",
    ),


    # ======================================================================
    # FAMILY 2 — ACKNOWLEDGEMENT IS NOT FULFILLMENT
    # ======================================================================

    EvalCase(
        id="socf-43",
        description="User acknowledges request to inspect something",
        messages=[
            ("inbound", "היתר שוב נדחה. צריך עוד זוג עיניים על הרצפה הזאת."),
            ("inbound", "אתה יכול לעבור על התמונות מחר?"),
            ("outbound", "ראיתי."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="Seeing the request is not the same as reviewing the material.",
    ),

    EvalCase(
        id="socf-44",
        description="User acknowledges request for image frame",
        messages=[
            ("inbound", "ראיתי משהו מוזר במצלמה."),
            ("inbound", "אתה יכול לשלוח לי את הפריים שצילמת?"),
            ("outbound", "כן, ראיתי את ההודעה."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The requested frame is still owed.",
    ),


    # ======================================================================
    # FAMILY 3 — BALL HANDED TO THEM
    # ======================================================================

    EvalCase(
        id="socf-45",
        description="User needs timestamp before retrieving requested footage",
        messages=[
            ("inbound", "אתה יכול לשלוח לי את הקטע מהמצלמה?"),
            ("outbound", "כן. תשלח לי קודם בערך באיזו שעה זה קרה."),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="The user's task is blocked until the other person provides the timestamp.",
    ),

    EvalCase(
        id="socf-46",
        description="User agrees to meeting but asks other person to pick date",
        messages=[
            ("inbound", "חייבים סוף סוף לשבת על זה פנים מול פנים."),
            ("outbound", "מסכים. תגיד לי תאריך שמתאים לך ואני אחסום ביומן."),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="The next scheduling action belongs to the other person.",
    ),


    # ======================================================================
    # FAMILY 4 — DEPENDENCY SATISFIED, OBLIGATION REACTIVATES
    # ======================================================================

    EvalCase(
        id="socf-47",
        description="Timestamp arrives and user's footage task becomes actionable",
        messages=[
            ("inbound", "אתה יכול לשלוח לי את הקטע מהמצלמה?"),
            ("outbound", "כן. תשלח לי קודם בערך באיזו שעה זה קרה."),
            ("inbound", "03:32 בערך."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The missing dependency was supplied, so the user's original task is active again.",
    ),

    EvalCase(
        id="socf-48",
        description="Requested document details arrive and unblock user's review",
        messages=[
            ("inbound", "תוכל לבדוק למה ההיתר נדחה?"),
            ("outbound", "כן, אבל אני צריך את הסעיף שהם סימנו."),
            ("inbound", "סעיף 4.2, שלחתי צילום עכשיו."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The prerequisite for the review has now been provided.",
    ),


    # ======================================================================
    # FAMILY 5 — CANCELLATION / SUPERSESSION
    # ======================================================================

    EvalCase(
        id="socf-49",
        description="Sender cancels review because wrong version was sent",
        messages=[
            ("inbound", "תעבור בבקשה על הקובץ ששלחתי."),
            ("outbound", "בסדר, אסתכל הערב."),
            ("inbound", "רגע, אל תבדוק אותו. שלחתי לך גרסה ישנה בטעות."),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="The outstanding review request was explicitly cancelled.",
    ),

    EvalCase(
        id="socf-50",
        description="Future commitment disappears because event was cancelled",
        messages=[
            ("inbound", "אתה עדיין יכול להגיע בשבת ולעזור לנו?"),
            ("outbound", "כן, אהיה שם בעשר."),
            ("inbound", "ביטלו את האירוע בגלל מזג האוויר. לא צריך להגיע."),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="The real-world commitment no longer exists.",
    ),


    # ======================================================================
    # FAMILY 6 — OBLIGATION REPLACED
    # ======================================================================

    EvalCase(
        id="socf-51",
        description="Raw-data request replaced by metadata request",
        messages=[
            ("inbound", "תשלח לי שבוע של נתונים גולמיים."),
            ("outbound", "אני לא יכול להעביר את הנתונים עצמם."),
            ("outbound", "אני יכול לשלוח hash של השבוע כדי שתוכל לאמת אותם."),
            ("inbound", "בסדר, שלח את ה-hash."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The original obligation was replaced with a different agreed deliverable.",
    ),

    EvalCase(
        id="socf-52",
        description="Full document request replaced by screenshot request",
        messages=[
            ("inbound", "אתה יכול לשלוח לי את כל הדוח?"),
            ("outbound", "בטח."),
            ("inbound", "בעצם הוא ענק. פשוט תצלם לי את העמוד עם טבלת הזמנים."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="Only the latest version of the requested deliverable should remain open.",
    ),


    # ======================================================================
    # FAMILY 7 — MULTIPLE SIMULTANEOUS OBLIGATIONS
    # ======================================================================

    EvalCase(
        id="socf-53",
        description="User fulfills image request but not timestamp request",
        messages=[
            ("inbound", "תשלח לי את הפריים."),
            ("inbound", "וגם תרשום לי באיזו שעה בדיוק זה היה."),
            ("outbound", "<image>צילום מהמצלמה</image>"),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The frame was supplied, but the timestamp is still missing.",
    ),

    EvalCase(
        id="socf-54",
        description="Several promised items with only one completed",
        messages=[
            ("outbound", "בשבת אביא שני ספלים, כיסא מתקפל ועוד זרעים."),
            ("inbound", "מושלם."),
            ("outbound", "הכיסא כבר באוטו."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="Other explicitly promised items remain outstanding.",
    ),


    # ======================================================================
    # FAMILY 8 — BURIED OBLIGATION / TOPIC DRIFT
    # ======================================================================

    EvalCase(
        id="socf-55",
        description="Promised links survive emotional topic drift",
        messages=[
            ("outbound", "יש לי שני סרטים שיתאימו לך. אשלח לינקים."),
            ("inbound", "תודה."),
            ("inbound", "אגב, אחותי ראתה את הסרט הקודם."),
            ("outbound", "ומה היא חשבה?"),
            ("inbound", "היא בכתה ואז צחקה על עצמה."),
            ("outbound", "חחח נשמע שהיא אהבה אותו."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The earlier promise to send links remains unresolved.",
    ),

    EvalCase(
        id="socf-56",
        description="Promised contact remains open through unrelated updates",
        messages=[
            ("outbound", "אני אשלח לך את הקשר המשפטי מהמקרה בברקלי."),
            ("inbound", "תודה, זה יכול להציל אותנו."),
            ("inbound", "בינתיים מצאתי את מסמכי המים הישנים."),
            ("outbound", "זה כבר משהו."),
            ("inbound", "וגם היתר צבעונים משנת 2008 😂"),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="Topic drift does not resolve the promised contact.",
    ),


    # ======================================================================
    # FAMILY 9 — SOFT / IMPLICIT REQUESTS
    # ======================================================================

    EvalCase(
        id="socf-57",
        description="Informal phrase 'need eyes on this' is a real request",
        messages=[
            ("inbound", "ההיתר נדחה שוב."),
            ("inbound", "צריך עוד זוג עיניים על הרצפה הזאת מחר."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="'Need eyes on this' implicitly asks the user to inspect/review something.",
    ),

    EvalCase(
        id="socf-58",
        description="'Just say the date' implicitly requests scheduling input",
        messages=[
            ("inbound", "אני אשמור לך מקום בדיינר."),
            ("inbound", "רק תגיד תאריך ונעשה את זה."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="No question mark, but the other person's next action depends on the user's response.",
    ),


    # ======================================================================
    # FAMILY 10 — OFFER IS NOT AN OBLIGATION
    # ======================================================================

    EvalCase(
        id="socf-59",
        description="Offer to send movie links is optional",
        messages=[
            ("inbound", "יש לי עוד כמה סרטים כאלה."),
            ("inbound", "תגיד אם תרצה את הלינקים."),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="The user is not required to respond to an optional offer.",
    ),

    EvalCase(
        id="socf-60",
        description="Offer to provide extra technical detail is optional",
        messages=[
            ("inbound", "נראה לי שזה קשור לזווית האור."),
            ("inbound", "אם תרצה להיכנס לזה ממש, אני יכול לשלוח לך את הזווית המדויקת."),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="Optional additional detail should not generate WFM.",
    ),


    # ======================================================================
    # FAMILY 11 — OFFER-LIKE LANGUAGE THAT DOES REQUIRE AN ANSWER
    # ======================================================================

    EvalCase(
        id="socf-61",
        description="User is expected to report results of agreed investigation",
        messages=[
            ("outbound", "אני אשאל בשקט בפגישת הוועד אם באמת מתכננים שביתה."),
            ("inbound", "מעולה. תעדכן אותי מה אתה שומע."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The other person is explicitly waiting for the result of an agreed action.",
    ),

    EvalCase(
        id="socf-62",
        description="Prototype feedback blocks next step",
        messages=[
            ("inbound", "<image>אב-טיפוס חדש של הכיסא</image>"),
            ("inbound", "תגיד לי אם זה עובד לך לפני שאני חותך את הפלדה."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="Feedback is required before the sender can continue.",
    ),


    # ======================================================================
    # FAMILY 12 — SUGGESTION IS NOT A REQUEST
    # ======================================================================

    EvalCase(
        id="socf-63",
        description="Safety advice about tomorrow's run is not a task for sender",
        messages=[
            ("inbound", "18 מייל בחום הזה?"),
            ("inbound", "עדיף שתתחיל בחמש בבוקר ותיקח אלקטרוליטים."),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="Advice directed at the user does not mean the sender is waiting for completion.",
    ),

    EvalCase(
        id="socf-64",
        description="Friendly suggestion not to repeat risky stunt",
        messages=[
            ("inbound", "אולי בפעם הבאה פשוט תוותר על הסלטה 😅"),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="A suggestion about future behavior is not itself an interpersonal obligation.",
    ),


    # ======================================================================
    # FAMILY 13 — CONDITIONAL COMMITMENT
    # ======================================================================

    EvalCase(
        id="socf-65",
        description="Conditional request is inactive before triggering event",
        messages=[
            ("outbound", "עוד לא החלפתי את הנורה במסדרון."),
            ("inbound", "כשתחליף אותה, תשלח לי תמונה. רוצה לראות שהאור חזר."),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="Sending a photo is conditional on an event that has not happened yet.",
    ),

    EvalCase(
        id="socf-66",
        description="Conditional photo request activates once condition occurs",
        messages=[
            ("outbound", "עוד לא החלפתי את הנורה במסדרון."),
            ("inbound", "כשתחליף אותה, תשלח לי תמונה."),
            ("outbound", "סוף סוף החלפתי אותה עכשיו."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The condition has occurred, so the promised/requested photo is now due.",
    ),


    # ======================================================================
    # FAMILY 14 — SCHEDULING OWNERSHIP SWITCHES
    # Three cases, same distribution as previous batch.
    # ======================================================================

    EvalCase(
        id="socf-67",
        description="Informal meeting availability request",
        messages=[
            ("inbound", "אתה פנוי מחר? צריך עיניים על הרצפה הזאת."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The sender needs an availability decision.",
    ),

    EvalCase(
        id="socf-68",
        description="User accepts but sender takes ownership of logistics",
        messages=[
            ("inbound", "אתה פנוי מחר? צריך עיניים על הרצפה הזאת."),
            ("outbound", "כן, אני יכול."),
            ("inbound", "מעולה. אני אשלח לך שעה וכתובת בבוקר."),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="The next step belongs to the sender.",
    ),

    EvalCase(
        id="socf-69",
        description="Sender proposes concrete time and ball returns to user",
        messages=[
            ("inbound", "אתה פנוי מחר? צריך עיניים על הרצפה הזאת."),
            ("outbound", "כן."),
            ("inbound", "10:00 בסדנה מתאים?"),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="A concrete scheduling proposal now requires user confirmation.",
    ),


    # ======================================================================
    # FAMILY 15 — FOLLOW-UP / CHASING
    # ======================================================================

    EvalCase(
        id="socf-70",
        description="Promise to send links is WFM without any reminder",
        messages=[
            ("outbound", "אני אשלח לך הערב את שני הלינקים."),
            ("inbound", "תודה ❤️"),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="No follow-up is required for a user-created obligation to remain open.",
    ),

    EvalCase(
        id="socf-71",
        description="Delayed reminder refers to same existing obligation",
        messages=[
            ("outbound", "אני אשלח לך הערב את שני הלינקים."),
            ("inbound", "תודה ❤️"),
            ("inbound", "<delay days=\"1\"/>"),
            ("inbound", "מצאת במקרה את הלינקים שדיברנו עליהם?"),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The reminder reinforces the same unresolved obligation.",
    ),


    # ======================================================================
    # FAMILY 16 — REPLY VS ACTION
    # Strong contrast pair from SOC-style messages.
    # ======================================================================

    EvalCase(
        id="socf-72",
        description="Yes/no visibility question is completely answered",
        messages=[
            ("inbound", "אתה רואה את מסך הטעינה של המשחק?"),
            ("outbound", "כן, רואה אותו."),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="The requested reply was provided; there is no action remaining.",
    ),

    EvalCase(
        id="socf-73",
        description="Yes accepts image-send request but does not perform it",
        messages=[
            ("inbound", "אתה יכול לשלוח לי את הפריים מהמצלמה?"),
            ("outbound", "כן."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="Agreement to send something is not equivalent to sending it.",
    ),


    # ======================================================================
    # FAMILY 17 — OLD OBLIGATION CLOSES, NEW ONE OPENS
    # ======================================================================

    EvalCase(
        id="socf-74",
        description="Image request completed but new zoom request opens",
        messages=[
            ("inbound", "תשלח לי את הפריים מהמצלמה."),
            ("outbound", "<image>צילום ממצלמת האבטחה</image>"),
            ("inbound", "קיבלתי. אתה יכול לשלוח גם crop של הפינה עם השעה?"),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The original request closed, but a new related request immediately opened.",
    ),

    EvalCase(
        id="socf-75",
        description="Investigation completed then new artifact requested",
        messages=[
            ("outbound", "בדקתי בפגישת הוועד. אין שביתה כרגע."),
            ("inbound", "איזה הקלה."),
            ("inbound", "תוכל לשלוח לי גם את הפרטים של הנציגה שדיברת איתה?"),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The investigation obligation is complete; a new contact-sharing obligation is open.",
    ),


    # ======================================================================
    # FAMILY 18 — IMPLICIT COMPLETION
    # ======================================================================

    EvalCase(
        id="socf-76",
        description="Sending requested media directly fulfills request",
        messages=[
            ("inbound", "אתה יכול לשלוח לי את הפריים שראית?"),
            ("outbound", "<image>פריים ממצלמת רחוב בשעה 03:32</image>"),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="The outbound media itself is the requested deliverable.",
    ),

    EvalCase(
        id="socf-77",
        description="Coordinates directly answer information request",
        messages=[
            ("inbound", "תשלח לי את הקואורדינטות המדויקות."),
            ("outbound", "49.2827, -123.1207"),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="The requested information was supplied without an explicit completion phrase.",
    ),


    # ======================================================================
    # FAMILY 19 — CORRECTIONS / RETRACTIONS
    # Three cases, same distribution as previous batch.
    # ======================================================================

    EvalCase(
        id="socf-78",
        description="Sender immediately retracts accidental work request",
        messages=[
            ("inbound", "רק מוודאת שראית את ההודעה הקודמת."),
            ("inbound", "צירפתי את הטופס, הוא צריך להיות מוכן למחר."),
            ("inbound", "רגע לא — סליחה, זה היה לעבודה. תתעלם 😅"),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="The apparent obligation is explicitly invalidated by the sender.",
    ),

    EvalCase(
        id="socf-79",
        description="User corrects timing but keeps same commitment",
        messages=[
            ("outbound", "אני אשלח לך את הלוגים הערב."),
            ("outbound", "תיקון — אני במשמרת עד מאוחר. אשלח מחר בבוקר."),
            ("inbound", "בסדר."),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="The commitment remains open; only its expected timing changed.",
    ),

    EvalCase(
        id="socf-80",
        description="User retracts promised send because recipient already has access",
        messages=[
            ("outbound", "אני אשלח לך את הדוח כשאגיע הביתה."),
            ("inbound", "רגע, יש לי בעצם גישה לתיקייה המשותפת."),
            ("outbound", "נכון, הוא כבר שם. קח משם את הגרסה האחרונה."),
            ("inbound", "מצאתי, תודה."),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="The promised send is no longer necessary and the recipient confirms they obtained the artifact.",
    ),

    # ======================================================================
    # FAMILY: OWNER INVERSION (socf-81..87)
    # Cases where v1 incorrectly labels as WFM when the next actionable step
    # belongs to the OTHER person. Two patterns are covered:
    #   1. QUESTION (socf-81, socf-83): user asks a clarifying question → other
    #      person must answer. v1 inverts the label to WFM.
    #   2. CONTRAST (socf-82, 84, 85, 86, 87): offer-blocked or explicit-request
    #      patterns where WFM is correct. These prevent the fix from collapsing
    #      into a naive "last message outbound → OTHER" rule.
    # ======================================================================

    EvalCase(
        id="socf-81",
        description="User asks 'מה להביא?' after being asked to bring something — other must specify",
        messages=[
            ("inbound", "תביא משהו"),
            ("outbound", "מה להביא?"),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="v1 says WFM 98-99%: 'the other person asked the user to bring something, and the user asked what to bring.' But the user asked a question — the other person must answer what to bring before the user can act (BLOCKED). 3/3 fails on v1.",
    ),
    EvalCase(
        id="socf-82",
        description="User offers to send something, other says 'כן' — user should follow up",
        messages=[
            ("outbound", "אני יכול לשלוח לך משהו, רוצה?"),
            ("inbound", "כן"),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="Offer-blocked pattern: the user offered, the other person accepted — the user should follow up and ask what to send. WFM is correct. v1 correctly returns WFM (3/3). Contrast case for the QUESTION-pattern inversions.",
    ),
    EvalCase(
        id="socf-83",
        description="User asks 'הבאת קופסא קטנה יותר?' after receiving a task list",
        messages=[
            ("outbound", "רוצה נכנס כולנו לבריכה או ים?"),
            ("inbound", "יושבת עם מיקה ללמוד\nהיא בשוונג"),
            ("inbound", "שי עושה לגו!"),
            ("inbound", "?"),
            ("inbound", "משימות למתוקי בעזרת השם:\nלזרוק קרטונים למחזור\nלשייף את התקרה \nלסדר את הקופסא עם הכלים במגירה\n❣️"),
            ("outbound", "הבאת קופסא קטנה יותר?"),
        ],
        expected=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        notes="Real snapshot v11: v1 reason said 'They asked a direct question and it has not been answered yet' but the USER asked the question — the other person must answer. Label was inverted to WFM. 3/3 fails on v1.",
    ),
    EvalCase(
        id="socf-84",
        description="User offers to make food, other says 'כן' — user should follow up",
        messages=[
            ("outbound", "אני מכין משהו לאכול, אתה רוצה?"),
            ("inbound", "כן"),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="Offer-blocked pattern: the user offered, the other person accepted — the user should follow up and ask what to make. WFM is correct. v1 correctly returns WFM (3/3). Contrast case for the QUESTION-pattern inversions.",
    ),
    EvalCase(
        id="socf-85",
        description="User offers to bring something; other says 'כן' — ambiguous (UNC)",
        messages=[
            ("outbound", "050-513-1216"),
            ("outbound", "😘"),
            ("inbound", "היי"),
            ("outbound", "מה קורה מתוקה,  סיימת?"),
            ("outbound", "להביא לך משהו בדרך?"),
            ("inbound", "כן"),
        ],
        expected=WaitingForMeDecision.UNCERTAIN,
        notes="Real snapshot v88-v92: the 'כן' reply is genuinely ambiguous — it could answer either of the user's two questions ('סיימת?' or 'להביא לך משהו?'). v3 correctly returns UNCERTAIN (3/3). v1 incorrectly returned WFM or NWM. UNCERTAIN is the defensible label.",
    ),
    EvalCase(
        id="socf-86",
        description="Other person explicitly requests the user bring a blessing to the meeting",
        messages=[
            ("outbound", "050-513-1216"),
            ("outbound", "😘"),
            ("inbound", "תכף נסיימת עם המייל, אני מתייחסת להערות של עינת."),
            ("inbound", "היי"),
            ("outbound", "מה קורה מתוקה,  סיימת?"),
            ("outbound", "להביא לך משהו בדרך?"),
            ("inbound", "כן"),
            ("inbound", "להביא היום ברכה לשי לאסיפה"),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="Real snapshot v95-v100: contrast case. The other person explicitly requested the user bring a blessing — the user owes the action. v1 correctly returned WFM here. 0/3 fails on v1.",
    ),
    EvalCase(
        id="socf-87",
        description="Other person asks if user is awake and says the girls need a ride",
        messages=[
            ("inbound", "/16 מביא בוקר / אחהצ\n17 כל היום \n18 צהרים"),
            ("outbound", "מזה?"),
            ("outbound", "כל הכבוד מתוקה. וואו. שאפו"),
            ("inbound", "בפטריה?"),
            ("outbound", "יאללה"),
            ("inbound", "מתוקי ער ?"),
            ("inbound", "הילולי לא יכולה לקחת את הבנות"),
            ("inbound", "התחנה 24 פז יסמין\nממש קרןב לביהס"),
        ],
        expected=WaitingForMeDecision.WAITING_FOR_ME,
        notes="Real snapshot v41: contrast case. The other person asked 'מתוקי ער?' and said the girls need a ride — the user must respond. v1 correctly returned WFM here. 0/3 fails on v1.",
    ),
]


# ===========================================================================
# METADATA: family, source_group, split for all 80 SOC cases.
#
# source_group identifies the underlying conversation/scenario. Cases that
# are derived from the same conversation share a source_group and MUST go
# in the same split to prevent dev/test leakage.
#
# Split sizes: dev=40, test=40.
# - DEV: cases we freely inspect and use to improve the prompt.
# - TEST: cases we score but do not use to decide prompt changes.
# Sanity cases are NOT part of this split — they are always run separately
# as a regression baseline.
# ===========================================================================

SOC_CASE_METADATA: dict[str, dict[str, str]] = {
    # --- Batch 1 (socf-01..40) ---
    "socf-01": {"family": "user_created_commitment", "source_group": "presentation_fix", "split": "dev"},
    "socf-02": {"family": "user_created_commitment", "source_group": "numbers_check", "split": "dev"},
    "socf-03": {"family": "acknowledgement_not_fulfillment", "source_group": "contract_review", "split": "dev"},
    "socf-04": {"family": "acknowledgement_not_fulfillment", "source_group": "signature_request", "split": "dev"},
    "socf-05": {"family": "ball_handed_to_them", "source_group": "price_quote_dimensions", "split": "test"},
    "socf-06": {"family": "ball_handed_to_them", "source_group": "meeting_schedule", "split": "test"},
    "socf-07": {"family": "dependency_reactivation", "source_group": "price_quote_dimensions", "split": "test"},
    "socf-08": {"family": "dependency_reactivation", "source_group": "report_danny_numbers", "split": "test"},
    "socf-09": {"family": "cancellation_supersession", "source_group": "draft_review_maya", "split": "dev"},
    "socf-10": {"family": "cancellation_supersession", "source_group": "file_send_found", "split": "dev"},
    "socf-11": {"family": "obligation_replaced", "source_group": "excel_to_total", "split": "dev"},
    "socf-12": {"family": "obligation_replaced", "source_group": "explanation_to_call", "split": "dev"},
    "socf-13": {"family": "multiple_obligations", "source_group": "presentation_day_price", "split": "dev"},
    "socf-14": {"family": "multiple_obligations", "source_group": "presentation_day_price", "split": "dev"},
    "socf-15": {"family": "buried_obligation", "source_group": "address_game_drift", "split": "dev"},
    "socf-16": {"family": "buried_obligation", "source_group": "link_trip_drift", "split": "dev"},
    "socf-17": {"family": "soft_implicit_request", "source_group": "feedback_before_meeting", "split": "dev"},
    "socf-18": {"family": "soft_implicit_request", "source_group": "missing_approval", "split": "dev"},
    "socf-19": {"family": "offer_not_obligation", "source_group": "optional_full_version", "split": "test"},
    "socf-20": {"family": "offer_not_obligation", "source_group": "optional_details_offer", "split": "test"},
    "socf-21": {"family": "offer_requires_answer", "source_group": "option_selection_order", "split": "test"},
    "socf-22": {"family": "offer_requires_answer", "source_group": "color_choice", "split": "test"},
    "socf-23": {"family": "suggestion_not_request", "source_group": "resume_advice", "split": "dev"},
    "socf-24": {"family": "suggestion_not_request", "source_group": "version_check_suggestion", "split": "dev"},
    "socf-25": {"family": "conditional_commitment", "source_group": "invoice_client_approval", "split": "test"},
    "socf-26": {"family": "conditional_commitment", "source_group": "invoice_client_approval", "split": "test"},
    "socf-27": {"family": "scheduling_ownership", "source_group": "day_choice", "split": "test"},
    "socf-28": {"family": "scheduling_ownership", "source_group": "day_choice", "split": "test"},
    "socf-29": {"family": "scheduling_ownership", "source_group": "day_choice", "split": "test"},
    "socf-30": {"family": "follow_up_chasing", "source_group": "form_promise", "split": "dev"},
    "socf-31": {"family": "follow_up_chasing", "source_group": "form_promise", "split": "dev"},
    "socf-32": {"family": "reply_vs_action", "source_group": "fairness_question", "split": "test"},
    "socf-33": {"family": "reply_vs_action", "source_group": "excel_price_update", "split": "test"},
    "socf-34": {"family": "old_closes_new_opens", "source_group": "file_received_review", "split": "dev"},
    "socf-35": {"family": "old_closes_new_opens", "source_group": "file_received_review", "split": "dev"},
    "socf-36": {"family": "implicit_completion", "source_group": "address_request", "split": "test"},
    "socf-37": {"family": "implicit_completion", "source_group": "order_number", "split": "test"},
    "socf-38": {"family": "corrections_retractions", "source_group": "send_reschedule", "split": "dev"},
    "socf-39": {"family": "corrections_retractions", "source_group": "danny_call_transfer", "split": "dev"},
    "socf-40": {"family": "corrections_retractions", "source_group": "accidental_invoice", "split": "dev"},
    # --- Batch 2 (socf-41..80) ---
    "socf-41": {"family": "user_created_commitment", "source_group": "movie_links_promise", "split": "test"},
    "socf-42": {"family": "user_created_commitment", "source_group": "legal_contact_promise", "split": "dev"},
    "socf-43": {"family": "acknowledgement_not_fulfillment", "source_group": "permit_inspection", "split": "test"},
    "socf-44": {"family": "acknowledgement_not_fulfillment", "source_group": "camera_frame", "split": "test"},
    "socf-45": {"family": "ball_handed_to_them", "source_group": "camera_footage_timestamp", "split": "test"},
    "socf-46": {"family": "ball_handed_to_them", "source_group": "face_to_face_meeting", "split": "dev"},
    "socf-47": {"family": "dependency_reactivation", "source_group": "camera_footage_timestamp", "split": "test"},
    "socf-48": {"family": "dependency_reactivation", "source_group": "permit_review_section", "split": "dev"},
    "socf-49": {"family": "cancellation_supersession", "source_group": "wrong_version_cancel", "split": "dev"},
    "socf-50": {"family": "cancellation_supersession", "source_group": "event_cancelled_weather", "split": "dev"},
    "socf-51": {"family": "obligation_replaced", "source_group": "raw_data_hash", "split": "dev"},
    "socf-52": {"family": "obligation_replaced", "source_group": "report_screenshot", "split": "dev"},
    "socf-53": {"family": "multiple_obligations", "source_group": "camera_frame_timestamp", "split": "test"},
    "socf-54": {"family": "multiple_obligations", "source_group": "saturday_supplies", "split": "dev"},
    "socf-55": {"family": "buried_obligation", "source_group": "movie_links_drift", "split": "test"},
    "socf-56": {"family": "buried_obligation", "source_group": "legal_contact_drift", "split": "dev"},
    "socf-57": {"family": "soft_implicit_request", "source_group": "permit_eyes", "split": "test"},
    "socf-58": {"family": "soft_implicit_request", "source_group": "diner_date", "split": "dev"},
    "socf-59": {"family": "offer_not_obligation", "source_group": "movie_links_offer", "split": "test"},
    "socf-60": {"family": "offer_not_obligation", "source_group": "light_angle_offer", "split": "dev"},
    "socf-61": {"family": "offer_requires_answer", "source_group": "committee_investigation", "split": "dev"},
    "socf-62": {"family": "offer_requires_answer", "source_group": "chair_prototype", "split": "dev"},
    "socf-63": {"family": "suggestion_not_request", "source_group": "run_safety_advice", "split": "dev"},
    "socf-64": {"family": "suggestion_not_request", "source_group": "salto_suggestion", "split": "dev"},
    "socf-65": {"family": "conditional_commitment", "source_group": "bulb_photo", "split": "test"},
    "socf-66": {"family": "conditional_commitment", "source_group": "bulb_photo", "split": "test"},
    "socf-67": {"family": "scheduling_ownership", "source_group": "floor_availability", "split": "test"},
    "socf-68": {"family": "scheduling_ownership", "source_group": "floor_availability", "split": "test"},
    "socf-69": {"family": "scheduling_ownership", "source_group": "floor_availability", "split": "test"},
    "socf-70": {"family": "follow_up_chasing", "source_group": "links_promise_reminder", "split": "test"},
    "socf-71": {"family": "follow_up_chasing", "source_group": "links_promise_reminder", "split": "test"},
    "socf-72": {"family": "reply_vs_action", "source_group": "loading_screen", "split": "test"},
    "socf-73": {"family": "reply_vs_action", "source_group": "camera_frame_send", "split": "test"},
    "socf-74": {"family": "old_closes_new_opens", "source_group": "camera_crop", "split": "test"},
    "socf-75": {"family": "old_closes_new_opens", "source_group": "committee_contact", "split": "dev"},
    "socf-76": {"family": "implicit_completion", "source_group": "camera_frame_sent", "split": "test"},
    "socf-77": {"family": "implicit_completion", "source_group": "coordinates", "split": "test"},
    "socf-78": {"family": "corrections_retractions", "source_group": "accidental_form", "split": "test"},
    "socf-79": {"family": "corrections_retractions", "source_group": "logs_reschedule", "split": "test"},
    "socf-80": {"family": "corrections_retractions", "source_group": "report_shared_folder", "split": "dev"},
    # --- Batch 3 (socf-81..87) — owner_inversion family, all DEV ---
    "socf-81": {"family": "owner_inversion", "source_group": "what_to_bring_question", "split": "dev"},
    "socf-82": {"family": "owner_inversion", "source_group": "send_something_offer", "split": "dev"},
    "socf-83": {"family": "owner_inversion", "source_group": "smaller_box_question", "split": "dev"},
    "socf-84": {"family": "owner_inversion", "source_group": "make_food_offer", "split": "dev"},
    "socf-85": {"family": "owner_inversion", "source_group": "bring_something_blocked", "split": "dev"},
    "socf-86": {"family": "owner_inversion", "source_group": "shai_blessing_meeting", "split": "dev"},
    "socf-87": {"family": "owner_inversion", "source_group": "girls_ride_request", "split": "dev"},
}


def _tag_cases(cases: list[EvalCase]) -> list[EvalCase]:
    """Return copies of ``cases`` with family/source_group/split populated
    from :data:`SOC_CASE_METADATA`.
    """
    tagged: list[EvalCase] = []
    for case in cases:
        meta = SOC_CASE_METADATA.get(case.id)
        if meta is None:
            raise ValueError(f"No metadata for case {case.id!r}")
        tagged.append(replace(case, **meta))
    return tagged


def validate_no_leakage(cases: list[EvalCase] | None = None) -> None:
    """Assert that no ``source_group`` appears in more than one split.

    Args:
        cases: The cases to validate. Defaults to all 80 SOC cases.
    """
    all_cases = cases or SOC_ALL_CASES
    groups_by_split: dict[str, set[str]] = {}
    for case in all_cases:
        if not case.split or not case.source_group:
            continue
        groups_by_split.setdefault(case.split, set()).add(case.source_group)

    splits = list(groups_by_split.keys())
    for i, s1 in enumerate(splits):
        for s2 in splits[i + 1:]:
            overlap = groups_by_split[s1] & groups_by_split[s2]
            assert not overlap, (
                f"Leakage: source_groups {overlap} appear in both "
                f"'{s1}' and '{s2}'"
            )


# All 80 cases, tagged with metadata.
SOC_ALL_CASES: list[EvalCase] = _tag_cases(SOC_FAMILY_CASES + SOC_FAMILY_CASES_2)

# Split exports.
SOC_DEV_CASES: list[EvalCase] = [c for c in SOC_ALL_CASES if c.split == "dev"]
SOC_TEST_CASES: list[EvalCase] = [c for c in SOC_ALL_CASES if c.split == "test"]

# Validate at import time — fail fast if leakage is introduced.
validate_no_leakage()