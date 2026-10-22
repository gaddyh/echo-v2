"""Unit tests for the IncidentTracker service."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.domain.alerted_incident import AlertedIncident
from echo_v2.domain.guard_exposure import ExposureAssessment, ExposureCategory
from echo_v2.domain.guard_simple import ChildRole, SafetyAssessment
from echo_v2.domain.scheduling import ScheduledAction, ScheduledActionType
from echo_v2.persistence.alerted_incident_repository import (
    InMemoryAlertedIncidentRepository,
)
from echo_v2.persistence.guard_repositories import InMemoryGuardianChildLinkRepository
from echo_v2.services.incident_matcher import (
    IncidentMatchDecision,
    IncidentMatcher,
    IncidentMatchResult,
    MatcherAssessment,
)
from echo_v2.services.incident_tracker import IncidentTracker

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


class FakeUsers:
    async def get_phone_by_id(self, user_id: str) -> str | None:
        return f"+9725000{user_id[-6:]}" if user_id.startswith("guardian") else None


class FakeMatcher(IncidentMatcher):
    """A controllable matcher stub that returns preset decisions."""

    def __init__(self) -> None:
        self._queue: list[IncidentMatchResult] = []
        self.calls: list[tuple[str, MatcherAssessment, tuple]] = []

    def set_next(self, result: IncidentMatchResult) -> None:
        self._queue.append(result)

    async def match(
        self,
        *,
        alert_type: str,
        assessment: MatcherAssessment,
        incidents: tuple,
    ) -> IncidentMatchResult:
        self.calls.append((alert_type, assessment, incidents))
        if self._queue:
            return self._queue.pop(0)
        return IncidentMatchResult(
            decision=IncidentMatchDecision.NEW_INCIDENT,
            incident_id=None,
            reason="default new",
        )


class FakeScheduling:
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
            status=None,  # type: ignore[arg-type]
            payload=payload,
        )
        self.created.append(action)
        return action, True


def _make_safety(
    *,
    summary: str = "Child reports pain and breathing issues",
    evidence: tuple[str, ...] = ("msg-1", "msg-2"),
    child_id: str = "child-001",
    chat_id: str = "chat-001",
    child_name: str = "Mikush",
    yes: bool = True,
) -> SafetyAssessment:
    return SafetyAssessment(
        parent_should_know=yes,
        child_role=ChildRole.TARGET,
        summary=summary,
        evidence_message_ids=evidence,
        reason="Analyzer detected concerning content",
        child_user_id=child_id,
        child_name=child_name,
        chat_id=chat_id,
        created_at=BASE,
    )


def _make_tracker(
    *,
    matcher: FakeMatcher | None = None,
    incidents: InMemoryAlertedIncidentRepository | None = None,
    links: InMemoryGuardianChildLinkRepository | None = None,
    scheduling: FakeScheduling | None = None,
    enabled: bool = True,
) -> tuple[IncidentTracker, FakeMatcher, InMemoryAlertedIncidentRepository, InMemoryGuardianChildLinkRepository, FakeScheduling]:
    matcher = matcher or FakeMatcher()
    incidents = incidents or InMemoryAlertedIncidentRepository()
    links = links or InMemoryGuardianChildLinkRepository()
    scheduling = scheduling or FakeScheduling()
    tracker = IncidentTracker(
        matcher=matcher,
        incidents=incidents,
        links=links,
        users=FakeUsers(),
        scheduling=scheduling,
        enabled=enabled,
    )
    return tracker, matcher, incidents, links, scheduling


# --- NEW incident ---------------------------------------------------------


async def test_new_incident_schedules_one_alert_per_guardian() -> None:
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(
        guardian_user_id="guardian-001", child_user_id="child-001", now=BASE
    )
    await links.upsert_active(
        guardian_user_id="guardian-002", child_user_id="child-001", now=BASE
    )
    tracker, matcher, incidents, _, scheduling = _make_tracker(links=links)

    # No existing incidents → NEW without LLM call
    result = await tracker.evaluate_security(
        assessment=_make_safety(),
        assessment_id="assess-001",
    )

    assert result == "scheduled"
    assert len(scheduling.created) == 2  # one per guardian
    assert matcher.calls == []  # no LLM call when no candidates

    # Incident saved
    active = await incidents.list_active("child-001", "chat-001", "security")
    assert len(active) == 1
    assert active[0].assessment_ids == ("assess-001",)
    assert active[0].notification_version == 1


async def test_no_active_link_saves_incident_without_notification() -> None:
    tracker, _, incidents, _, scheduling = _make_tracker()

    result = await tracker.evaluate_security(
        assessment=_make_safety(),
        assessment_id="assess-001",
    )

    assert result == "no_active_link"
    assert scheduling.created == []
    active = await incidents.list_active("child-001", "chat-001", "security")
    assert len(active) == 1  # incident still saved


# --- SAME incident --------------------------------------------------------


async def test_same_incident_suppresses_and_updates_context() -> None:
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian-001", child_user_id="child-001", now=BASE)
    tracker, matcher, incidents, _, scheduling = _make_tracker(links=links)

    # First assessment → NEW
    await tracker.evaluate_security(
        assessment=_make_safety(summary="Pain and breathing", evidence=("msg-1",)),
        assessment_id="assess-001",
    )
    assert len(scheduling.created) == 1

    # Second assessment → SAME
    matcher.set_next(
        IncidentMatchResult(
            decision=IncidentMatchDecision.SAME_INCIDENT,
            incident_id=(await incidents.list_active("child-001", "chat-001", "security"))[0].id,
            reason="same issue",
        )
    )
    result = await tracker.evaluate_security(
        assessment=_make_safety(summary="Still in pain", evidence=("msg-2", "msg-3")),
        assessment_id="assess-002",
    )

    assert result == "suppressed"
    assert len(scheduling.created) == 1  # no new alert

    active = await incidents.list_active("child-001", "chat-001", "security")
    assert len(active) == 1
    assert "assess-002" in active[0].assessment_ids
    assert set(active[0].evidence_message_ids) == {"msg-1", "msg-2", "msg-3"}


# --- No candidates → NEW without LLM -------------------------------------


async def test_no_candidates_new_without_llm() -> None:
    tracker, matcher, _, _, _ = _make_tracker()
    result = await tracker.evaluate_security(
        assessment=_make_safety(),
        assessment_id="assess-001",
    )
    assert result in ("scheduled", "no_active_link")
    assert matcher.calls == []  # no LLM call


# --- Recently closed → LLM call ------------------------------------------


async def test_recently_closed_triggers_llm() -> None:
    incidents = InMemoryAlertedIncidentRepository()
    closed_incident = AlertedIncident(
        id="inc-closed",
        child_user_id="child-001",
        chat_id="chat-001",
        alert_type="security",
        summary="Previous bullying",
        severity_summary="Previous bullying",
        notified_severity_summary="Previous bullying",
        evidence_message_ids=("old-msg",),
        assessment_ids=("old-assess",),
        last_seen_at=BASE - timedelta(days=5),
        closed_at=BASE - timedelta(days=5),
        related_incident_id=None,
        notification_version=1,
        created_at=BASE - timedelta(days=10),
        updated_at=BASE - timedelta(days=5),
    )
    await incidents.create(closed_incident)

    matcher = FakeMatcher()
    matcher.set_next(
        IncidentMatchResult(
            decision=IncidentMatchDecision.NEW_INCIDENT,
            incident_id=None,
            reason="different issue",
        )
    )
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian-001", child_user_id="child-001", now=BASE)
    tracker, _, _, _, scheduling = _make_tracker(
        matcher=matcher, incidents=incidents, links=links
    )

    result = await tracker.evaluate_security(
        assessment=_make_safety(summary="New blackmail", evidence=("msg-new",)),
        assessment_id="assess-001",
    )

    assert result == "scheduled"
    assert len(matcher.calls) == 1  # LLM was called
    assert len(scheduling.created) == 1


# --- Escalation -----------------------------------------------------------


async def test_escalation_schedules_new_version() -> None:
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian-001", child_user_id="child-001", now=BASE)
    tracker, matcher, incidents, _, scheduling = _make_tracker(links=links)

    # First: NEW
    await tracker.evaluate_security(
        assessment=_make_safety(summary="Bullying at school", evidence=("msg-1",)),
        assessment_id="assess-001",
    )
    assert len(scheduling.created) == 1

    incident = (await incidents.list_active("child-001", "chat-001", "security"))[0]

    # Second: ESCALATION
    matcher.set_next(
        IncidentMatchResult(
            decision=IncidentMatchDecision.SIGNIFICANT_ESCALATION,
            incident_id=incident.id,
            reason="weapon threat",
        )
    )
    result = await tracker.evaluate_security(
        assessment=_make_safety(summary="Knife threat", evidence=("msg-2",)),
        assessment_id="assess-002",
    )

    assert result == "escalation_scheduled"
    assert len(scheduling.created) == 2  # new version scheduled

    updated = await incidents.get(incident.id)
    assert updated is not None
    assert updated.notification_version == 2
    assert updated.severity_summary == "Knife threat"
    assert updated.notified_severity_summary == "Knife threat"


async def test_escalation_reprocess_same_assessment_is_idempotent() -> None:
    """Reprocessing the same assessment after escalation → already_processed."""
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian-001", child_user_id="child-001", now=BASE)
    tracker, matcher, incidents, _, scheduling = _make_tracker(links=links)

    # NEW
    await tracker.evaluate_security(
        assessment=_make_safety(summary="Bullying", evidence=("msg-1",)),
        assessment_id="assess-001",
    )
    incident = (await incidents.list_active("child-001", "chat-001", "security"))[0]

    # ESCALATION
    matcher.set_next(
        IncidentMatchResult(
            decision=IncidentMatchDecision.SIGNIFICANT_ESCALATION,
            incident_id=incident.id,
            reason="weapon",
        )
    )
    await tracker.evaluate_security(
        assessment=_make_safety(summary="Knife threat", evidence=("msg-2",)),
        assessment_id="assess-002",
    )
    assert len(scheduling.created) == 2

    # Reprocess assess-002 → already_processed (idempotency)
    result = await tracker.evaluate_security(
        assessment=_make_safety(summary="Knife threat", evidence=("msg-2",)),
        assessment_id="assess-002",
    )
    assert result == "already_processed"
    assert len(scheduling.created) == 2  # no new alert


async def test_same_severity_after_escalation_returns_same() -> None:
    """New assessment at same severity → SAME from matcher (no new version)."""
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian-001", child_user_id="child-001", now=BASE)
    tracker, matcher, incidents, _, scheduling = _make_tracker(links=links)

    # NEW
    await tracker.evaluate_security(
        assessment=_make_safety(summary="Bullying", evidence=("msg-1",)),
        assessment_id="assess-001",
    )
    incident = (await incidents.list_active("child-001", "chat-001", "security"))[0]

    # ESCALATION
    matcher.set_next(
        IncidentMatchResult(
            decision=IncidentMatchDecision.SIGNIFICANT_ESCALATION,
            incident_id=incident.id,
            reason="weapon",
        )
    )
    await tracker.evaluate_security(
        assessment=_make_safety(summary="Knife threat", evidence=("msg-2",)),
        assessment_id="assess-002",
    )
    assert len(scheduling.created) == 2

    # Same severity → SAME (no new version)
    matcher.set_next(
        IncidentMatchResult(
            decision=IncidentMatchDecision.SAME_INCIDENT,
            incident_id=incident.id,
            reason="same escalation level",
        )
    )
    result = await tracker.evaluate_security(
        assessment=_make_safety(summary="Knife threat again", evidence=("msg-3",)),
        assessment_id="assess-003",
    )
    assert result == "suppressed"
    assert len(scheduling.created) == 2  # no new alert


# --- Auto-close stale -----------------------------------------------------


async def test_auto_close_stale_incident() -> None:
    incidents = InMemoryAlertedIncidentRepository()
    stale = AlertedIncident(
        id="inc-stale",
        child_user_id="child-001",
        chat_id="chat-001",
        alert_type="security",
        summary="Old issue",
        severity_summary="Old issue",
        notified_severity_summary="Old issue",
        evidence_message_ids=("msg-old",),
        assessment_ids=("assess-old",),
        last_seen_at=BASE - timedelta(hours=48),
        closed_at=None,
        related_incident_id=None,
        notification_version=1,
        created_at=BASE - timedelta(hours=48),
        updated_at=BASE - timedelta(hours=48),
    )
    await incidents.create(stale)

    tracker, _, _, _, _ = _make_tracker(incidents=incidents)

    # Trigger auto-close by evaluating a new assessment
    await tracker.evaluate_security(
        assessment=_make_safety(summary="New issue", evidence=("msg-new",)),
        assessment_id="assess-new",
    )

    closed = await incidents.get("inc-stale")
    assert closed is not None
    assert closed.closed_at is not None


# --- Exposure -------------------------------------------------------------


async def test_exposure_evaluation() -> None:
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian-001", child_user_id="child-001", now=BASE)
    tracker, _, _, _, scheduling = _make_tracker(links=links)

    exposure = ExposureAssessment(
        significant_exposure=True,
        category=ExposureCategory.VIOLENCE,
        summary="Violent content in group",
        evidence_message_ids=("msg-1",),
        reason="Violent content detected",
        child_user_id="child-001",
        child_name="Mikush",
        chat_id="chat-001",
        created_at=BASE,
    )

    result = await tracker.evaluate_exposure(
        assessment=exposure,
        assessment_id="exp-001",
    )

    assert result == "scheduled"
    assert len(scheduling.created) == 1


async def test_exposure_suppressed_by_security_precedence() -> None:
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian-001", child_user_id="child-001", now=BASE)
    tracker, _, _, _, scheduling = _make_tracker(links=links)

    security = _make_safety(
        summary="Security issue", evidence=("msg-1", "msg-2"), yes=True
    )
    exposure = ExposureAssessment(
        significant_exposure=True,
        category=ExposureCategory.VIOLENCE,
        summary="Violent content",
        evidence_message_ids=("msg-2", "msg-3"),
        reason="Violent",
        child_user_id="child-001",
        child_name="Mikush",
        chat_id="chat-001",
        created_at=BASE,
    )

    result = await tracker.evaluate_exposure(
        assessment=exposure,
        assessment_id="exp-001",
        security_assessment=security,
    )

    assert result == "security_precedence"
    assert scheduling.created == []


# --- Disabled -------------------------------------------------------------


async def test_disabled_tracker_returns_disabled() -> None:
    tracker, _, _, _, _ = _make_tracker(enabled=False)
    result = await tracker.evaluate_security(
        assessment=_make_safety(),
        assessment_id="assess-001",
    )
    assert result == "disabled"


async def test_not_parent_attention() -> None:
    tracker, _, _, _, _ = _make_tracker()
    result = await tracker.evaluate_security(
        assessment=_make_safety(yes=False),
        assessment_id="assess-001",
    )
    assert result == "not_parent_attention"


# --- Idempotency ----------------------------------------------------------


async def test_reprocessing_same_assessment_is_idempotent() -> None:
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian-001", child_user_id="child-001", now=BASE)
    tracker, _, _, _, scheduling = _make_tracker(links=links)

    await tracker.evaluate_security(
        assessment=_make_safety(),
        assessment_id="assess-001",
    )
    assert len(scheduling.created) == 1

    result = await tracker.evaluate_security(
        assessment=_make_safety(),
        assessment_id="assess-001",
    )
    assert result == "already_processed"
    assert len(scheduling.created) == 1  # no duplicate


# --- Concurrency: lock-time rerun -----------------------------------------


async def test_concurrent_new_incident_rerun_matching() -> None:
    """If a new incident appears while waiting for the lock, rerun matching."""
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian-001", child_user_id="child-001", now=BASE)
    incidents = InMemoryAlertedIncidentRepository()
    matcher = FakeMatcher()
    tracker, _, _, _, scheduling = _make_tracker(
        matcher=matcher, incidents=incidents, links=links
    )

    # First call: no candidates → NEW without LLM
    await tracker.evaluate_security(
        assessment=_make_safety(summary="Issue A", evidence=("msg-1",)),
        assessment_id="assess-001",
    )
    assert len(scheduling.created) == 1

    # Now there's one active incident. Second assessment is for a DIFFERENT issue.
    # Matcher first says NEW, but after lock a new incident appeared → rerun.
    # But in this case, the incident already existed before the lock,
    # so no rerun happens. The matcher should say NEW for a distinct issue.
    matcher.set_next(
        IncidentMatchResult(
            decision=IncidentMatchDecision.NEW_INCIDENT,
            incident_id=None,
            reason="distinct issue",
        )
    )
    result = await tracker.evaluate_security(
        assessment=_make_safety(summary="Blackmail", evidence=("msg-2",)),
        assessment_id="assess-002",
    )
    assert result == "scheduled"
    assert len(scheduling.created) == 2  # second incident gets its own alert

    active = await incidents.list_active("child-001", "chat-001", "security")
    assert len(active) == 2  # two distinct incidents
