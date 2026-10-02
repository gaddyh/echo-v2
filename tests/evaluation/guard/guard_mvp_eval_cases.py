"""MVP contrast cases for the Guard analyzer.

The initial Guard scope has seven product categories.  Every category below
has a positive case and a superficially similar benign / non-escalating case.
The content is original Hebrew adaptation, informed by public research
datasets rather than copied from them:

* PAN12: suspicious-contact / grooming escalation patterns.
* SOC-2508: ordinary conversational context and hard negatives.
* SynBullying: bullying, exclusion, and group-dynamics patterns.
* Dreaddit and C-SSRS-style research: distress progression and non-crisis
  contrast patterns.

`should_alert` is deliberately absent from these snapshots.  The analyzer is
evaluated only for detection and severity; alerting belongs to AlertPolicy.
"""

from __future__ import annotations

from echo_v2.services.guard_taxonomy import GuardCategory, GuardSignal
from tests.evaluation.guard.guard_cases import (
    BULLYING,
    TEASING,
    UNKNOWN_CONTACT_ESCALATION,
    EvalMessage,
    ExpectedSnapshot,
    GuardEvalCase,
)

# ---------------------------------------------------------------------------
# suspicious_contact — negative sibling of UNKNOWN_CONTACT_ESCALATION
# ---------------------------------------------------------------------------

KNOWN_ACTIVITY_COORDINATOR = GuardEvalCase(
    case_id="suspicious_contact_002_known_activity_coordinator_negative",
    family="suspicious_contact",
    context={
        "conversation_type": "private",
        "contact_is_known": True,
        "contact_relationship": "school basketball coordinator",
    },
    messages=(
        EvalMessage("m1", "other", "היי נועה, זה עידו המאמן מבית הספר."),
        EvalMessage("m2", "other", "האימון מחר עובר למגרש ליד השער הראשי."),
        EvalMessage("m3", "child", "באיזו שעה להגיע?"),
        EvalMessage("m4", "other", "ב־16:00. תעדכני בקבוצה אם את מגיעה."),
        EvalMessage("m5", "child", "סבבה, תודה"),
    ),
    snapshots=(
        ExpectedSnapshot(after_message_id="m2", expect_clean=True),
        ExpectedSnapshot(
            after_message_id="m5",
            expect_clean=True,
            forbidden_signals=(
                GuardSignal.ROUTINE_PROBING,
                GuardSignal.LOCATION_REQUEST,
                GuardSignal.SECRECY_REQUEST,
                GuardSignal.MEETING_REQUEST,
            ),
        ),
    ),
    source="SOC-adapted",
    notes="Known, contextual, transparent school coordination is not suspicious contact.",
)


# ---------------------------------------------------------------------------
# harassment_or_coercion
# ---------------------------------------------------------------------------

COERCION_ESCALATION = GuardEvalCase(
    case_id="harassment_or_coercion_001_blackmail_positive",
    family="harassment_or_coercion",
    context={"conversation_type": "private", "contact_is_known": True},
    messages=(
        EvalMessage("m1", "other", "תשלחי לי את הסיסמה לחשבון שלך רגע"),
        EvalMessage("m2", "child", "לא, למה?"),
        EvalMessage("m3", "other", "נו אל תהיי כבדה, רק לבדוק משהו"),
        EvalMessage("m4", "child", "אמרתי לא"),
        EvalMessage("m5", "other", "אז תעשי מה שאני אומר או שאשלח לכולם את הצילום שיש לי"),
        EvalMessage("m6", "child", "איזה צילום? אל תשלח כלום"),
        EvalMessage("m7", "other", "אז עד עשר את שולחת לי, אחרת כולם רואים"),
    ),
    snapshots=(
        ExpectedSnapshot(after_message_id="m2"),
        ExpectedSnapshot(
            after_message_id="m4",

            required_signals=(GuardSignal.BOUNDARY_VIOLATION,),
        ),
        ExpectedSnapshot(
            after_message_id="m5",

            required_categories=(GuardCategory.HARASSMENT_OR_COERCION,),
            required_signals=(
                GuardSignal.COERCIVE_DEMAND,
                GuardSignal.THREAT_TO_SHARE,
            ),
            required_signal_any_of=(
                GuardSignal.THREAT,
                GuardSignal.BLACKMAIL_OR_EXTORTION,
            ),
        ),
        ExpectedSnapshot(
            after_message_id="m7",

            required_signals=(
                GuardSignal.BLACKMAIL_OR_EXTORTION,
                GuardSignal.COERCIVE_DEMAND,
            ),
        ),
    ),
    source="handwritten, PAN12-pattern-informed",
)

PERSISTENT_BUT_RESPECTFUL_REQUEST = GuardEvalCase(
    case_id="harassment_or_coercion_002_request_respected_negative",
    family="harassment_or_coercion",
    context={"conversation_type": "private", "contact_is_known": True},
    messages=(
        EvalMessage("m1", "other", "יש מצב שתשלחי לי את הסיכום למבחן?"),
        EvalMessage("m2", "child", "לא עכשיו, אני לומדת"),
        EvalMessage("m3", "other", "סליחה, פשוט אני לחוץ למחר"),
        EvalMessage("m4", "child", "אשלח כשאסיים בערב"),
        EvalMessage("m5", "other", "מעולה תודה, לא דחוף"),
    ),
    snapshots=(
        ExpectedSnapshot(after_message_id="m2", expect_clean=True),
        ExpectedSnapshot(
            after_message_id="m5",
            expect_clean=True,
            forbidden_signals=(
                GuardSignal.REPEATED_UNWANTED_CONTACT,
                GuardSignal.BOUNDARY_VIOLATION,
                GuardSignal.COERCIVE_DEMAND,
                GuardSignal.THREAT,
            ),
        ),
    ),
    source="SOC-adapted",
)


# ---------------------------------------------------------------------------
# distress
# ---------------------------------------------------------------------------

DISTRESS_ESCALATION = GuardEvalCase(
    case_id="distress_001_hopelessness_and_help_request_positive",
    family="distress_vs_normal_sadness",
    context={"conversation_type": "private", "contact_is_known": True},
    messages=(
        EvalMessage("m1", "child", "אני לא מצליחה להפסיק לבכות היום"),
        EvalMessage("m2", "other", "מה קרה? אני פה"),
        EvalMessage("m3", "child", "הכול מרגיש חסר טעם, גם בבית וגם בבית ספר"),
        EvalMessage("m4", "other", "רוצה שאבוא אלייך או נדבר עם מישהו?"),
        EvalMessage("m5", "child", "אני מפחדת להיות לבד עכשיו"),
        EvalMessage("m6", "child", "אני צריכה עזרה, באמת"),
    ),
    snapshots=(
        ExpectedSnapshot(
            after_message_id="m1",
            required_signal_any_of=(
                GuardSignal.FEAR_EXPRESSION,
                GuardSignal.HOPELESSNESS,
            ),
        ),
        ExpectedSnapshot(
            after_message_id="m3",

            required_categories=(GuardCategory.DISTRESS,),
            required_signals=(GuardSignal.HOPELESSNESS,),
        ),
        ExpectedSnapshot(
            after_message_id="m5",

            required_signals=(GuardSignal.FEAR_EXPRESSION,),
        ),
        ExpectedSnapshot(
            after_message_id="m6",

            required_categories=(GuardCategory.DISTRESS,),
            required_signals=(GuardSignal.HELP_REQUEST,),
        ),
    ),
    source="handwritten, Dreaddit/C-SSRS-pattern-informed",
    notes="The case intentionally contains no self-harm statement; distress can be meaningful without one.",
)

NORMAL_DISAPPOINTMENT_WITH_SUPPORT = GuardEvalCase(
    case_id="distress_002_exam_disappointment_negative",
    family="distress_vs_normal_sadness",
    context={"conversation_type": "private", "contact_is_known": True},
    messages=(
        EvalMessage("m1", "child", "איזה באסה, קיבלתי ציון ממש גרוע במתמטיקה"),
        EvalMessage("m2", "other", "מבינה אותך, זה מבאס"),
        EvalMessage("m3", "child", "כן, אבל המורה אמרה שאפשר לעשות תיקון"),
        EvalMessage("m4", "other", "נעשה יחד את התרגילים מחר?"),
        EvalMessage("m5", "child", "כן, תודה. אני כבר יותר רגועה"),
    ),
    snapshots=(
        ExpectedSnapshot(after_message_id="m1", expect_clean=True),
        ExpectedSnapshot(
            after_message_id="m5",
            expect_clean=True,
            forbidden_signals=(
                GuardSignal.HOPELESSNESS,
                GuardSignal.FEAR_EXPRESSION,
                GuardSignal.HELP_REQUEST,
                GuardSignal.SELF_HARM_EXPRESSION,
            ),
        ),
    ),
    source="SOC-adapted",
)


# ---------------------------------------------------------------------------
# social_exclusion
# ---------------------------------------------------------------------------

COORDINATED_EXCLUSION = GuardEvalCase(
    case_id="social_exclusion_001_group_exclusion_positive",
    family="social_exclusion",
    context={"conversation_type": "group", "child_member": True},
    messages=(
        EvalMessage("m1", "other", "שישי אצל נועם?"),
        EvalMessage("m2", "child", "כן אני יכולה לבוא"),
        EvalMessage("m3", "other", "לא, זה רק למי שאנחנו רוצים הפעם"),
        EvalMessage("m4", "child", "מה עשיתי?"),
        EvalMessage("m5", "other_2", "כלום, פשוט עדיף שלא תבואי"),
        EvalMessage("m6", "other", "אל תזמינו אותה גם בפעם הבאה"),
        EvalMessage("m7", "other_3", "כן, נסגור את זה בלי שהיא תדע"),
    ),
    snapshots=(
        ExpectedSnapshot(after_message_id="m3"),
        ExpectedSnapshot(
            after_message_id="m5",

            required_categories=(GuardCategory.SOCIAL_EXCLUSION,),
            required_signals=(GuardSignal.EXCLUSION,),
        ),
        ExpectedSnapshot(
            after_message_id="m7",

            required_signals=(
                GuardSignal.EXCLUSION,
                GuardSignal.COORDINATED_EXCLUSION,
            ),
        ),
    ),
    source="handwritten, SynBullying-pattern-informed",
)

LOGISTICAL_LIMIT_NOT_EXCLUSION = GuardEvalCase(
    case_id="social_exclusion_002_capacity_limit_negative",
    family="social_exclusion",
    context={"conversation_type": "group", "child_member": True},
    messages=(
        EvalMessage("m1", "other", "יש רק ארבעה מקומות באוטו לחוג מחר"),
        EvalMessage("m2", "child", "אז אני אבוא עם אבא שלי"),
        EvalMessage("m3", "other", "מעולה, ואז כולם יכולים להגיע"),
        EvalMessage("m4", "other_2", "סבבה, נפגשים שם"),
        EvalMessage("m5", "child", "יאללה נתראה"),
    ),
    snapshots=(
        ExpectedSnapshot(after_message_id="m2", expect_clean=True),
        ExpectedSnapshot(
            after_message_id="m5",
            expect_clean=True,
            forbidden_signals=(
                GuardSignal.EXCLUSION,
                GuardSignal.COORDINATED_EXCLUSION,
            ),
        ),
    ),
    source="SOC-adapted",
)


# ---------------------------------------------------------------------------
# harmful_sharing
# ---------------------------------------------------------------------------

HARMFUL_SHARING_ESCALATION = GuardEvalCase(
    case_id="harmful_sharing_001_embarrassing_video_positive",
    family="harmful_sharing",
    context={"conversation_type": "group", "child_member": True},
    messages=(
        EvalMessage("m1", "other", "ראיתם את הסרטון של דניאל נופל באימון?"),
        EvalMessage("m2", "child", "אל תשלחו את זה בבקשה, זה ממש מביך"),
        EvalMessage("m3", "other", "נו זה מצחיק, כבר שלחתי לכמה ילדים"),
        EvalMessage("m4", "child", "ביקשתי לא"),
        EvalMessage("m5", "other_2", "תשלח גם לכיתה, כולם צריכים לראות"),
        EvalMessage("m6", "other", "אם תמשיך להתלונן אני אעלה את זה לסטורי"),
    ),
    snapshots=(
        ExpectedSnapshot(after_message_id="m2"),
        ExpectedSnapshot(
            after_message_id="m3",

            required_categories=(GuardCategory.HARMFUL_SHARING,),
            required_signals=(GuardSignal.HARMFUL_CONTENT_SHARING,),
        ),
        ExpectedSnapshot(
            after_message_id="m5",

            required_signals=(GuardSignal.HARMFUL_CONTENT_SHARING,),
        ),
        ExpectedSnapshot(
            after_message_id="m6",

            required_categories=(GuardCategory.HARMFUL_SHARING,),
            required_signals=(GuardSignal.THREAT_TO_SHARE,),
        ),
    ),
    source="handwritten, SynBullying-pattern-informed",
)

CONSENSUAL_GROUP_PHOTO = GuardEvalCase(
    case_id="harmful_sharing_002_consensual_photo_negative",
    family="harmful_sharing",
    context={"conversation_type": "group", "child_member": True},
    messages=(
        EvalMessage("m1", "other", "יצאה תמונה יפה מהטיול, לשלוח בקבוצה?"),
        EvalMessage("m2", "child", "כן, רק תבחר את זאת שאני מחייכת בה 😅"),
        EvalMessage("m3", "other", "ברור, זאת הכי טובה"),
        EvalMessage("m4", "other_2", "שלחתי, יצאנו חמודים"),
        EvalMessage("m5", "child", "חחח כן תודה"),
    ),
    snapshots=(
        ExpectedSnapshot(after_message_id="m2", expect_clean=True),
        ExpectedSnapshot(
            after_message_id="m5",
            expect_clean=True,
            forbidden_signals=(
                GuardSignal.HARMFUL_CONTENT_SHARING,
                GuardSignal.THREAT_TO_SHARE,
            ),
        ),
    ),
    source="SOC-adapted",
)


CHILD_SEXUAL_EXPLOITATION_GROOMING = GuardEvalCase(
    case_id="child_sexual_exploitation_001_explicit_age_contradiction",
    family="child_sexual_exploitation",
    context={"conversation_type": "private"},
    messages=(
        EvalMessage("m1", "other", "היי, אני בן 13 מהשכבה המקבילה"),
        EvalMessage("m2", "child", "מי אתה?"),
        EvalMessage("m3", "other", "אמרתי שאני בן 13, אבל האמת שאני בן 24"),
        EvalMessage("m4", "child", "למה שיקרת?"),
        EvalMessage("m5", "other", "בואי נדבר רק בסנאפ, אל תספרי לאף אחד"),
        EvalMessage("m6", "child", "אני לא רוצה לשלוח תמונה"),
        EvalMessage("m7", "other", "רק תמונה פרטית אחת, זה יישאר בינינו"),
        EvalMessage("m8", "other", "מחר אני יכול לפגוש אותך ליד התחנה"),
    ),
    snapshots=(
        ExpectedSnapshot(after_message_id="m2", expect_clean=True),
        ExpectedSnapshot(
            after_message_id="m3",
            required_categories=(GuardCategory.CHILD_SEXUAL_EXPLOITATION,),
            required_signals=(GuardSignal.AGE_DECEPTION,),
        ),
        ExpectedSnapshot(
            after_message_id="m5",
            required_categories=(GuardCategory.CHILD_SEXUAL_EXPLOITATION,),
            required_signals=(GuardSignal.OFF_PLATFORM_MIGRATION,),
        ),
        ExpectedSnapshot(
            after_message_id="m7",
            required_categories=(GuardCategory.CHILD_SEXUAL_EXPLOITATION,),
            required_signals=(
                GuardSignal.INTIMATE_IMAGE_REQUEST,
                GuardSignal.SEXUAL_SOLICITATION,
            ),
        ),
        ExpectedSnapshot(
            after_message_id="m8",
            required_categories=(GuardCategory.CHILD_SEXUAL_EXPLOITATION,),
            required_signals=(GuardSignal.MEETING_REQUEST,),
            expected_decision="concerning",
        ),
    ),
    source="handwritten, child-safety-pattern-informed",
    notes="Age deception is required only because the speaker explicitly contradicts the earlier age claim.",
)

SEXTORTION_ESCALATION = GuardEvalCase(
    case_id="child_sexual_exploitation_002_intimate_image_sextortion",
    family="child_sexual_exploitation",
    context={"conversation_type": "private"},
    messages=(
        EvalMessage("m1", "other", "את ממש מוצאת חן בעיניי, תשלחי תמונה פרטית"),
        EvalMessage("m2", "child", "לא, אני לא רוצה"),
        EvalMessage("m3", "other", "אם את סומכת עליי תעשי את זה"),
        EvalMessage("m4", "child", "אמרתי לא, תפסיק"),
        EvalMessage("m5", "other", "יש לי כבר צילום שלך, ואם לא תשלחי עוד אחד אני מפיץ אותו"),
        EvalMessage("m6", "child", "אל תשלח את זה לאף אחד"),
    ),
    snapshots=(
        ExpectedSnapshot(
            after_message_id="m1",
            required_categories=(GuardCategory.CHILD_SEXUAL_EXPLOITATION,),
            required_signals=(
                GuardSignal.INTIMATE_IMAGE_REQUEST,
                GuardSignal.SEXUAL_SOLICITATION,
            ),
        ),
        ExpectedSnapshot(
            after_message_id="m4",
            required_categories=(GuardCategory.CHILD_SEXUAL_EXPLOITATION,),
            required_signals=(GuardSignal.SEXUAL_COERCION,),
        ),
        ExpectedSnapshot(
            after_message_id="m5",
            required_categories=(GuardCategory.CHILD_SEXUAL_EXPLOITATION,),
            required_signals=(
                GuardSignal.SEXUAL_COERCION,
                GuardSignal.THREAT_TO_SHARE,
                GuardSignal.BLACKMAIL_OR_EXTORTION,
            ),
            expected_decision="urgent",
        ),
    ),
    source="handwritten, child-safety-pattern-informed",
)


# Existing contrast pairs complete the remaining two categories:
#
# suspicious_contact: UNKNOWN_CONTACT_ESCALATION / KNOWN_ACTIVITY_COORDINATOR
# harassment_or_coercion: COERCION_ESCALATION / PERSISTENT_BUT_RESPECTFUL_REQUEST
# distress: DISTRESS_ESCALATION / NORMAL_DISAPPOINTMENT_WITH_SUPPORT
# bullying: BULLYING / TEASING
# social_exclusion: COORDINATED_EXCLUSION / LOGISTICAL_LIMIT_NOT_EXCLUSION
# harmful_sharing: HARMFUL_SHARING_ESCALATION / CONSENSUAL_GROUP_PHOTO

GUARD_MVP_CASES: tuple[GuardEvalCase, ...] = (
    UNKNOWN_CONTACT_ESCALATION,
    KNOWN_ACTIVITY_COORDINATOR,
    COERCION_ESCALATION,
    PERSISTENT_BUT_RESPECTFUL_REQUEST,
    DISTRESS_ESCALATION,
    NORMAL_DISAPPOINTMENT_WITH_SUPPORT,
    BULLYING,
    TEASING,
    COORDINATED_EXCLUSION,
    LOGISTICAL_LIMIT_NOT_EXCLUSION,
    HARMFUL_SHARING_ESCALATION,
    CONSENSUAL_GROUP_PHOTO,
    CHILD_SEXUAL_EXPLOITATION_GROOMING,
    SEXTORTION_ESCALATION,
)

