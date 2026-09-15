"""Realistic evaluation cases derived from the SOC-2508 dataset.

40 cases grounded in real conversation dynamics from the Synthetic Online
Conversations dataset (``marcodsn/SOC-2508``, CC BY 4.0):
https://huggingface.co/datasets/marcodsn/SOC-2508

Unlike the hand-crafted :mod:`waiting_for_me_soc_cases` (short, clean,
single-topic), these cases mirror how SOC-2508 conversations actually
look — and how real WhatsApp chats look:

* **Long, noisy windows** (8–18 messages) with multi-message bursts.
* **Buried obligations**: a promise or request made mid-conversation,
  followed by heavy topic drift.
* **Base-rate negatives**: long, mundane chats with *no* obligation at
  all — the most common state of real chats, underrepresented in the
  original suite (which is 62% WFM vs reality's heavy NWM skew).
* **UNCERTAIN cases**: media-only messages, missing context, ambiguous
  addressee — the third label, previously never tested.
* **Time dynamics**: ``<delay .../>`` tags for reminders, staleness, and
  post-delay completion.
* **Rhetorical/joke commitments**: "we should totally patent this" is
  not an obligation.

Case ID prefix: ``socr-`` (SOC realistic). Splits: dev (inspection and
iteration) / test (unbiased milestone signal). Cases sharing a
``source_group`` never span splits (verified by
:func:`validate_no_leakage` from the SOC cases module).
"""

from __future__ import annotations

from echo_v2.domain.waiting_for_me import WaitingForMeDecision

from .waiting_for_me_cases import EvalCase

__all__ = [
    "SOC2508_ALL_CASES",
    "SOC2508_DEV_CASES",
    "SOC2508_TEST_CASES",
]

_WFM = WaitingForMeDecision.WAITING_FOR_ME
_NWM = WaitingForMeDecision.NOT_WAITING_FOR_ME
_UNC = WaitingForMeDecision.UNCERTAIN


SOC2508_ALL_CASES: list[EvalCase] = [

    # ======================================================================
    # WORLD: journal_pages (dev) — from SOC-2508 idx 3: a promise to send
    # a journal page, buried inside a long emotional exchange.
    # ======================================================================

    EvalCase(
        id="socr-01",
        description="Promise buried in long emotional chat, heavy drift after",
        messages=[
            ("inbound", "קראתי הלילה עוד קטע מהיומן של אבא שלך ששלחת. זה ממש נגע בי."),
            ("inbound", "הקטע על הגשם. וואו."),
            ("outbound", "אני שמח שזה עשה לך משהו. לי לקח שנה לפתוח את היומן בכלל."),
            ("outbound", "יש עמוד אחד שאני חייב להראות לך, עמוד 48. על סליחה. אשלח לך צילום שלו."),
            ("inbound", "מחכה לזה 🤍"),
            ("inbound", "איך היה הלילה עם הקטנה? עוד קדחת?"),
            ("outbound", "ירד לה החום סוף סוף. היינו ערים עד 3 עם מרק וסרטים מצוירים 😅"),
            ("inbound", "גיבור. אצלנו התאומים החליטו ש-5 בבוקר זה זמן טוב לתופים."),
            ("outbound", "חחח אין עליהם. טוב, אני קורס. לילה טוב"),
            ("inbound", "לילה טוב 🌙"),
        ],
        expected=_WFM,
        notes="The promised page-48 photo was never sent; the obligation survives the drift into kids talk.",
        family="buried_obligation_long",
        source_group="journal_pages",
        split="dev",
    ),

    EvalCase(
        id="socr-02",
        description="Buried promise fulfilled later by media, drift continues after",
        messages=[
            ("inbound", "קראתי הלילה עוד קטע מהיומן. הקטע על הגשם. וואו."),
            ("outbound", "יש עמוד שאני חייב להראות לך, עמוד 48. אשלח לך צילום שלו."),
            ("inbound", "מחכה לזה 🤍"),
            ("inbound", "איך היה הלילה עם הקטנה?"),
            ("outbound", "ירד לה החום סוף סוף. היינו ערים עד 3 😅"),
            ("outbound", "<image>צילום של עמוד 48 מהיומן, כתב יד צפוף</image>"),
            ("outbound", "הנה. תקראי כשיש לך רגע שקט."),
            ("inbound", "וואו. קורא את זה עכשיו לאט."),
            ("inbound", "תודה שאתה חולק את זה איתי."),
            ("outbound", "בשמחה. לילה טוב 🌙"),
        ],
        expected=_NWM,
        notes="The promised photo WAS sent (media message). Nothing remains open despite no explicit 'done' phrase.",
        family="implicit_completion",
        source_group="journal_pages",
        split="dev",
    ),

    # ======================================================================
    # WORLD: wrong_chat_form (test) — from SOC-2508 idx 0: "I attached the
    # form. It's due tomorrow. Wait no—that was for my classmate lol"
    # ======================================================================

    EvalCase(
        id="socr-03",
        description="Misdirected request is retracted — wrong chat",
        messages=[
            ("inbound", "היי! מזמן לא דיברנו 😊"),
            ("inbound", "מצרפת את הטופס, צריך לחתום ולהחזיר עד מחר."),
            ("inbound", "<image>טופס הרשמה סרוק</image>"),
            ("inbound", "רגע, זה לא היה אמור ללכת אלייך 🤦‍♀️ זה לקולגה שלי. סורי!"),
            ("outbound", "חחח קורה. מה שלומך בכלל?"),
            ("inbound", "הכל טוב! עמוסה בטירוף עם הקורס הזה."),
        ],
        expected=_NWM,
        notes="The signature request was sent to the wrong person and explicitly retracted. Nothing is owed.",
        family="corrections_retractions",
        source_group="wrong_chat_form",
        split="test",
    ),

    EvalCase(
        id="socr-04",
        description="Retraction reversed — the request does apply after all",
        messages=[
            ("inbound", "מצרפת את הטופס, צריך לחתום ולהחזיר עד מחר."),
            ("inbound", "<image>טופס הרשמה סרוק</image>"),
            ("inbound", "רגע, זה היה אמור ללכת למישהו אחר..."),
            ("inbound", "בעצם לא, טעות שלי — זה כן בשבילך 😅 את ההורה המלווה השני, נכון? צריכה את החתימה שלך עד מחר."),
            ("outbound", "אה כן, זאת אני חחח"),
        ],
        expected=_WFM,
        notes="Adversarial double-retraction: the request was un-retracted and still requires the user's signature.",
        family="corrections_retractions",
        source_group="wrong_chat_form",
        split="test",
    ),

    # ======================================================================
    # WORLD: hvac_rant (dev) — from SOC-2508 idx 8: a long vent about a
    # broken AC. Empathy and rhetorical support are not obligations.
    # ======================================================================

    EvalCase(
        id="socr-05",
        description="Long vent with rhetorical support — no obligation created",
        messages=[
            ("inbound", "אני לא מאמינה. שוב המזגן במרכז הקהילתי מקולקל. גל חום. ילדים מזיעים בסדנה."),
            ("inbound", "<image>פלייר מקומט עם הכיתוב 'אתם חשובים לנו!'</image>"),
            ("inbound", "הם מעדיפים לממן ציור קיר במקום לתקן מזגן. ציור קיר!!"),
            ("outbound", "לא יאומן 😤 זה פשוט ביזיון."),
            ("outbound", "הייתי חותם על עצומה כזאת בשתי ידיים."),
            ("inbound", "נכון?? אולי באמת אפתח אחת."),
            ("inbound", "בינתיים הגשתי דוח מפגע רשמי. באותיות גדולות. עם תמונות."),
            ("outbound", "מלכה 👑 תעדכני מה קורה עם זה."),
            ("inbound", "ברור. טוב, הולכת להזיז את הסדנה לחצר. מאחלת לעצמי בהצלחה 🥵"),
            ("outbound", "בהצלחה!! 🍀"),
        ],
        expected=_NWM,
        notes="'I'd sign such a petition' and 'update me' are rhetorical support in venting, not actionable obligations on the user.",
        family="banter_no_obligation",
        source_group="hvac_rant",
        split="dev",
    ),

    EvalCase(
        id="socr-06",
        description="Same vent, but a concrete deliverable emerges at the end",
        messages=[
            ("inbound", "שוב המזגן במרכז הקהילתי מקולקל. גל חום. ילדים מזיעים בסדנה."),
            ("inbound", "הם מעדיפים לממן ציור קיר במקום לתקן מזגן!!"),
            ("outbound", "ביזיון 😤"),
            ("inbound", "רגע, אתה לא מכיר מישהו במחלקת תחזוקה בעירייה?"),
            ("outbound", "כן! יוסי, עבדנו יחד פעם. אשלח לך את המספר שלו."),
            ("inbound", "מושלם, את מציל אותי."),
            ("inbound", "בינתיים מזיזה את הסדנה לחצר 🥵"),
            ("outbound", "בהצלחה!"),
        ],
        expected=_WFM,
        notes="Inside the same vent, the user promised to send Yossi's number and hasn't yet.",
        family="user_created_commitment",
        source_group="hvac_rant",
        split="dev",
    ),

    # ======================================================================
    # WORLD: monopoly_card (test) — from SOC-2508 idx 22: long playful
    # banter; a joke "request" is not a real obligation.
    # ======================================================================

    EvalCase(
        id="socr-07",
        description="Pure banter with a joke request — nothing actionable",
        messages=[
            ("inbound", "אתה לא תאמין. נוסע אתמול נתן לי טיפ בכסף של מונופול 😂"),
            ("inbound", "<image>שטר צבעוני של מונופול על מגש טיפים</image>"),
            ("inbound", "אפילו לא מהסוג הכיף, קלף של 'צא מהכלא חינם'."),
            ("outbound", "אחחחח אני מת 😂😂"),
            ("outbound", "שמור לי את הקלף לחתונה. ליתר ביטחון חחח"),
            ("inbound", "חחחח שמרתי, הדבקתי אותו מתחת למגירת הקופה כקמע."),
            ("inbound", "העמיתה שלי חושבת שזאת בדיחה על העלמת מס. (זה לא. נראה לי.)"),
            ("outbound", "חחחחח מגניב. טוב, משמרת כפולה מחר. לילה!"),
            ("inbound", "לילה טוב 😄"),
        ],
        expected=_NWM,
        notes="'Save me the card for my wedding' is a joke, and even so it was already 'fulfilled'. Nothing is pending on the user.",
        family="banter_no_obligation",
        source_group="monopoly_card",
        split="test",
    ),

    # ======================================================================
    # WORLD: van_tools (dev) — from SOC-2508 idx 33: an advice exchange.
    # Receiving advice completes it; a concrete ask changes that.
    # ======================================================================

    EvalCase(
        id="socr-08",
        description="Advice requested and fully delivered — exchange complete",
        messages=[
            ("outbound", "ראיתי את המזווה המאורגן שלך בסטורי. אני חייב טיפים — 90 קילו של כלים בוואן אחד קטן 😅"),
            ("inbound", "הגעת למקום הנכון 😎 קודם כל: אזורים. 'חשמל', 'הידראוליקה', 'דברים שנראים כמו מפתח ברגים אבל הם לא'."),
            ("inbound", "אחר כך קופסאות בצבעים. אדום לדחוף, צהוב ליומיומי, ירוק ל'אולי אצטרך את זה מתישהו'."),
            ("inbound", "<image>מגירת תבלינים מסודרת עם קופסאות בצבעים</image>"),
            ("inbound", "זאת מגירת הכמון שלי. תדמיין את זה עם מפתחות."),
            ("inbound", "והכי חשוב — לעגן הכל. הטאפרוור שלי לא עף כשאני טורקת דלת. המפתחות שלך לא אמורים לעוף גם."),
            ("outbound", "וואו. את גאונה. מתחיל לסדר בסופ״ש 🙏"),
            ("inbound", "בכיף! תהנה 😄"),
        ],
        expected=_NWM,
        notes="The user asked for tips and received them in full. Thanking and planning to act is not an open obligation to the sender.",
        family="reply_vs_action",
        source_group="van_tools",
        split="dev",
    ),

    EvalCase(
        id="socr-09",
        description="Advice exchange ends with a concrete request for a list",
        messages=[
            ("outbound", "ראיתי את המזווה שלך בסטורי. אני חייב טיפים — 90 קילו של כלים בוואן 😅"),
            ("inbound", "קודם כל: אזורים. אחר כך קופסאות בצבעים."),
            ("inbound", "<image>מגירת תבלינים מסודרת</image>"),
            ("outbound", "וואו, גאונות."),
            ("inbound", "רגע, בעצם — תצלם לי את פנים הוואן כמו שהוא עכשיו. אבנה לך חלוקת אזורים מותאמת."),
            ("outbound", "סגור, אצלם."),
            ("inbound", "מעולה 😄"),
        ],
        expected=_WFM,
        notes="The sender's plan is blocked on a photo the user agreed to send and hasn't.",
        family="acknowledgement_not_fulfillment",
        source_group="van_tools",
        split="dev",
    ),

    # ======================================================================
    # WORLD: escape_room_prototype (test) — from SOC-2508 idx 129:
    # "prototype 1 will be ready by friday" — a self-created deadline
    # buried in enthusiastic rambling.
    # ======================================================================

    EvalCase(
        id="socr-10",
        description="Self-imposed deadline buried in long enthusiastic exchange",
        messages=[
            ("inbound", "הסרטון של הקטן שלך מתופף על הצעצוע — אני גמור 😂 הוא גאון."),
            ("outbound", "חחח הוא לקח את זה ברצינות מלאה. מו״פ של פעוטות."),
            ("inbound", "רגע, זה נותן לי רעיון מטורף לחדר הבריחה לילדים שאני בונה. חידות מבוססות קצב!"),
            ("outbound", "וואו זה מבריק."),
            ("outbound", "אני אבנה לך אב-טיפוס של משטח תופים דיגיטלי. יהיה מוכן עד שישי, אשלח סרטון."),
            ("inbound", "אתה רציני?? זה יהיה מושלם!!"),
            ("inbound", "<gif>חתול מקליד במחשב במהירות</gif>"),
            ("inbound", "כבר מדמיין את הילדים בטרנץ' קואוט קטן פותרים חידות 🕵️"),
            ("outbound", "חחחח חובה טרנץ' קואוט. בלי פשרות."),
            ("inbound", "סגרנו 😂 טוב, הבוס מסתכל עליי צוחק מול המסך. חוזר לעבודה."),
        ],
        expected=_WFM,
        notes="The user volunteered a prototype 'by Friday' mid-conversation; laughter and drift do not close it.",
        family="user_created_commitment",
        source_group="escape_room_prototype",
        split="test",
    ),

    EvalCase(
        id="socr-11",
        description="Self-imposed deadline fulfilled after delay via media",
        messages=[
            ("inbound", "הרעיון של חידות מבוססות קצב לחדר בריחה — אני חייב את זה!"),
            ("outbound", "אני אבנה לך אב-טיפוס. יהיה מוכן עד שישי, אשלח סרטון."),
            ("inbound", "מושלם!!"),
            ("outbound", "<delay days=\"2\"/>"),
            ("outbound", "<video>סרטון של משטח תופים דיגיטלי מהבהב בסדרת צבעים</video>"),
            ("outbound", "עובד! מקישים את הרצף הנכון ונפתח מנעול."),
            ("inbound", "אין. מצב. זה מדהים!!"),
            ("inbound", "אתה אשף. מראה לצוות מחר."),
            ("outbound", "תהנו 😄"),
        ],
        expected=_NWM,
        notes="The promised prototype video was delivered after the delay. The commitment is closed.",
        family="implicit_completion",
        source_group="escape_room_prototype",
        split="test",
    ),

    # ======================================================================
    # WORLD: recipe_links_chase (dev) — long noisy version of a chase:
    # promise, day of silence, gentle reminder, more drift.
    # ======================================================================

    EvalCase(
        id="socr-12",
        description="Promise, delay, gentle chase, then drift — still open",
        messages=[
            ("inbound", "המרק ההוא שהבאת לפיקניק. אני חייבת את המתכון. חיים ומוות."),
            ("outbound", "חחח קיבלתי אותו מסבתא של דנה. אחפש את הצילום ואשלח לך."),
            ("inbound", "את הכי 💛"),
            ("inbound", "<delay days=\"1\"/>"),
            ("inbound", "בוקר טוב ☀️ מצאת במקרה את המתכון?"),
            ("inbound", "אין לחץ, סתם מתה עליו."),
            ("outbound", "אוף לגמרי שכחתי, היום היה בלגן. הבטחה שאחפש הערב."),
            ("inbound", "סבבה 😄 איך היה האירוע אתמול בסוף?"),
            ("outbound", "מטורף. 200 איש. הרגליים שלי עדיין כואבות."),
            ("inbound", "וואו. מגיע לך ספה וסרט."),
        ],
        expected=_WFM,
        notes="Two promises now (original + 'tonight'), zero deliveries. The drift into event talk changes nothing.",
        family="followup_chasing",
        source_group="recipe_links_chase",
        split="dev",
    ),

    # ======================================================================
    # WORLD: voice_note_only (dev) — UNCERTAIN: audio without transcript.
    # ======================================================================

    EvalCase(
        id="socr-13",
        description="Voice note with no transcript after neutral context",
        messages=[
            ("inbound", "היי, לגבי הדירה"),
            ("inbound", "<audio>הודעה קולית, 1:24</audio>"),
        ],
        expected=_UNC,
        notes="The substantive content is locked in an untranscribed voice note. Cannot determine if the user owes anything.",
        family="media_only_uncertain",
        source_group="voice_note_only",
        split="dev",
    ),

    # ======================================================================
    # WORLD: screenshot_no_text (test) — UNCERTAIN: image only, no ask.
    # ======================================================================

    EvalCase(
        id="socr-14",
        description="Image-only message with no request and no context",
        messages=[
            ("inbound", "<image>צילום מסך של מסמך עם טבלה, הפרטים לא קריאים</image>"),
        ],
        expected=_UNC,
        notes="A bare screenshot with no text — could be FYI, could precede a request. Not enough information.",
        family="media_only_uncertain",
        source_group="screenshot_no_text",
        split="test",
    ),

    # ======================================================================
    # WORLD: truncated_context (dev) — UNCERTAIN: window starts mid-topic.
    # ======================================================================

    EvalCase(
        id="socr-15",
        description="Window opens mid-conversation with an unresolvable reference",
        messages=[
            ("inbound", "אז מה סגרנו בסוף?"),
        ],
        expected=_WFM,
        notes="Although the referent is outside the window, the question itself is complete and directed at the user — a reply is owed regardless. (Originally labeled UNCERTAIN; relabeled after review: the user knows the context even if the analyzer doesn't.)",
        family="truncated_context_uncertain",
        source_group="truncated_context",
        split="dev",
    ),

    # ======================================================================
    # WORLD: garbled_forward (test) — UNCERTAIN: forwarded fragment.
    # ======================================================================

    EvalCase(
        id="socr-16",
        description="Forwarded fragment with unclear relevance to the user",
        messages=[
            ("inbound", "מעבירה לך מה שהיא שלחה:"),
            ("inbound", "\"...ואם לא אז שיביא את זה ביום שלישי כמו שסוכם\""),
        ],
        expected=_UNC,
        notes="A forwarded fragment about an unnamed 'he' and an unknown agreement. It may or may not concern the user.",
        family="truncated_context_uncertain",
        source_group="garbled_forward",
        split="test",
    ),

    # ======================================================================
    # WORLD: kids_bedtime_banter (dev) — base-rate: long mundane chat,
    # zero obligations.
    # ======================================================================

    EvalCase(
        id="socr-17",
        description="Long mundane parenting banter — the everyday NWM chat",
        messages=[
            ("inbound", "שלוש. שלוש פעמים הוא קם הלילה 🧟‍♀️"),
            ("outbound", "אחת אצלנו אבל היא באה עם פרויקט: 'אבא, למה לירח אין פיג'מה'"),
            ("inbound", "חחחחח שאלה לגיטימית לגמרי ל-2 בלילה"),
            ("inbound", "<image>ספל קפה ענק עם הכיתוב 'אמא מתפקדת'</image>"),
            ("inbound", "הציוד שלי להיום"),
            ("outbound", "אצלי כבר קפה שלישי. השעה 9 בבוקר."),
            ("outbound", "<gif>אוגר רץ בגלגל בוער</gif>"),
            ("inbound", "😂😂 מדויק"),
            ("inbound", "טוב, הקטן שלי מנסה לאכול את השלט. הולכת להציל אותו"),
            ("outbound", "את השלט או את הקטן? 😂 ביי!"),
        ],
        expected=_NWM,
        notes="Ten messages of pure parenting banter. No request, no promise, no expectation. The most common real-world state.",
        family="banter_no_obligation",
        source_group="kids_bedtime_banter",
        split="dev",
    ),

    # ======================================================================
    # WORLD: meme_exchange (dev) — base-rate: meme trading, reactions.
    # ======================================================================

    EvalCase(
        id="socr-18",
        description="Meme exchange with reactions — no obligation anywhere",
        messages=[
            ("inbound", "<gif>כלב יושב רגוע בחדר עולה באש, שותה קפה</gif>"),
            ("inbound", "אני בפגישת הצוות של יום ראשון"),
            ("outbound", "חחחחחח"),
            ("outbound", "<image>צילום מסך של יומן עמוס בפגישות חופפות</image>"),
            ("outbound", "תראה את היומן שלי ואז נדבר על סבל"),
            ("inbound", "אוקיי ניצחת 😂"),
            ("inbound", "מגיע לך פרס. או חופשה. או שניהם"),
            ("outbound", "אני אקח את שניהם תודה 😌"),
        ],
        expected=_NWM,
        notes="Meme trading and mutual complaining. 'You deserve a prize' is not a deliverable.",
        family="banter_no_obligation",
        source_group="meme_exchange",
        split="dev",
    ),

    # ======================================================================
    # WORLD: catch_up_vague_close (test) — base-rate: vague social closing.
    # ======================================================================

    EvalCase(
        id="socr-19",
        description="Catch-up chat ending with vague social 'let's talk soon'",
        messages=[
            ("inbound", "היייי מזמן! ראיתי שהייתם בצפון, איזה יופי"),
            ("outbound", "היה מושלם. שלושה ימים בלי קליטה. מומלץ בחום"),
            ("inbound", "חלום. אצלנו הכל כרגיל — עבודה, גן, חזרה על ההתחלה"),
            ("outbound", "מכיר את זה טוב 😅"),
            ("inbound", "צריך לקבוע משהו מתישהו! מתגעגעת"),
            ("outbound", "לגמרי! נדבר בקרוב ונסגור משהו 🤗"),
            ("inbound", "יאללה נשמע. שיהיה המשך שבוע טוב!"),
            ("outbound", "גם לכם! 💛"),
        ],
        expected=_NWM,
        notes="'We should meet sometime' + 'we'll talk soon' is social closing ritual, not a concrete scheduling obligation.",
        family="banter_no_obligation",
        source_group="catch_up_vague_close",
        split="test",
    ),

    # ======================================================================
    # WORLD: good_news_congrats (test) — base-rate: sharing news.
    # ======================================================================

    EvalCase(
        id="socr-20",
        description="News shared and congratulated — closed exchange",
        messages=[
            ("outbound", "נו אז זהו... חתמתי היום. הדירה שלנו 🎉"),
            ("inbound", "אאאאאא מזל טוב!!! 🎉🎉🎉"),
            ("inbound", "איזה כיף לכם!! מתי עוברים?"),
            ("outbound", "עוד חודשיים בערך, יש שיפוץ קטן קודם"),
            ("inbound", "מהמם. שיהיה בשעה טובה! חנוכת בית עלינו 😄"),
            ("outbound", "סגור! 🥂"),
        ],
        expected=_NWM,
        notes="The scheduling question was answered; 'housewarming on us' is celebratory talk, not a pending item.",
        family="banter_no_obligation",
        source_group="good_news_congrats",
        split="test",
    ),

    # ======================================================================
    # WORLD: trip_recs_optional (dev) — soft offer in noisy context.
    # ======================================================================

    EvalCase(
        id="socr-21",
        description="Optional offer buried in travel chat — nothing owed",
        messages=[
            ("inbound", "טסים לברלין בעוד שבועיים! פעם ראשונה"),
            ("outbound", "איזה כיף!! הייתי שם שנה שעברה"),
            ("inbound", "באמת?? איך היה?"),
            ("outbound", "מדהים. אגב אם בא לך מתישהו, יש לי רשימת מסעדות ששווה"),
            ("inbound", "אולי, נראה איך יסתדר הלו״ז 😄 בכל מקרה בעיקר באנו למוזיאונים"),
            ("outbound", "גם זה שווה. תיהנו!!"),
            ("inbound", "תודה! 🙏"),
        ],
        expected=_NWM,
        notes="'If you feel like it sometime' was answered with a non-committal 'maybe'. No obligation activated on either side.",
        family="offer_not_obligation",
        source_group="trip_recs_optional",
        split="dev",
    ),

    # ======================================================================
    # WORLD: trip_recs_requested (test) — same softness, but activated.
    # ======================================================================

    EvalCase(
        id="socr-22",
        description="Soft offer explicitly accepted with a deadline",
        messages=[
            ("inbound", "טסים לרומא ביום ראשון! לחוצה אבל מתה מהתרגשות"),
            ("outbound", "איזה יופי! הייתי שם פעמיים. אם בא לך יש לי המלצות"),
            ("inbound", "כן!! בבקשה! תשלח לי עד שבת שאספיק לתכנן"),
            ("outbound", "סגור, אעביר לך הכל מסודר"),
            ("inbound", "מלך 🙏🙏"),
        ],
        expected=_WFM,
        notes="The optional offer was converted into an accepted commitment with a deadline. The list is now owed.",
        family="offer_not_obligation",
        source_group="trip_recs_requested",
        split="test",
    ),

    # ======================================================================
    # WORLD: stale_link_promise (dev) — time decay: promise still open
    # after days of silence.
    # ======================================================================

    EvalCase(
        id="socr-23",
        description="Promise followed by days of silence — stale but open",
        messages=[
            ("inbound", "הקורס הזה שדיברת עליו נשמע בדיוק מה שאני צריכה"),
            ("outbound", "הוא מעולה. אשלח לך הערב את הלינק וקוד ההנחה"),
            ("inbound", "מושלם תודה!!"),
            ("outbound", "<delay days=\"3\"/>"),
            ("inbound", "היי 😊 רק מזכירה בעדינות את הלינק לקורס"),
        ],
        expected=_WFM,
        notes="Three days of silence do not close a promise; the reminder confirms the sender is still waiting.",
        family="followup_chasing",
        source_group="stale_link_promise",
        split="dev",
    ),

    # ======================================================================
    # WORLD: expired_request (test) — time decay: request explicitly
    # obsoleted by events.
    # ======================================================================

    EvalCase(
        id="socr-24",
        description="Old request explicitly obsoleted after long delay",
        messages=[
            ("inbound", "תוכל לבדוק אם נשארו כרטיסים להצגה של חמישי?"),
            ("outbound", "אבדוק ואעדכן"),
            ("inbound", "<delay days=\"6\"/>"),
            ("inbound", "היי! שכח מהכרטיסים אגב — השגנו דרך אמא של יעל והיה מושלם 😄"),
            ("outbound", "איזה יופי! סליחה שנעלמתי, שבוע מטורף"),
            ("inbound", "שטויות, הכל טוב!"),
        ],
        expected=_NWM,
        notes="The user dropped the ball for six days, but the request was explicitly cancelled by events. Nothing remains.",
        family="cancellation_supersession",
        source_group="expired_request",
        split="test",
    ),

    # ======================================================================
    # WORLD: interleaved_two_topics (dev) — multi-topic window where one
    # thread closes and one stays open.
    # ======================================================================

    EvalCase(
        id="socr-25",
        description="Two interleaved topics: one closed, one buried and open",
        messages=[
            ("inbound", "שתי בקשות ממך 🙏 אחת: המסמך של הביטוח. שתיים: ההמלצה על רואה החשבון"),
            ("outbound", "<image>צילום של מסמך פוליסת ביטוח</image>"),
            ("outbound", "הנה המסמך"),
            ("inbound", "תודה!!"),
            ("inbound", "אה ותגיד, איך היה הטיול בסופ״ש?"),
            ("outbound", "כיף חיים. עלינו למצפה, הילדים שרדו בקושי אבל שווה 😅"),
            ("inbound", "חחח אלופים"),
            ("inbound", "<image>תמונה מטיול משפחתי אחר, ים ושמיים</image>"),
            ("inbound", "אנחנו היינו פה שבוע שעבר"),
            ("outbound", "וואו איזה יופי!"),
            ("inbound", "ממליצה בחום. טוב, יום טוב לך!"),
        ],
        expected=_WFM,
        notes="Insurance document delivered; the accountant recommendation was never given and got buried under trip talk.",
        family="multiple_obligations",
        source_group="interleaved_two_topics",
        split="dev",
    ),

    # ======================================================================
    # WORLD: interleaved_both_closed (test) — multi-topic window where
    # everything closes.
    # ======================================================================

    EvalCase(
        id="socr-26",
        description="Two interleaved topics, both fulfilled by the end",
        messages=[
            ("inbound", "שתי בקשות 🙏 המספר של הגנן, ותמונה של הפרגולה שבניתם"),
            ("outbound", "גנן: אבי 052-1234567. אמרו לו שממני"),
            ("inbound", "תודה!"),
            ("inbound", "איך היה בחתונה אתמול אגב?"),
            ("outbound", "רקדנו עד 3. אני זומבי היום 😂"),
            ("inbound", "חחח שווה"),
            ("outbound", "<image>פרגולת עץ עם גפן מטפסת</image>"),
            ("outbound", "והנה הפרגולה"),
            ("inbound", "מהממת!! בדיוק מה שרציתי. תודה ענקית 🙏"),
            ("outbound", "בכיף!"),
        ],
        expected=_NWM,
        notes="Both requested items (number + photo) were delivered despite the wedding-talk interleave.",
        family="multiple_obligations",
        source_group="interleaved_both_closed",
        split="test",
    ),

    # ======================================================================
    # WORLD: rhetorical_outrage (dev) — rhetorical questions in banter.
    # ======================================================================

    EvalCase(
        id="socr-27",
        description="Rhetorical outrage questions — no answer actually owed",
        messages=[
            ("inbound", "ראית מה הם עשו עם החניה של הבניין???"),
            ("inbound", "<image>שער חניה חדש עם שלט 'לדיירי בניין 12 בלבד'</image>"),
            ("inbound", "אתה מאמין?? עשר שנים חנינו שם"),
            ("outbound", "לא נורמלי. פשוט חוצפה"),
            ("inbound", "נו ומה נעשה, מה. נחנה ברחוב הבא כמו פראיירים 🙄"),
            ("outbound", "כנראה 😮‍💨 בלגן"),
        ],
        expected=_NWM,
        notes="'Can you believe it??' and 'what will we do' are venting, not questions awaiting the user's answer.",
        family="rhetorical_not_question",
        source_group="rhetorical_outrage",
        split="dev",
    ),

    # ======================================================================
    # WORLD: real_question_after_banter (test) — banter that ends with a
    # genuine decision question.
    # ======================================================================

    EvalCase(
        id="socr-28",
        description="Banter pivots to a genuine decision question at the end",
        messages=[
            ("inbound", "המחשב הזה שדיברנו עליו ירד ל-2,900 באיביי"),
            ("inbound", "<image>צילום מסך של מבצע באתר קניות</image>"),
            ("outbound", "וואו ירידה רצינית"),
            ("inbound", "נכון?? כמעט אלף פחות מבארץ"),
            ("outbound", "מטורף"),
            ("inbound", "אז מה אתה אומר — שווה שאזמין לך גם? המבצע נגמר מחר בערב"),
        ],
        expected=_WFM,
        notes="After the deal banter, a real yes/no purchase decision with a deadline is squarely on the user.",
        family="rhetorical_not_question",
        source_group="real_question_after_banter",
        split="test",
    ),

    # ======================================================================
    # WORLD: plan_fully_set (dev) — a plan that fully closes.
    # ======================================================================

    EvalCase(
        id="socr-29",
        description="Meetup plan fully confirmed by both sides",
        messages=[
            ("inbound", "אז מה אמרנו — רביעי בערב?"),
            ("outbound", "כן, רביעי מעולה"),
            ("inbound", "יש! אז 20:00 אצל גילי, אני מביאה יין"),
            ("outbound", "מושלם, אני על הפיצות. נתראה שם 🍕"),
            ("inbound", "🍷🍕 נתראה!!"),
        ],
        expected=_NWM,
        notes="Time, place, and who-brings-what are all confirmed. The plan is closed; nothing is pending.",
        family="scheduling_ownership",
        source_group="plan_fully_set",
        split="dev",
    ),

    # ======================================================================
    # WORLD: plan_with_tail (test) — a plan that closes except one item.
    # ======================================================================

    EvalCase(
        id="socr-30",
        description="Plan confirmed but one confirmation still owed by user",
        messages=[
            ("inbound", "אז רביעי 20:00 אצל גילי, אני על היין"),
            ("outbound", "מושלם, אני על הפיצות 🍕"),
            ("inbound", "יש!! אה רגע — אתה מביא את המקרן בשביל הסרט? תאשר לי שאדע אם לסחוב את שלנו"),
        ],
        expected=_WFM,
        notes="The social plan is set, but a concrete yes/no about the projector is explicitly awaited from the user.",
        family="scheduling_ownership",
        source_group="plan_with_tail",
        split="test",
    ),

    # ======================================================================
    # WORLD: breakup_support (dev) — emotional support, no ask.
    # ======================================================================

    EvalCase(
        id="socr-31",
        description="Emotional support conversation with no request",
        messages=[
            ("inbound", "נפרדנו אתמול בלילה. הפעם באמת."),
            ("outbound", "אוף חמודה 💔 אני כל כך מצטערת"),
            ("outbound", "רוצה שאבוא? אני יכולה להיות אצלך בחצי שעה"),
            ("inbound", "לא צריך, אמא שלי פה. אני בסדר יחסית"),
            ("inbound", "פשוט עצוב. שלוש שנים."),
            ("outbound", "מותר להיות עצובה. זה ענק."),
            ("inbound", "תודה שאת פה 🤍"),
            ("outbound", "תמיד. כל שעה, גם ב-3 בלילה."),
            ("inbound", "🤍🤍"),
        ],
        expected=_NWM,
        notes="The user's offer to come over was declined. Emotional presence is not a pending deliverable.",
        family="banter_no_obligation",
        source_group="breakup_support",
        split="dev",
    ),

    # ======================================================================
    # WORLD: support_with_ask (test) — emotional context, real ask at end.
    # ======================================================================

    EvalCase(
        id="socr-32",
        description="Emotional conversation ends with a concrete request",
        messages=[
            ("inbound", "נפרדנו אתמול. הפעם באמת."),
            ("outbound", "אוף 💔 אני מצטער. רוצה שאבוא?"),
            ("inbound", "לא עכשיו, אני עם אחותי"),
            ("inbound", "אבל תגיד... אפשר לישון אצלכם מחר? אני לא מסוגלת להיות בדירה שלנו"),
        ],
        expected=_WFM,
        notes="A direct, answerable request (staying over tomorrow) is open, regardless of the emotional framing.",
        family="soft_requests",
        source_group="support_with_ask",
        split="test",
    ),

    # ======================================================================
    # WORLD: two_asks_one_media (dev) — partial fulfillment via media in a
    # noisy window.
    # ======================================================================

    EvalCase(
        id="socr-33",
        description="Photo delivered but the second ask is still open",
        messages=[
            ("inbound", "תשלח לי תמונה של הספה שאתם מוכרים? ואת המידות שלה"),
            ("outbound", "<image>ספה תלת-מושבית אפורה בסלון</image>"),
            ("inbound", "יפה!! בדיוק הסגנון שחיפשנו"),
            ("inbound", "מה המצב שלה מבחינת כתמים ושפשופים?"),
            ("outbound", "מצוינת, שנתיים בשימוש עדין. בלי חיות ובלי ילדים קטנים 😄"),
            ("inbound", "מעולה. אז נשאר רק עניין המידות 📏"),
        ],
        expected=_WFM,
        notes="The photo and condition questions were answered; the explicitly re-raised dimensions are still owed.",
        family="multiple_obligations",
        source_group="two_asks_one_media",
        split="dev",
    ),

    # ======================================================================
    # WORLD: one_ask_media_done (test) — media fulfills, drift continues.
    # ======================================================================

    EvalCase(
        id="socr-34",
        description="Single ask fulfilled by media, chat drifts on happily",
        messages=[
            ("inbound", "תשלח לי תמונה של החדר אחרי הצביעה? מתה לראות את הצבע"),
            ("outbound", "<image>חדר שינה צבוע בירוק זית עם פינת קריאה</image>"),
            ("inbound", "וואוווו איזה יפה!!"),
            ("inbound", "הירוק הזה מושלם. איפה קניתם את הכורסה?"),
            ("outbound", "איקאה, דגם ישן שלהם. הייתה של סבתא של רון במקור 😄"),
            ("inbound", "מהמם. נותן השראה לשיפוץ שלנו"),
            ("outbound", "בשמחה, שווה כל שנייה של בלגן"),
        ],
        expected=_NWM,
        notes="The photo was the only ask and was delivered. The armchair question was answered inline. Nothing pending.",
        family="implicit_completion",
        source_group="one_ask_media_done",
        split="test",
    ),

    # ======================================================================
    # WORLD: self_answered_question (dev) — asker resolves their own ask.
    # ======================================================================

    EvalCase(
        id="socr-35",
        description="Sender answers their own question before the user replies",
        messages=[
            ("inbound", "באיזו שעה אמרת שהטיסה שלכם נוחתת?"),
            ("inbound", "רגע אל תענה, מצאתי את זה בקבוצה. 16:40. נאסוף אתכם 😄"),
            ("outbound", "אלופים!! תודה!"),
            ("inbound", "בכיף, נתראה מחר!"),
        ],
        expected=_NWM,
        notes="The question was explicitly withdrawn and self-answered ('don't answer, found it').",
        family="cancellation_supersession",
        source_group="self_answered_question",
        split="dev",
    ),

    # ======================================================================
    # WORLD: self_answered_then_new (test) — self-resolve, then a new ask.
    # ======================================================================

    EvalCase(
        id="socr-36",
        description="Self-answered question immediately followed by a new ask",
        messages=[
            ("inbound", "באיזו שעה הטיסה שלכם נוחתת?"),
            ("inbound", "רגע, מצאתי בקבוצה — 16:40. נאסוף אתכם"),
            ("outbound", "אלופים!"),
            ("inbound", "רק תשלח לי את מספר הטרמינל כשתדעו, שלא נחכה בצד הלא נכון 😅"),
            ("outbound", "ברור, ברגע שנדע"),
        ],
        expected=_WFM,
        notes="The first question self-resolved, but a new concrete deliverable (terminal number) was accepted by the user.",
        family="old_closes_new_opens",
        source_group="self_answered_then_new",
        split="test",
    ),

    # ======================================================================
    # WORLD: chain_forward (dev) — forwarded chain message, no obligation.
    # ======================================================================

    EvalCase(
        id="socr-37",
        description="Forwarded chain/broadcast message — nothing personal",
        messages=[
            ("inbound", "הועבר: 🎉 מבצע ענק בחנות של דודו! כל החנות 30% הנחה עד שישי! תעבירו הלאה 🎉"),
        ],
        expected=_NWM,
        notes="A forwarded promotional broadcast. 'Pass it on' is not a personal request awaiting the user.",
        family="banter_no_obligation",
        source_group="chain_forward",
        split="dev",
    ),

    # ======================================================================
    # WORLD: forward_with_ask (test) — forward plus a personal question.
    # ======================================================================

    EvalCase(
        id="socr-38",
        description="Forwarded listing with a personal decision question attached",
        messages=[
            ("inbound", "הועבר: 🏠 3 חדרים, קומה 2, מרפסת שמש, כניסה מיידית"),
            ("inbound", "<image>תמונות של דירה מוארת עם מרפסת</image>"),
            ("inbound", "זה נראה לי בול בשבילכם. להגיד לו שתבואו לראות? הוא שכן שלי"),
        ],
        expected=_WFM,
        notes="Unlike a bare forward, this one ends with a personal yes/no question blocking the sender's next step.",
        family="offer_requires_answer",
        source_group="forward_with_ask",
        split="test",
    ),

    # ======================================================================
    # WORLD: late_night_spiral (dev) — long rambling multi-message chat
    # with one buried commitment made BY the user, SOC-style pacing.
    # ======================================================================

    EvalCase(
        id="socr-39",
        description="Rambling late-night chat hides one real user commitment",
        messages=[
            ("inbound", "ערה?"),
            ("outbound", "צופה בפרק שלישי ברצף. אז כן, ערה ובבחירה גרועה 😅"),
            ("inbound", "חחח איזו סדרה?"),
            ("outbound", "זאת עם הבלשית האיסלנדית. מכורה."),
            ("inbound", "אה!! זאת שרציתי להתחיל!"),
            ("outbound", "חובה. יש לי גם רשימה שלמה של סדרות נורדיות, אשלח לך אותה מחר כשאהיה ליד המחשב"),
            ("inbound", "כן בבקשה!!"),
            ("inbound", "טוב עכשיו אני חייבת לדעת — היא מוצאת את האחות בסוף??"),
            ("outbound", "אין ספוילרים אצלי 😌"),
            ("inbound", "אכזרית!! 😭"),
            ("outbound", "חחח תתחילי לצפות ותגלי. טוב, פרק רביעי קורא לי. לילה!"),
            ("inbound", "לילה! 🌙"),
        ],
        expected=_WFM,
        notes="Buried mid-banter: the user promised the Nordic-series list 'tomorrow at the computer'. Still open.",
        family="buried_obligation_long",
        source_group="late_night_spiral",
        split="dev",
    ),

    # ======================================================================
    # WORLD: workshop_debrief (test) — long noisy chat, obligation on the
    # OTHER side, none on the user.
    # ======================================================================

    EvalCase(
        id="socr-40",
        description="Long chat where the open obligation belongs to the other person",
        messages=[
            ("outbound", "איך הלכה הסדנה בסוף?? חשבתי עליך כל הבוקר"),
            ("inbound", "מדהים!!! 18 משתתפים, אפס תקלות"),
            ("inbound", "<image>אולם סדנאות עם עמדות יצירה מלאות</image>"),
            ("outbound", "וואוו איזה יופי!! ידעתי שתקרעי אותה"),
            ("inbound", "הייתי בעננים. ואחת המשתתפות רוצה להזמין אותי לסדנה פרטית!"),
            ("outbound", "מטורף!! את חייבת לספר לי איך זה מתקדם"),
            ("inbound", "ברור. אני שולחת לך מחר את התמונות מהצלם, יצאו מהממות"),
            ("outbound", "מחכה!! 📸"),
            ("inbound", "טוב אני קורסת. תודה שאת תמיד בעדי 🤍"),
            ("outbound", "תמיד תמיד. לילה טוב אלופה!"),
        ],
        expected=_NWM,
        notes="The only open item (photographer's photos) is owed BY the other person TO the user. Nothing is on the user.",
        family="ball_with_them",
        source_group="workshop_debrief",
        split="test",
    ),
]


SOC2508_DEV_CASES: list[EvalCase] = [c for c in SOC2508_ALL_CASES if c.split == "dev"]
SOC2508_TEST_CASES: list[EvalCase] = [c for c in SOC2508_ALL_CASES if c.split == "test"]
