"""Guard v1 comprehensive baseline: 12 development and 20 held-out test cases.

The messages are original Hebrew adaptations, not copied dataset records.  Each
case is tagged with the public corpus whose *interaction pattern* informed it:

* PAN12-pattern-informed: suspicious contact / coercive escalation.
* SOC-adapted: ordinary, benign conversational hard negatives.
* SynBullying-pattern-informed: group bullying, exclusion, and sharing harm.
* Dreaddit/C-SSRS-pattern-informed: distress language and recovery contrasts.

Never load TEST_CASES into a few-shot prompt.  Prompt iteration may use DEV_CASES
only; TEST_CASES are the held-out regression set.
"""

from __future__ import annotations

from echo_v2.services.guard_taxonomy import (
    GuardCategory as C,
)
from echo_v2.services.guard_taxonomy import (
    GuardDecision,
)
from echo_v2.services.guard_taxonomy import (
    GuardSignal as S,
)
from tests.evaluation.guard.guard_cases import (
    EvalMessage,
    ExpectedSnapshot,
    GuardEvalCase,
)
from tests.evaluation.guard.guard_mvp_eval_cases import GUARD_MVP_CASES


def m(id: str, sender: str, text: str) -> EvalMessage:
    return EvalMessage(id, sender, text)


def snap(
    after: str,
    _legacy_decisions: tuple[GuardDecision, ...] | tuple[str, ...] = (),
    *,
    categories: tuple[C, ...] = (),
    signals: tuple[S, ...] = (),
    any_signals: tuple[S, ...] = (),
    forbidden: tuple[S, ...] = (),
) -> ExpectedSnapshot:
    return ExpectedSnapshot(
        after_message_id=after,
        acceptable_decisions=(),
        required_categories=categories,
        required_signals=signals,
        required_signal_any_of=any_signals,
        forbidden_signals=forbidden,
    )


# ---------------------------------------------------------------------------
# Held-out test cases: private chats
# ---------------------------------------------------------------------------

TEST_ROMANCE_TO_GROOMING = GuardEvalCase(
    case_id="test_suspicious_contact_001_romance_to_grooming",
    family="romance_vs_grooming",
    context={"conversation_type": "private", "contact_is_known": False},
    messages=(
        m("m1", "other", "היי, ראיתי אותך בטיקטוק. את נראית ממש חמודה"),
        m("m2", "child", "מי אתה?"),
        m("m3", "other", "לא חשוב, אני בערך בגיל שלך. אל תספרי לחברות שלך עדיין"),
        m("m4", "child", "למה?"),
        m("m5", "other", "הן סתם יקנאו. יש לך אינסטגרם פרטי?"),
        m("m6", "child", "כן"),
        m("m7", "other", "תשלחי לי איפה את לומדת, אולי אעבור לידך בהפסקה"),
    ),
    snapshots=(
        snap("m2", ("none", "watch")),
        snap("m3", signals=(S.SECRECY_REQUEST,)),
        snap(
            "m5", (GuardDecision.CONCERNING,), categories=(C.SUSPICIOUS_CONTACT,),
            signals=(S.SECRECY_REQUEST, S.PERSONAL_INFORMATION_REQUEST),
        ),
        snap(
            "m7", ("concerning", "urgent"), categories=(C.SUSPICIOUS_CONTACT,),
            signals=(S.SECRECY_REQUEST, S.LOCATION_REQUEST),
        ),
    ),
    source="handwritten, PAN12-pattern-informed",
)

TEST_UNKNOWN_BENIGN_SERVICE = GuardEvalCase(
    case_id="test_suspicious_contact_002_unknown_benign_service",
    family="unknown_but_benign_contact",
    context={"conversation_type": "private", "contact_is_known": False},
    messages=(
        m("m1", "other", "היי, זאת הודעה אוטומטית מהספרייה העירונית"),
        m("m2", "other", "הספר שהזמנת מחכה לך בדלפק עד יום חמישי"),
        m("m3", "child", "תודה"),
        m("m4", "other", "בשמחה. שעות הפתיחה באתר העירייה"),
    ),
    snapshots=(
        snap("m2"),
        snap("m4", forbidden=(S.LOCATION_REQUEST, S.SECRECY_REQUEST, S.MEETING_REQUEST)),
    ),
    source="SOC-adapted",
)

TEST_CREDIBLE_THREAT = GuardEvalCase(
    case_id="test_harassment_001_credible_threat",
    family="credible_threat_vs_idiom",
    context={"conversation_type": "private", "contact_is_known": True},
    messages=(
        m("m1", "other", "אם תספר למורה על מה שהיה, תהיה לך בעיה"),
        m("m2", "child", "תפסיק לאיים עליי"),
        m("m3", "other", "מחר אחרי בית ספר אני מחכה לך ליד החניה"),
        m("m4", "child", "אני לא בא"),
        m("m5", "other", "נראה אותך. אני יודע באיזה אוטובוס אתה נוסע"),
    ),
    snapshots=(
        snap("m1", signals=(S.THREAT,)),
        snap("m3", categories=(C.HARASSMENT_OR_COERCION,), signals=(S.THREAT, S.MEETING_REQUEST)),
        snap("m5", signals=(S.THREAT, S.OFFLINE_KNOWLEDGE)),
    ),
    source="handwritten, PAN12-pattern-informed",
)

TEST_GAMING_HYPERBOLE = GuardEvalCase(
    case_id="test_harassment_002_gaming_hyperbole_oos",
    family="credible_threat_vs_idiom",
    context={"conversation_type": "private", "contact_is_known": True, "scope": "oos"},
    messages=(
        m("m1", "other", "די נו, הרסת לי את המשחק שוב"),
        m("m2", "child", "חחח הייתי טוב מדי"),
        m("m3", "other", "אני מחסל אותך בסיבוב הבא 😂"),
        m("m4", "child", "נראה אותך"),
    ),
    snapshots=(
        snap("m4", forbidden=(S.THREAT, S.COERCIVE_DEMAND)),
    ),
    source="SOC-adapted",
)

TEST_PRESSURE_WITHOUT_THREAT = GuardEvalCase(
    case_id="test_harassment_003_pressure_without_explicit_threat",
    family="pressure_without_explicit_threat",
    context={"conversation_type": "private", "contact_is_known": True},
    messages=(
        m("m1", "other", "תשלחי לי את הפתרונות שלך לפני המבחן"),
        m("m2", "child", "לא, אני לא רוצה"),
        m("m3", "other", "אבל את חייבת לי אחרי שעזרתי לך פעם"),
        m("m4", "child", "לא ביקשתי את זה ממך"),
        m("m5", "other", "אז את ממש אגואיסטית. אני לא מפסיק עד שתשלחי"),
    ),
    snapshots=(
        snap("m2", signals=(S.BOUNDARY_VIOLATION,)),
        snap("m5", categories=(C.HARASSMENT_OR_COERCION,), signals=(S.REPEATED_UNWANTED_CONTACT, S.COERCIVE_DEMAND)),
    ),
    source="handwritten, PAN12-pattern-informed",
)

TEST_IMMEDIATE_SELF_HARM_RISK = GuardEvalCase(
    case_id="test_distress_001_immediate_self_harm_risk",
    family="self_harm_immediacy",
    context={"conversation_type": "private", "contact_is_known": True},
    messages=(
        m("m1", "child", "אני לא יכולה יותר עם הכול"),
        m("m2", "other", "אני איתך, מה קורה?"),
        m("m3", "child", "אני חושבת לפגוע בעצמי עכשיו"),
        m("m4", "other", "בבקשה אל תהיי לבד, אני מתקשרת אלייך עכשיו"),
    ),
    snapshots=(
        snap("m1", ("watch", "concerning")),
        snap("m3", categories=(C.DISTRESS,), signals=(S.SELF_HARM_EXPRESSION,)),
        snap("m4", signals=(S.SELF_HARM_EXPRESSION,)),
    ),
    source="handwritten, Dreaddit/C-SSRS-pattern-informed",
)

TEST_DISTRESSED_FRIEND_NOT_CHILD = GuardEvalCase(
    case_id="test_distress_002_friend_distressed_oos",
    family="child_supporting_distressed_friend",
    context={"conversation_type": "private", "contact_is_known": True, "scope": "oos"},
    messages=(
        m("m1", "other", "אני ממש לא בסדר היום, לא רוצה להיות לבד"),
        m("m2", "child", "אני פה איתך. רוצה שאפנה לאחותך?"),
        m("m3", "other", "אולי כן, תודה שאת עונה"),
        m("m4", "child", "אני נשארת איתך עד שהיא תגיע"),
    ),
    snapshots=(
        snap("m4", forbidden=(S.HOPELESSNESS, S.SELF_HARM_EXPRESSION)),
    ),
    source="handwritten, Dreaddit-pattern-informed",
    notes="The initial MVP protects the monitored child; routing risk to a distressed peer is future scope.",
)


# ---------------------------------------------------------------------------
# Held-out test cases: group chats
# ---------------------------------------------------------------------------

TEST_SINGLE_CONFLICT = GuardEvalCase(
    case_id="test_bullying_001_single_conflict_negative",
    family="single_conflict_vs_repeated_targeting",
    context={"conversation_type": "group", "child_member": True},
    messages=(
        m("m1", "other", "למה מחקת את ההודעה שלי?"),
        m("m2", "child", "כי היא הייתה מעליבה"),
        m("m3", "other", "צודק, נסחפתי. סליחה"),
        m("m4", "child", "בסדר, בוא נמשיך הלאה"),
    ),
    snapshots=(
        snap("m4", forbidden=(S.REPEATED_TARGETING, S.GROUP_PILE_ON)),
    ),
    source="SOC-adapted",
)

TEST_GROUP_PILE_ON = GuardEvalCase(
    case_id="test_bullying_002_group_pile_on_positive",
    family="group_pile_on",
    context={"conversation_type": "group", "child_member": True},
    messages=(
        m("m1", "other", "שוב איחרת, ברור"),
        m("m2", "child", "האוטובוס פשוט לא הגיע"),
        m("m3", "other_2", "תמיד יש לך תירוץ"),
        m("m4", "child", "אפשר להפסיק?"),
        m("m5", "other_3", "לא, זה באמת מצחיק אותנו"),
        m("m6", "other", "אל תבוא גם לפגישה מחר, אתה רק מעכב"),
    ),
    snapshots=(
        snap("m3", signals=(S.REPEATED_TARGETING,)),
        snap("m5", categories=(C.BULLYING,), signals=(S.GROUP_PILE_ON, S.REPEATED_TARGETING)),
        snap("m6", categories=(C.BULLYING, C.SOCIAL_EXCLUSION), signals=(S.EXCLUSION, S.GROUP_PILE_ON)),
    ),
    source="handwritten, SynBullying-pattern-informed",
)

TEST_CHILD_IS_AGGRESSOR = GuardEvalCase(
    case_id="test_bullying_003_child_is_aggressor_oos",
    family="child_is_aggressor_not_target",
    context={"conversation_type": "group", "child_member": True, "scope": "oos"},
    messages=(
        m("m1", "child", "איזה אפס אתה, הרסת הכול"),
        m("m2", "other", "די, זה לא מצחיק"),
        m("m3", "child", "מגיע לך, אף אחד לא רוצה אותך איתנו"),
        m("m4", "other_2", "תפסיקו כבר"),
    ),
    snapshots=(
        snap("m4", forbidden=(S.REPEATED_TARGETING, S.GROUP_PILE_ON)),
    ),
    source="handwritten, SynBullying-pattern-informed",
    notes="Out of scope for v1: the child is the aggressor rather than the target.",
)

TEST_AMBIGUOUS_INVITATION = GuardEvalCase(
    case_id="test_exclusion_001_ambiguous_invitation_negative",
    family="ambiguous_invitation",
    context={"conversation_type": "group", "child_member": True},
    messages=(
        m("m1", "other", "מי רוצה לבוא לסרט היום?"),
        m("m2", "other_2", "אני לא יכולה היום"),
        m("m3", "child", "גם אני אצל סבתא"),
        m("m4", "other", "אז נקבע יום שכולם יכולים"),
    ),
    snapshots=(
        snap("m4", forbidden=(S.EXCLUSION, S.COORDINATED_EXCLUSION)),
    ),
    source="SOC-adapted",
)

TEST_EXCLUSION_PLUS_HUMILIATION = GuardEvalCase(
    case_id="test_exclusion_002_exclusion_plus_humiliation_positive",
    family="exclusion_plus_bullying",
    context={"conversation_type": "group", "child_member": True},
    messages=(
        m("m1", "other", "פותחים קבוצה חדשה לטיול בלי תומר"),
        m("m2", "child", "למה בלי?"),
        m("m3", "other_2", "כי אף אחד לא רוצה לשמוע את הבדיחות שלך"),
        m("m4", "child", "זה פוגע"),
        m("m5", "other", "אז לפחות אל תבוא, יהיה יותר שקט"),
        m("m6", "other_3", "מסכים, אל תוסיפו אותו"),
    ),
    snapshots=(
        snap("m3", signals=(S.EXCLUSION, S.INSULT_OR_HUMILIATION)),
        snap("m5", categories=(C.BULLYING, C.SOCIAL_EXCLUSION), signals=(S.EXCLUSION, S.INSULT_OR_HUMILIATION)),
        snap("m6", signals=(S.COORDINATED_EXCLUSION, S.GROUP_PILE_ON)),
    ),
    source="handwritten, SynBullying-pattern-informed",
)

TEST_ACCIDENTAL_SHARING_REPAIR = GuardEvalCase(
    case_id="test_harmful_sharing_001_accidental_sharing_repair_negative",
    family="accidental_sharing_and_repair",
    context={"conversation_type": "group", "child_member": True},
    messages=(
        m("m1", "other", "אופס שלחתי את התמונה הלא נכונה מהטיול"),
        m("m2", "child", "אפשר למחוק? אני לא אוהבת אותה"),
        m("m3", "other", "כן, מחקתי עכשיו. סליחה"),
        m("m4", "other_2", "לא הספקתי לראות בכלל"),
        m("m5", "child", "תודה"),
    ),
    snapshots=(
        snap("m5", forbidden=(S.HARMFUL_CONTENT_SHARING, S.THREAT_TO_SHARE)),
    ),
    source="SOC-adapted",
)

TEST_SHARING_AS_COERCION = GuardEvalCase(
    case_id="test_harmful_sharing_002_sharing_as_coercion_positive",
    family="sharing_as_coercion",
    context={"conversation_type": "group", "child_member": True},
    messages=(
        m("m1", "other", "יש לי צילום של עומר נרדם באוטובוס"),
        m("m2", "child", "אל תשלח את זה, בבקשה"),
        m("m3", "other", "אז תעשה בשבילי את העבודה למחר"),
        m("m4", "child", "לא, זה לא הוגן"),
        m("m5", "other", "יש לך עד הערב, אחרת זה הולך לכל השכבה"),
    ),
    snapshots=(
        snap("m2", signals=(S.HARMFUL_CONTENT_SHARING,)),
        snap("m3", (GuardDecision.CONCERNING,), categories=(C.HARMFUL_SHARING, C.HARASSMENT_OR_COERCION), signals=(S.COERCIVE_DEMAND,)),
        snap("m5", signals=(S.THREAT_TO_SHARE, S.BLACKMAIL_OR_EXTORTION)),
    ),
    source="handwritten, SynBullying/PAN12-pattern-informed",
)


# ---------------------------------------------------------------------------
# Held-out out-of-scope cases
# ---------------------------------------------------------------------------

TEST_NEWS_DISCUSSION_OOS = GuardEvalCase(
    case_id="test_oos_001_news_discussion",
    family="news_media_or_third_party_discussion",
    context={"conversation_type": "group", "child_member": True, "scope": "oos"},
    messages=(
        m("m1", "other", "ראיתם את הכתבה על החרם בבית הספר ההוא?"),
        m("m2", "child", "כן, ממש עצוב"),
        m("m3", "other_2", "המורה דיברה על זה בכיתה"),
        m("m4", "child", "טוב שמדברים על זה"),
    ),
    snapshots=(snap("m4"),),
    source="SOC-adapted",
)

TEST_RISK_BEHAVIOR_OOS = GuardEvalCase(
    case_id="test_oos_002_risk_behavior_outside_mvp",
    family="risk_behavior_outside_mvp",
    context={"conversation_type": "group", "child_member": True, "scope": "oos"},
    messages=(
        m("m1", "other", "בואו נבריז מהשיעור האחרון ונלך לים"),
        m("m2", "child", "אני לא יודע, יש לי מבחן מחר"),
        m("m3", "other_2", "יאללה פעם אחת"),
        m("m4", "child", "לא, אני נשאר"),
    ),
    snapshots=(snap("m4"),),
    source="SOC-adapted",
    notes="Risk behavior is intentionally outside the six-category MVP taxonomy.",
)

TEST_CONSENSUAL_ROMANCE_OOS = GuardEvalCase(
    case_id="test_oos_003_consensual_age_appropriate_romance",
    family="consensual_age_appropriate_romance",
    context={"conversation_type": "private", "contact_is_known": True, "scope": "oos"},
    messages=(
        m("m1", "other", "היה לי כיף לדבר איתך היום"),
        m("m2", "child", "גם לי :)"),
        m("m3", "other", "רוצה לשבת יחד בהפסקה מחר?"),
        m("m4", "child", "כן, ליד החברים שלנו"),
        m("m5", "other", "סגור"),
    ),
    snapshots=(snap("m5", forbidden=(S.SECRECY_REQUEST, S.COERCIVE_DEMAND)),),
    source="SOC-adapted",
)

TEST_ADULT_LOGISTICS_OOS = GuardEvalCase(
    case_id="test_oos_004_adult_logistics",
    family="adult_logistics_and_services",
    context={"conversation_type": "private", "contact_is_known": True, "contact_relationship": "teacher", "scope": "oos"},
    messages=(
        m("m1", "other", "היי, המבחן נדחה ליום שני"),
        m("m2", "child", "תודה המורה"),
        m("m3", "other", "תעדכן גם את מי שלא בקבוצה בבקשה"),
        m("m4", "child", "אעדכן"),
    ),
    snapshots=(snap("m4"),),
    source="SOC-adapted",
)

TEST_PROFANITY_OOS = GuardEvalCase(
    case_id="test_oos_005_ordinary_profanity",
    family="ordinary_profanity_and_slang",
    context={"conversation_type": "group", "child_member": True, "scope": "oos"},
    messages=(
        m("m1", "other", "איזה מטומטם הייתי ששכחתי את התיק"),
        m("m2", "child", "חחח קורה לכולם"),
        m("m3", "other_2", "מחר אני מזכיר לך"),
        m("m4", "other", "תודה יא מלכים"),
    ),
    snapshots=(snap("m4", forbidden=(S.INSULT_OR_HUMILIATION, S.THREAT)),),
    source="SOC-adapted",
)

TEST_FICTIONAL_VIOLENCE_OOS = GuardEvalCase(
    case_id="test_oos_006_fictional_violence_discussion",
    family="news_media_or_third_party_discussion",
    context={"conversation_type": "group", "child_member": True, "scope": "oos"},
    messages=(
        m("m1", "other", "הפרק החדש היה מלחיץ, הדמות איימה על כולם"),
        m("m2", "child", "כן, אבל ברור שזה רק בסדרה"),
        m("m3", "other_2", "מחר נראה את הפרק הבא יחד?"),
        m("m4", "child", "בטח"),
    ),
    snapshots=(snap("m4", forbidden=(S.THREAT, S.COERCIVE_DEMAND)),),
    source="SOC-adapted",
)


# The previous 12 cases are the mutable development corpus.  The cases below
# must remain held out from prompt examples and prompt-informed edits.
DEV_CASES: tuple[GuardEvalCase, ...] = GUARD_MVP_CASES

TEST_CASES: tuple[GuardEvalCase, ...] = (
    TEST_ROMANCE_TO_GROOMING,
    TEST_UNKNOWN_BENIGN_SERVICE,
    TEST_CREDIBLE_THREAT,
    TEST_GAMING_HYPERBOLE,
    TEST_PRESSURE_WITHOUT_THREAT,
    TEST_IMMEDIATE_SELF_HARM_RISK,
    TEST_DISTRESSED_FRIEND_NOT_CHILD,
    TEST_SINGLE_CONFLICT,
    TEST_GROUP_PILE_ON,
    TEST_CHILD_IS_AGGRESSOR,
    TEST_AMBIGUOUS_INVITATION,
    TEST_EXCLUSION_PLUS_HUMILIATION,
    TEST_ACCIDENTAL_SHARING_REPAIR,
    TEST_SHARING_AS_COERCION,
    TEST_NEWS_DISCUSSION_OOS,
    TEST_RISK_BEHAVIOR_OOS,
    TEST_CONSENSUAL_ROMANCE_OOS,
    TEST_ADULT_LOGISTICS_OOS,
    TEST_PROFANITY_OOS,
    TEST_FICTIONAL_VIOLENCE_OOS,
)

ALL_CASES: tuple[GuardEvalCase, ...] = DEV_CASES + TEST_CASES
