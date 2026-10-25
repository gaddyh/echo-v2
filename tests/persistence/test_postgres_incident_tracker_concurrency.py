"""Postgres-backed integration tests for IncidentTracker concurrency & atomicity.

These tests start a Postgres Testcontainer, run migrations, and verify that
the IncidentTracker's UoW-based atomicity guarantees hold under real
concurrency and scheduling-failure scenarios.

Requires Docker. Skips locally if Docker is unavailable; fails in CI.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.alerted_incident import AlertedIncident
from echo_v2.domain.guard_simple import ChildRole, SafetyAssessment
from echo_v2.persistence.alerted_incident_repository import (
    PostgresAlertedIncidentRepository,
)
from echo_v2.persistence.postgres_guard import PostgresGuardianChildLinkRepository
from echo_v2.persistence.postgres_scheduled_actions import (
    PostgresScheduledActionRepository,
)
from echo_v2.services.incident_matcher import (
    IncidentCandidate,
    IncidentMatchDecision,
    IncidentMatcher,
    IncidentMatchResult,
    MatcherAssessment,
)
from echo_v2.services.incident_tracker import IncidentTracker

pytestmark = pytest.mark.asyncio
BASE = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)


class StubMatcher(IncidentMatcher):
    """Matcher that returns NEW when no candidates exist, SAME when they do.

    This simulates the real matcher behavior for concurrency tests: the
    first worker sees no candidates → NEW, the second worker (under the
    lock) sees the first worker's incident → SAME.
    """

    def __init__(self, decision: IncidentMatchDecision = IncidentMatchDecision.NEW_INCIDENT) -> None:
        self._override = decision
        self.calls: list[tuple[str, MatcherAssessment, tuple[IncidentCandidate, ...]]] = []

    async def match(
        self,
        *,
        alert_type: str,
        assessment: MatcherAssessment,
        incidents: tuple[IncidentCandidate, ...],
    ) -> IncidentMatchResult:
        self.calls.append((alert_type, assessment, incidents))
        if self._override is not IncidentMatchDecision.NEW_INCIDENT:
            return IncidentMatchResult(
                decision=self._override,
                incident_id=incidents[0].id if incidents else None,
                reason="stub override",
            )
        # Default: NEW when no candidates, SAME when there are any.
        if incidents:
            return IncidentMatchResult(
                decision=IncidentMatchDecision.SAME_INCIDENT,
                incident_id=incidents[0].id,
                reason="stub: existing incident found",
            )
        return IncidentMatchResult(
            decision=IncidentMatchDecision.NEW_INCIDENT,
            incident_id=None,
            reason="stub: no existing incidents",
        )


class StubUsers:
    """Returns a deterministic phone for any guardian user id."""

    async def get_phone_by_id(self, user_id: str) -> str | None:
        return f"+9725000{user_id[-6:]}"


async def _insert_user(session_factory: async_sessionmaker[AsyncSession], phone: str) -> str:
    async with session_factory() as session:
        result = await session.execute(
            text("INSERT INTO users (phone_number) VALUES (:p) RETURNING id"),
            {"p": phone},
        )
        user_id = str(result.scalar_one())
        await session.commit()
    return user_id


async def _insert_guardian_link(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    guardian_user_id: str,
    child_user_id: str,
    now: datetime,
) -> None:
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO guardian_child_links "
                "(guardian_user_id, child_user_id, status, guardian_authorized_at, "
                "safety_enabled_at, created_at, updated_at) "
                "VALUES (:g, :c, 'active', :now, :now, :now, :now)"
            ),
            {
                "g": guardian_user_id,
                "c": child_user_id,
                "now": now,
            },
        )
        await session.commit()


def _make_safety(
    *,
    summary: str = "Bullying at school",
    evidence: tuple[str, ...] = ("msg-1", "msg-2"),
    child_id: str = "child-001",
    chat_id: str = "chat-001",
    assessment_id: str = "assess-001",
) -> SafetyAssessment:
    return SafetyAssessment(
        parent_should_know=True,
        child_role=ChildRole.TARGET,
        summary=summary,
        evidence_message_ids=evidence,
        reason="Analyzer detected concerning content",
        child_user_id=child_id,
        child_name="Mikush",
        chat_id=chat_id,
        created_at=BASE,
        last_evidence_at=BASE,
    )


@pytest_asyncio.fixture
async def tracker_deps(
    session_factory: async_sessionmaker[AsyncSession], clean_db: None
) -> dict[str, Any]:
    """Set up Postgres repos + a UoW factory for the IncidentTracker."""
    from echo_v2.persistence.credential_cipher import IdentityCredentialCipher
    from echo_v2.persistence.unit_of_work import PostgresUnitOfWork

    incidents_repo = PostgresAlertedIncidentRepository(session_factory)
    links_repo = PostgresGuardianChildLinkRepository(session_factory)
    scheduling_repo = PostgresScheduledActionRepository(session_factory)

    def uow_factory() -> PostgresUnitOfWork:
        return PostgresUnitOfWork(session_factory, IdentityCredentialCipher())

    # Insert a guardian user + link
    guardian_id = await _insert_user(session_factory, "+9725000001")
    child_id = await _insert_user(session_factory, "+9725000002")
    await _insert_guardian_link(
        session_factory,
        guardian_user_id=guardian_id,
        child_user_id=child_id,
        now=BASE,
    )

    return {
        "incidents_repo": incidents_repo,
        "links_repo": links_repo,
        "scheduling_repo": scheduling_repo,
        "uow_factory": uow_factory,
        "guardian_id": guardian_id,
        "child_id": child_id,
    }


async def test_concurrent_new_produces_one_incident_one_schedule(tracker_deps: dict[str, Any]) -> None:
    """Two concurrent evaluate_security calls for the same child/chat → one incident + one schedule."""
    matcher = StubMatcher(IncidentMatchDecision.NEW_INCIDENT)
    tracker = IncidentTracker(
        matcher=matcher,
        incidents=tracker_deps["incidents_repo"],
        links=tracker_deps["links_repo"],
        users=StubUsers(),
        scheduling=tracker_deps["scheduling_repo"],
        unit_of_work_factory=tracker_deps["uow_factory"],
        close_after_hours=24 * 365,  # disable auto-close for time-independent tests
        enabled=True,
    )

    child_id = tracker_deps["child_id"]
    assessment = _make_safety(child_id=child_id, assessment_id="assess-001")

    # Two concurrent evaluations for the same child/chat
    results = await asyncio.gather(
        tracker.evaluate_security(assessment=assessment, assessment_id="assess-001"),
        tracker.evaluate_security(assessment=assessment, assessment_id="assess-002"),
        return_exceptions=True,
    )

    # One should succeed (scheduled), the other should be suppressed or already_processed
    # The advisory lock serializes them; the second sees the first's incident.
    scheduled_count = sum(1 for r in results if r == "scheduled")
    suppressed_count = sum(
        1 for r in results if isinstance(r, str) and r in ("suppressed", "already_processed")
    )
    assert scheduled_count == 1, f"Expected exactly 1 scheduled, got {results}"
    assert suppressed_count == 1, f"Expected 1 suppressed/already_processed, got {results}"

    # Verify only one incident in the DB
    active = await tracker_deps["incidents_repo"].list_active(child_id, "chat-001", "security")
    assert len(active) == 1

    # Verify only one scheduled action
    actions = await tracker_deps["scheduling_repo"].list_pending(tracker_deps["guardian_id"])
    assert len(actions) == 1


async def test_repeated_same_suppresses(tracker_deps: dict[str, Any]) -> None:
    """First call creates an incident + schedule; second (SAME) suppresses."""
    matcher = StubMatcher(IncidentMatchDecision.NEW_INCIDENT)
    tracker = IncidentTracker(
        matcher=matcher,
        incidents=tracker_deps["incidents_repo"],
        links=tracker_deps["links_repo"],
        users=StubUsers(),
        scheduling=tracker_deps["scheduling_repo"],
        unit_of_work_factory=tracker_deps["uow_factory"],
        close_after_hours=24 * 365,  # disable auto-close for time-independent tests
        enabled=True,
    )

    child_id = tracker_deps["child_id"]

    # First call → NEW
    result1 = await tracker.evaluate_security(
        assessment=_make_safety(summary="Bullying", evidence=("msg-1",), child_id=child_id),
        assessment_id="assess-001",
    )
    assert result1 == "scheduled"

    # Get the incident id
    active = await tracker_deps["incidents_repo"].list_active(child_id, "chat-001", "security")
    assert len(active) == 1

    # Second call → SAME (matcher returns SAME for the existing incident)
    matcher_same = StubMatcher(IncidentMatchDecision.SAME_INCIDENT)
    tracker._matcher = matcher_same
    result2 = await tracker.evaluate_security(
        assessment=_make_safety(summary="More bullying", evidence=("msg-2",), child_id=child_id),
        assessment_id="assess-002",
    )
    assert result2 == "suppressed"

    # Still only one incident
    active = await tracker_deps["incidents_repo"].list_active(child_id, "chat-001", "security")
    assert len(active) == 1

    # Still only one scheduled action
    actions = await tracker_deps["scheduling_repo"].list_pending(tracker_deps["guardian_id"])
    assert len(actions) == 1


async def test_escalation_schedules_new_version(tracker_deps: dict[str, Any]) -> None:
    """First call creates version 1; escalation creates version 2."""
    matcher = StubMatcher(IncidentMatchDecision.NEW_INCIDENT)
    tracker = IncidentTracker(
        matcher=matcher,
        incidents=tracker_deps["incidents_repo"],
        links=tracker_deps["links_repo"],
        users=StubUsers(),
        scheduling=tracker_deps["scheduling_repo"],
        unit_of_work_factory=tracker_deps["uow_factory"],
        close_after_hours=24 * 365,  # disable auto-close for time-independent tests
        enabled=True,
    )

    child_id = tracker_deps["child_id"]

    # First call → NEW
    await tracker.evaluate_security(
        assessment=_make_safety(summary="Bullying", evidence=("msg-1",), child_id=child_id),
        assessment_id="assess-001",
    )

    active = await tracker_deps["incidents_repo"].list_active(child_id, "chat-001", "security")
    assert len(active) == 1
    incident_id = active[0].id
    assert active[0].notification_version == 1

    # Second call → ESCALATION
    matcher_esc = StubMatcher(IncidentMatchDecision.SIGNIFICANT_ESCALATION)
    tracker._matcher = matcher_esc
    result = await tracker.evaluate_security(
        assessment=_make_safety(summary="Knife threat", evidence=("msg-2",), child_id=child_id),
        assessment_id="assess-002",
    )
    assert result == "escalation_scheduled"

    # Incident version bumped to 2
    updated = await tracker_deps["incidents_repo"].get(incident_id)
    assert updated is not None
    assert updated.notification_version == 2
    assert updated.severity_summary == "Knife threat"

    # Two scheduled actions (v1 + v2)
    actions = await tracker_deps["scheduling_repo"].list_pending(tracker_deps["guardian_id"])
    assert len(actions) == 2


async def test_closed_recurrence_creates_linked_incident(tracker_deps: dict[str, Any]) -> None:
    """Matcher returns SAME for a closed incident → new linked incident + alert."""
    child_id = tracker_deps["child_id"]

    # Pre-create a closed incident
    closed = AlertedIncident(
        id="inc-closed",
        child_user_id=child_id,
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
    await tracker_deps["incidents_repo"].create(closed)

    matcher = StubMatcher(IncidentMatchDecision.SAME_INCIDENT)
    tracker = IncidentTracker(
        matcher=matcher,
        incidents=tracker_deps["incidents_repo"],
        links=tracker_deps["links_repo"],
        users=StubUsers(),
        scheduling=tracker_deps["scheduling_repo"],
        unit_of_work_factory=tracker_deps["uow_factory"],
        close_after_hours=24 * 365,  # disable auto-close for time-independent tests
        enabled=True,
    )

    result = await tracker.evaluate_security(
        assessment=_make_safety(summary="Bullying again", evidence=("msg-new",), child_id=child_id),
        assessment_id="assess-001",
    )
    assert result == "recurrence_scheduled"

    # A new incident was created, linked to the closed one
    active = await tracker_deps["incidents_repo"].list_active(child_id, "chat-001", "security")
    assert len(active) == 1
    new_incident = active[0]
    assert new_incident.id != "inc-closed"
    assert new_incident.related_incident_id == "inc-closed"
    assert new_incident.notification_version == 1

    # One scheduled action for the recurrence
    actions = await tracker_deps["scheduling_repo"].list_pending(tracker_deps["guardian_id"])
    assert len(actions) == 1


async def test_scheduling_failure_rolls_back_incident(tracker_deps: dict[str, Any]) -> None:
    """If scheduling fails, the UoW rolls back — no incident, no schedule."""
    from echo_v2.domain.scheduling import ScheduledAction
    from echo_v2.persistence.scheduled_actions import ScheduledActionRepository
    from echo_v2.persistence.unit_of_work import PostgresUnitOfWork

    class FailingSchedulingRepo(ScheduledActionRepository):
        """ScheduledActionRepository that always fails create_once."""

        async def create_once(self, action: ScheduledAction) -> tuple[ScheduledAction, bool]:
            raise RuntimeError("scheduler down")

        async def get(self, action_id: str) -> ScheduledAction | None:
            return None

        async def list_pending(self, user_id: str) -> list[ScheduledAction]:
            return []

    class FailingUoW(PostgresUnitOfWork):
        """UoW that injects a failing scheduled_actions repo."""

        async def __aenter__(self) -> PostgresUnitOfWork:
            await super().__aenter__()
            assert self.repos is not None
            self.repos.scheduled_actions = FailingSchedulingRepo()  # type: ignore[assignment]
            return self

    original_factory = tracker_deps["uow_factory"]

    def failing_uow_factory() -> PostgresUnitOfWork:
        original = original_factory()
        # Create a FailingUoW with the same session_factory
        return FailingUoW(original._session_factory, original._cipher, original._lease_seconds)

    matcher = StubMatcher(IncidentMatchDecision.NEW_INCIDENT)
    tracker = IncidentTracker(
        matcher=matcher,
        incidents=tracker_deps["incidents_repo"],
        links=tracker_deps["links_repo"],
        users=StubUsers(),
        scheduling=tracker_deps["scheduling_repo"],
        unit_of_work_factory=failing_uow_factory,
        enabled=True,
    )

    child_id = tracker_deps["child_id"]

    with pytest.raises(RuntimeError, match="scheduler down"):
        await tracker.evaluate_security(
            assessment=_make_safety(child_id=child_id),
            assessment_id="assess-001",
        )

    # No incident saved — UoW rolled back
    active = await tracker_deps["incidents_repo"].list_active(child_id, "chat-001", "security")
    assert len(active) == 0

    # No scheduled action
    actions = await tracker_deps["scheduling_repo"].list_pending(tracker_deps["guardian_id"])
    assert len(actions) == 0
