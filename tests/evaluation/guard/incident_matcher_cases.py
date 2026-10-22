"""Gold cases for the Incident Matcher evaluation.

Each case is a sequence of assessments with expected match decisions.
The eval harness replays assessments sequentially, maintaining an in-memory
list of incident candidates that accumulates evidence and severity after
every match.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from echo_v2.services.incident_matcher import IncidentMatchDecision


@dataclass(frozen=True)
class MatcherAssessmentSpec:
    """A single assessment in a matcher eval case."""

    assessment_id: str
    summary: str
    reason: str
    evidence_message_ids: tuple[str, ...]
    created_at: datetime
    expected_decision: IncidentMatchDecision
    expected_incident_id: str | None = None  # None for NEW, set for SAME/ESCALATION
    alert_type: str | None = None  # override case-level alert_type per assessment


@dataclass(frozen=True)
class IncidentMatcherGoldCase:
    case_id: str
    title: str
    alert_type: str  # "security" | "exposure" — default for all assessments
    assessments: tuple[MatcherAssessmentSpec, ...]


_T0 = datetime(2026, 10, 22, 7, 0, tzinfo=timezone.utc)


def _t(minutes: int) -> datetime:
    return _T0.replace(minute=0, second=0) + _td(minutes)


def _td(minutes: int) -> datetime:
    from datetime import timedelta

    return timedelta(minutes=minutes)


CASES: tuple[IncidentMatcherGoldCase, ...] = (
    # ------------------------------------------------------------------
    # Case 1: Mikush — 8 real assessments, same pain/breathing incident
    # ------------------------------------------------------------------
    IncidentMatcherGoldCase(
        case_id="mikush_pain_breathing",
        title="Child reports ongoing pain and breathing difficulty — same incident across 8 assessments",
        alert_type="security",
        assessments=(
            MatcherAssessmentSpec(
                assessment_id="mikush-a1",
                summary="מיקוש דיווחה על כאב חזק בצד ימין שמתגבר בנשימה. זהו סימן גופני משמעותי שמצדיק עדכון ותשומת לב של הורה.",
                reason="Strong right-side pain worsening with breathing.",
                evidence_message_ids=("efec6d14", "8f3dba0a"),
                created_at=_t(9),
                expected_decision=IncidentMatchDecision.NEW_INCIDENT,
            ),
            MatcherAssessmentSpec(
                assessment_id="mikush-a2",
                summary="מיקוש מדווחת על כאב משמעותי בצד ימין שמחמיר בנשימה. למרות שהיא מצליחה לנשום, הכאב נמשך ולכן חשוב שהורה יהיה מודע לכך ויוודא שהיא מקבלת הערכה וטיפול מתאימים.",
                reason="Same pain, child can still breathe but pain persists.",
                evidence_message_ids=("efec6d14", "8f3dba0a", "e1037919"),
                created_at=_t(15),
                expected_decision=IncidentMatchDecision.SAME_INCIDENT,
                expected_incident_id="mikush-a1",
            ),
            MatcherAssessmentSpec(
                assessment_id="mikush-a3",
                summary="מיקוש מדווחת על כאב משמעותי בצד ימין שמחמיר בנשימה ובתנועה, ואף מתארת כאב מטורף לאחר מאמץ. מדובר בתסמין גופני משמעותי שמצדיק תשומת לב הורית ומעקב מתאים.",
                reason="Same pain, now also with movement and after exertion.",
                evidence_message_ids=("efec6d14", "e1037919", "ccfd85eb"),
                created_at=_t(29),
                expected_decision=IncidentMatchDecision.SAME_INCIDENT,
                expected_incident_id="mikush-a1",
            ),
            MatcherAssessmentSpec(
                assessment_id="mikush-a4",
                summary="מיקוש מתארת כאב חזק בצד ימין שמתגבר בנשימה ובתנועה, ואף צופה כאב מטורף לאחר מאמץ. מדובר בתסמין גופני משמעותי שמצדיק תשומת לב הורית ובדיקה מתאימה.",
                reason="Same pain, same severity.",
                evidence_message_ids=("efec6d14", "8f3dba0a", "e1037919", "ccfd85eb"),
                created_at=_t(35),
                expected_decision=IncidentMatchDecision.SAME_INCIDENT,
                expected_incident_id="mikush-a1",
            ),
            MatcherAssessmentSpec(
                assessment_id="mikush-a5",
                summary="מיקוש מדווחת על כאבים חזקים בצד ימין שמתגברים בנשימה ובתנועה, וממשיכה להרגיש כאב משמעותי במהלך היום. למרות שהיא מצליחה לנשום, מדובר בתסמין גופני שמצדיק עדכון הורה והערכה מתאימה.",
                reason="Same pain continuing through the day.",
                evidence_message_ids=("efec6d14", "e1037919", "0ad92584", "8791b8cb"),
                created_at=_t(114),
                expected_decision=IncidentMatchDecision.SAME_INCIDENT,
                expected_incident_id="mikush-a1",
            ),
            MatcherAssessmentSpec(
                assessment_id="mikush-a6",
                summary="מיקוש מדווחת על כאבים חזקים בצד ימין שמתגברים בנשימה ובתנועה, ומתארת שהכאב לעיתים מטורף. למרות זאת היא נשארת בבית הספר; נדרשת תשומת לב הורית ומעקב מתאים.",
                reason="Same pain, child stays at school.",
                evidence_message_ids=("efec6d14", "e1037919", "0ad92584", "ccfd85eb"),
                created_at=_t(133),
                expected_decision=IncidentMatchDecision.SAME_INCIDENT,
                expected_incident_id="mikush-a1",
            ),
            MatcherAssessmentSpec(
                assessment_id="mikush-a7",
                summary="מיקוש מתארת כאבים חזקים בצד ימין שמתגברים בנשימה ולעיתים לאחר תנועה, וממשיכה להיות במסגרת למרות שאינה יודעת אם הכאב מצדיק חזרה הביתה. מדובר בתסמין גופני משמעותי שמצדיק עדכון ומעקב הורי.",
                reason="Same pain, child unsure if she should go home.",
                evidence_message_ids=("efec6d14", "e1037919", "0ad92584", "8791b8cb", "06a45b02"),
                created_at=_t(549),
                expected_decision=IncidentMatchDecision.SAME_INCIDENT,
                expected_incident_id="mikush-a1",
            ),
            MatcherAssessmentSpec(
                assessment_id="mikush-a8",
                summary="מיקוש מדווחת על כאבים חזקים בצד ימין שמתגברים בנשימה ובתנועה, ואף אומרת שכואב לה מאוד. מדובר בתסמין גופני משמעותי שמצריך תשומת לב הורית ומעקב, גם אם ייתכן שמדובר בכאב שרירי.",
                reason="Same pain, child says it hurts a lot.",
                evidence_message_ids=("efec6d14", "e1037919", "0ad92584", "ccfd85eb"),
                created_at=_t(556),
                expected_decision=IncidentMatchDecision.SAME_INCIDENT,
                expected_incident_id="mikush-a1",
            ),
        ),
    ),
    # ------------------------------------------------------------------
    # Case 2: Recurring bullying — same issue, different evidence
    # ------------------------------------------------------------------
    IncidentMatcherGoldCase(
        case_id="recurring_bullying",
        title="Bullying over multiple messages — same ongoing issue",
        alert_type="security",
        assessments=(
            MatcherAssessmentSpec(
                assessment_id="bully-b1",
                summary="Shay is being mocked and called useless by classmates in the class group.",
                reason="Group targeting of the child.",
                evidence_message_ids=("msg-001", "msg-002"),
                created_at=_t(0),
                expected_decision=IncidentMatchDecision.NEW_INCIDENT,
            ),
            MatcherAssessmentSpec(
                assessment_id="bully-b2",
                summary="More classmates continue mocking Shay in the same group.",
                reason="Continuation of group bullying.",
                evidence_message_ids=("msg-001", "msg-002", "msg-003"),
                created_at=_t(30),
                expected_decision=IncidentMatchDecision.SAME_INCIDENT,
                expected_incident_id="bully-b1",
            ),
            MatcherAssessmentSpec(
                assessment_id="bully-b3",
                summary="Different kids join the bullying against Shay in the same group.",
                reason="Same bullying pattern, more participants.",
                evidence_message_ids=("msg-004", "msg-005"),
                created_at=_t(60),
                expected_decision=IncidentMatchDecision.SAME_INCIDENT,
                expected_incident_id="bully-b1",
            ),
        ),
    ),
    # ------------------------------------------------------------------
    # Case 3: Zero-overlap same incident — semantic matching, no ID overlap
    # ------------------------------------------------------------------
    IncidentMatcherGoldCase(
        case_id="zero_overlap_same_incident",
        title="Same ongoing bullying with completely different evidence IDs",
        alert_type="security",
        assessments=(
            MatcherAssessmentSpec(
                assessment_id="zero-g1",
                summary="Shay being called useless and mocked by classmates.",
                reason="Group bullying targeting the child.",
                evidence_message_ids=("msg-101", "msg-102"),
                created_at=_t(0),
                expected_decision=IncidentMatchDecision.NEW_INCIDENT,
            ),
            MatcherAssessmentSpec(
                assessment_id="zero-g2",
                summary="Shay being humiliated and excluded by the same group of classmates.",
                reason="Same ongoing bullying, different messages.",
                evidence_message_ids=("msg-201", "msg-202", "msg-203"),
                created_at=_t(45),
                expected_decision=IncidentMatchDecision.SAME_INCIDENT,
                expected_incident_id="zero-g1",
            ),
            MatcherAssessmentSpec(
                assessment_id="zero-g3",
                summary="Classmates continue targeting Shay with insults and exclusion.",
                reason="Same bullying continues.",
                evidence_message_ids=("msg-301",),
                created_at=_t(90),
                expected_decision=IncidentMatchDecision.SAME_INCIDENT,
                expected_incident_id="zero-g1",
            ),
        ),
    ),
    # ------------------------------------------------------------------
    # Case 4: Distinct simultaneous risks — two unrelated issues
    # ------------------------------------------------------------------
    IncidentMatcherGoldCase(
        case_id="distinct_simultaneous_risks",
        title="Two unrelated issues in the same chat at the same time",
        alert_type="security",
        assessments=(
            MatcherAssessmentSpec(
                assessment_id="distinct-c1",
                summary="Shay is being cyberbullied with repeated insults about his appearance.",
                reason="Ongoing verbal bullying.",
                evidence_message_ids=("msg-401", "msg-402"),
                created_at=_t(0),
                expected_decision=IncidentMatchDecision.NEW_INCIDENT,
            ),
            MatcherAssessmentSpec(
                assessment_id="distinct-c2",
                summary="Someone threatens to share a private embarrassing photo of Shay.",
                reason="Blackmail with private material — distinct from bullying.",
                evidence_message_ids=("msg-410",),
                created_at=_t(15),
                expected_decision=IncidentMatchDecision.NEW_INCIDENT,
            ),
        ),
    ),
    # ------------------------------------------------------------------
    # Case 5: Escalation — verbal teasing → credible physical threat
    # ------------------------------------------------------------------
    IncidentMatcherGoldCase(
        case_id="escalation_threat",
        title="Verbal teasing escalates to credible physical threat",
        alert_type="security",
        assessments=(
            MatcherAssessmentSpec(
                assessment_id="esc-d1",
                summary="Classmates are mocking Shay and calling him names in the group.",
                reason="Verbal bullying.",
                evidence_message_ids=("msg-501", "msg-502"),
                created_at=_t(0),
                expected_decision=IncidentMatchDecision.NEW_INCIDENT,
            ),
            MatcherAssessmentSpec(
                assessment_id="esc-d2",
                summary="A classmate threatens to wait outside Shay's school with a knife.",
                reason="Credible physical threat — material escalation of the same bullying.",
                evidence_message_ids=("msg-501", "msg-502", "msg-510"),
                created_at=_t(30),
                expected_decision=IncidentMatchDecision.SIGNIFICANT_ESCALATION,
                expected_incident_id="esc-d1",
            ),
        ),
    ),
    # ------------------------------------------------------------------
    # Case 6: Exposure group conflict — same ongoing group-level issue
    # ------------------------------------------------------------------
    IncidentMatcherGoldCase(
        case_id="exposure_group_conflict",
        title="Ongoing group-level harmful content across multiple assessments",
        alert_type="exposure",
        assessments=(
            MatcherAssessmentSpec(
                assessment_id="expo-e1",
                summary="Group chat filled with violent content and threats directed at each other.",
                reason="Significant group-level exposure to violence.",
                evidence_message_ids=("msg-601", "msg-602"),
                created_at=_t(0),
                expected_decision=IncidentMatchDecision.NEW_INCIDENT,
            ),
            MatcherAssessmentSpec(
                assessment_id="expo-e2",
                summary="More violent messages in the same group conflict.",
                reason="Same ongoing group conflict.",
                evidence_message_ids=("msg-601", "msg-602", "msg-603"),
                created_at=_t(20),
                expected_decision=IncidentMatchDecision.SAME_INCIDENT,
                expected_incident_id="expo-e1",
            ),
            MatcherAssessmentSpec(
                assessment_id="expo-e3",
                summary="Different participants continue the same violent group conflict.",
                reason="Same conflict, different participants.",
                evidence_message_ids=("msg-610", "msg-611"),
                created_at=_t(40),
                expected_decision=IncidentMatchDecision.SAME_INCIDENT,
                expected_incident_id="expo-e1",
            ),
        ),
    ),
    # ------------------------------------------------------------------
    # Case 7: Closed incident recurrence — matcher says SAME (lifecycle
    # is the service's job)
    # ------------------------------------------------------------------
    IncidentMatcherGoldCase(
        case_id="closed_incident_recurrence",
        title="Closed incident's same issue recurs — matcher recognizes same issue",
        alert_type="security",
        assessments=(
            MatcherAssessmentSpec(
                assessment_id="closed-h1",
                summary="Shay being bullied about his appearance by classmates.",
                reason="Appearance-based bullying.",
                evidence_message_ids=("msg-701", "msg-702"),
                created_at=_t(0),
                expected_decision=IncidentMatchDecision.NEW_INCIDENT,
            ),
            MatcherAssessmentSpec(
                assessment_id="closed-h2",
                summary="Shay being mocked about his appearance again after a break.",
                reason="Same appearance-based bullying resumed.",
                evidence_message_ids=("msg-801",),
                created_at=_t(4320),  # 3 days later
                expected_decision=IncidentMatchDecision.SAME_INCIDENT,
                expected_incident_id="closed-h1",
            ),
        ),
    ),
    # ------------------------------------------------------------------
    # Case 8: Cross alert-type independence — security + exposure don't
    # suppress each other
    # ------------------------------------------------------------------
    IncidentMatcherGoldCase(
        case_id="cross_alert_type_independence",
        title="Security and exposure in the same chat must not suppress each other",
        alert_type="security",  # default, overridden per-assessment
        assessments=(
            MatcherAssessmentSpec(
                assessment_id="cross-f1",
                summary="Shay is being individually targeted with threats and insults.",
                reason="Individual targeting of the child.",
                evidence_message_ids=("msg-901",),
                created_at=_t(0),
                expected_decision=IncidentMatchDecision.NEW_INCIDENT,
                alert_type="security",
            ),
            MatcherAssessmentSpec(
                assessment_id="cross-f2",
                summary="The group chat contains widespread toxic and violent content.",
                reason="Group-level exposure to harmful content.",
                evidence_message_ids=("msg-902",),
                created_at=_t(5),
                expected_decision=IncidentMatchDecision.NEW_INCIDENT,
                alert_type="exposure",
            ),
        ),
    ),
)


def validate_cases(cases: tuple[IncidentMatcherGoldCase, ...] = CASES) -> None:
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("Incident matcher case IDs must be unique")
    for case in cases:
        assessment_ids = [spec.assessment_id for spec in case.assessments]
        if len(assessment_ids) != len(set(assessment_ids)):
            raise ValueError(f"{case.case_id}: assessment IDs must be unique")


__all__ = [
    "CASES",
    "IncidentMatcherGoldCase",
    "MatcherAssessmentSpec",
    "validate_cases",
]
