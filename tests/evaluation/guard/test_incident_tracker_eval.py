"""End-to-end evaluation for the Incident Tracker (Layer 2/3).

Replays Mikush's 8 real YES assessments through the IncidentTracker with a real
LLM matcher. Asserts: 1 incident, 1 scheduled alert per guardian, 7 suppressed.
Then replays a distinct new issue and asserts a 2nd incident + alert.

Requires ``OPENAI_API_KEY`` and calls the real LLM.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import pytest

from echo_v2.domain.guard_simple import ChildRole, SafetyAssessment
from echo_v2.domain.scheduling import (
    ScheduledAction,
    ScheduledActionStatus,
    ScheduledActionType,
)
from echo_v2.persistence.alerted_incident_repository import (
    InMemoryAlertedIncidentRepository,
)
from echo_v2.persistence.guard_repositories import InMemoryGuardianChildLinkRepository
from echo_v2.services.incident_matcher import IncidentMatcher
from echo_v2.services.incident_tracker import IncidentTracker

pytestmark = [
    pytest.mark.eval_guard,
    pytest.mark.skipif(
        not os.environ.get("OPENAI_API_KEY"),
        reason="OPENAI_API_KEY not set — eval requires real API access",
    ),
]

_T0 = datetime(2026, 10, 22, 7, 0, tzinfo=timezone.utc)


class FakeUsers:
    async def get_phone_by_id(self, user_id: str) -> str | None:
        return f"+9725000{user_id[-6:]}"


class CapturingScheduling:
    """Captures scheduled actions without sending."""

    def __init__(self) -> None:
        self.created: list[ScheduledAction] = []

    async def create_once(
        self,
        *,
        request_id: str,
        user_id: str,
        type: ScheduledActionType,
        execute_at_utc: datetime,
        timezone_name: str,
        payload: dict[str, object],
    ) -> tuple[ScheduledAction, bool]:
        action = ScheduledAction(
            id=request_id,
            user_id=user_id,
            type=type,
            execute_at_utc=execute_at_utc,
            timezone=timezone_name,
            status=ScheduledActionStatus.PENDING,
            payload=payload,
        )
        self.created.append(action)
        return action, True


# Mikush's 8 real assessment summaries (from production data)
MIKUSH_ASSESSMENTS: list[dict[str, Any]] = [
    {
        "assessment_id": "mikush-a1",
        "summary": "מיקוש דיווחה על כאב חזק בצד ימין שמתגבר בנשימה. זהו סימן גופני משמעותי שמצדיק עדכון ותשומת לב של הורה.",
        "reason": "Strong right-side pain worsening with breathing.",
        "evidence": ("efec6d14", "8f3dba0a"),
    },
    {
        "assessment_id": "mikush-a2",
        "summary": "מיקוש מדווחת על כאב משמעותי בצד ימין שמחמיר בנשימה. למרות שהיא מצליחה לנשום, הכאב נמשך ולכן חשוב שהורה יהיה מודע לכך ויוודא שהיא מקבלת הערכה וטיפול מתאימים.",
        "reason": "Same pain, child can still breathe but pain persists.",
        "evidence": ("efec6d14", "8f3dba0a", "e1037919"),
    },
    {
        "assessment_id": "mikush-a3",
        "summary": "מיקוש מדווחת על כאב משמעותי בצד ימין שמחמיר בנשימה ובתנועה, ואף מתארת כאב מטורף לאחר מאמץ. מדובר בתסמין גופני משמעותי שמצדיק תשומת לב הורית ומעקב מתאים.",
        "reason": "Same pain, now also with movement and after exertion.",
        "evidence": ("efec6d14", "e1037919", "ccfd85eb"),
    },
    {
        "assessment_id": "mikush-a4",
        "summary": "מיקוש מתארת כאב חזק בצד ימין שמתגבר בנשימה ובתנועה, ואף צופה כאב מטורף לאחר מאמץ. מדובר בתסמין גופני משמעותי שמצדיק תשומת לב הורית ובדיקה מתאימה.",
        "reason": "Same pain, same severity.",
        "evidence": ("efec6d14", "8f3dba0a", "e1037919", "ccfd85eb"),
    },
    {
        "assessment_id": "mikush-a5",
        "summary": "מיקוש מדווחת על כאבים חזקים בצד ימין שמתגברים בנשימה ובתנועה, וממשיכה להרגיש כאב משמעותי במהלך היום. למרות שהיא מצליחה לנשום, מדובר בתסמין גופני שמצדיק עדכון הורה והערכה מתאימה.",
        "reason": "Same pain continuing through the day.",
        "evidence": ("efec6d14", "e1037919", "0ad92584", "8791b8cb"),
    },
    {
        "assessment_id": "mikush-a6",
        "summary": "מיקוש מדווחת על כאבים חזקים בצד ימין שמתגברים בנשימה ובתנועה, ומתארת שהכאב לעיתים מטורף. למרות זאת היא נשארת בבית הספר; נדרשת תשומת לב הורית ומעקב מתאים.",
        "reason": "Same pain, child stays at school.",
        "evidence": ("efec6d14", "e1037919", "0ad92584", "ccfd85eb"),
    },
    {
        "assessment_id": "mikush-a7",
        "summary": "מיקוש מתארת כאבים חזקים בצד ימין שמתגברים בנשימה ולעיתים לאחר תנועה, וממשיכה להיות במסגרת למרות שאינה יודעת אם הכאב מצדיק חזרה הביתה. מדובר בתסמין גופני משמעותי שמצדיק עדכון ומעקב הורי.",
        "reason": "Same pain, child unsure if she should go home.",
        "evidence": ("efec6d14", "e1037919", "0ad92584", "8791b8cb", "06a45b02"),
    },
    {
        "assessment_id": "mikush-a8",
        "summary": "מיקוש מדווחת על כאבים חזקים בצד ימין שמתגברים בנשימה ובתנועה, ואף אומרת שכואב לה מאוד. מדובר בתסמין גופני משמעותי שמצריך תשומת לב הורית ומעקב, גם אם ייתכן שמדובר בכאב שרירי.",
        "reason": "Same pain, child says it hurts a lot.",
        "evidence": ("efec6d14", "e1037919", "0ad92584", "ccfd85eb"),
    },
]


def _make_safety(spec: dict[str, Any]) -> SafetyAssessment:
    return SafetyAssessment(
        parent_should_know=True,
        child_role=ChildRole.TARGET,
        summary=spec["summary"],
        evidence_message_ids=spec["evidence"],
        reason=spec["reason"],
        child_user_id="child-mikush",
        child_name="Mikush",
        chat_id="chat-mikush",
        created_at=_T0,
    )


@pytest.fixture
async def tracker() -> IncidentTracker:
    from openai import AsyncOpenAI

    matcher = IncidentMatcher(
        client=AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", "")),
        model=os.environ.get(
            "INCIDENT_MATCHER_LLM_MODEL",
            os.environ.get("GUARD_LLM_MODEL_NAME", "gpt-4.1"),
        ),
    )
    incidents = InMemoryAlertedIncidentRepository()
    links = InMemoryGuardianChildLinkRepository()
    scheduling = CapturingScheduling()

    await links.upsert_active(
        guardian_user_id="guardian-001",
        child_user_id="child-mikush",
        now=_T0,
    )

    return IncidentTracker(
        matcher=matcher,
        incidents=incidents,
        links=links,
        users=FakeUsers(),
        scheduling=scheduling,
        enabled=True,
    )


async def test_mikush_eight_assessments_one_incident_one_alert(tracker: IncidentTracker) -> None:
    """Mikush's 8 YES assessments → 1 incident, 1 alert per guardian, 7 suppressed."""
    results: list[str] = []
    for spec in MIKUSH_ASSESSMENTS:
        result = await tracker.evaluate_security(
            assessment=_make_safety(spec),
            assessment_id=spec["assessment_id"],
        )
        results.append(result)
        print(f"{spec['assessment_id']}: {result}")

    # First assessment should schedule (NEW)
    assert results[0] == "scheduled", f"First assessment should be NEW, got {results[0]}"

    # Remaining 7 should be suppressed (SAME)
    for i, result in enumerate(results[1:], 1):
        assert result == "suppressed", (
            f"Assessment {i+1} should be suppressed (SAME), got {result}"
        )

    # Exactly 1 scheduled alert (for 1 guardian)
    scheduling = tracker._scheduling  # type: ignore[attr-defined]
    assert len(scheduling.created) == 1, (
        f"Expected 1 scheduled alert, got {len(scheduling.created)}"
    )

    # Exactly 1 incident
    incidents = tracker._incidents  # type: ignore[attr-defined]
    active = await incidents.list_active("child-mikush", "chat-mikush", "security")
    assert len(active) == 1, f"Expected 1 incident, got {len(active)}"

    # All 8 assessment IDs recorded
    assert len(active[0].assessment_ids) == 8, (
        f"Expected 8 assessment IDs, got {len(active[0].assessment_ids)}"
    )

    # All unique evidence IDs accumulated
    expected_evidence = {
        "efec6d14", "8f3dba0a", "e1037919", "ccfd85eb",
        "0ad92584", "8791b8cb", "06a45b02",
    }
    assert set(active[0].evidence_message_ids) == expected_evidence


async def test_distinct_new_issue_produces_second_alert(tracker: IncidentTracker) -> None:
    """After Mikush, a distinct new issue → 2nd incident + 2nd alert."""
    # First: replay all 8 Mikush assessments
    for spec in MIKUSH_ASSESSMENTS:
        result = await tracker.evaluate_security(
            assessment=_make_safety(spec),
            assessment_id=spec["assessment_id"],
        )
        print(f"{spec['assessment_id']}: {result}")

    scheduling = tracker._scheduling  # type: ignore[attr-defined]
    assert len(scheduling.created) == 1, (
        f"Expected 1 scheduled alert after Mikush, got {len(scheduling.created)}"
    )

    # Now a completely different issue: blackmail
    blackmail = SafetyAssessment(
        parent_should_know=True,
        child_role=ChildRole.TARGET,
        summary="ילדה אחרת מאיימת על מיקוש שתחשוף תמונות אינטימיות אם לא תשלם כסף. מדובר בסחיטה באיומים חמורה.",
        reason="Blackmail with threat of exposing intimate photos.",
        evidence_message_ids=("blackmail-1", "blackmail-2"),
        child_user_id="child-mikush",
        child_name="Mikush",
        chat_id="chat-mikush",
        created_at=_T0,
    )

    result = await tracker.evaluate_security(
        assessment=blackmail,
        assessment_id="blackmail-001",
    )

    assert result == "scheduled", f"Distinct issue should be NEW, got {result}"
    assert len(scheduling.created) == 2, (
        f"Expected 2 scheduled alerts, got {len(scheduling.created)}"
    )

    incidents = tracker._incidents  # type: ignore[attr-defined]
    active = await incidents.list_active("child-mikush", "chat-mikush", "security")
    assert len(active) == 2, f"Expected 2 incidents, got {len(active)}"
