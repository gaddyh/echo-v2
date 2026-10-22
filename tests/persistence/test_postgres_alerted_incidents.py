"""Postgres-backed tests for the alerted incidents repository."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from echo_v2.domain.alerted_incident import AlertedIncident
from echo_v2.persistence.alerted_incident_repository import (
    PostgresAlertedIncidentRepository,
)

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 10, 23, 10, tzinfo=timezone.utc)


def _make_incident(
    *,
    id: str = "inc-001",
    child_user_id: str = "child-001",
    chat_id: str = "chat-001",
    alert_type: str = "security",
    summary: str = "Bullying at school",
    closed_at: datetime | None = None,
    assessment_ids: tuple[str, ...] = ("assess-001",),
    evidence_message_ids: tuple[str, ...] = ("msg-1",),
    notification_version: int = 1,
    last_seen_at: datetime | None = None,
) -> AlertedIncident:
    return AlertedIncident(
        id=id,
        child_user_id=child_user_id,
        chat_id=chat_id,
        alert_type=alert_type,
        summary=summary,
        severity_summary=summary,
        notified_severity_summary=summary,
        evidence_message_ids=evidence_message_ids,
        assessment_ids=assessment_ids,
        last_seen_at=last_seen_at or NOW,
        closed_at=closed_at,
        related_incident_id=None,
        notification_version=notification_version,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest_asyncio.fixture
async def repo(session_factory, clean_db) -> PostgresAlertedIncidentRepository:
    return PostgresAlertedIncidentRepository(session_factory)


async def test_create_and_get(repo: PostgresAlertedIncidentRepository) -> None:
    incident = _make_incident()
    created = await repo.create(incident)
    assert created.id == "inc-001"

    fetched = await repo.get("inc-001")
    assert fetched is not None
    assert fetched.child_user_id == "child-001"
    assert fetched.summary == "Bullying at school"
    assert fetched.assessment_ids == ("assess-001",)
    assert fetched.evidence_message_ids == ("msg-1",)


async def test_get_nonexistent_returns_none(repo: PostgresAlertedIncidentRepository) -> None:
    assert await repo.get("nonexistent") is None


async def test_list_active(repo: PostgresAlertedIncidentRepository) -> None:
    await repo.create(_make_incident(id="inc-1", summary="Issue A"))
    await repo.create(_make_incident(id="inc-2", summary="Issue B"))
    # Closed incident should not appear in list_active
    await repo.create(
        _make_incident(id="inc-3", summary="Closed", closed_at=NOW)
    )

    active = await repo.list_active("child-001", "chat-001", "security")
    assert len(active) == 2
    summaries = {inc.summary for inc in active}
    assert summaries == {"Issue A", "Issue B"}


async def test_list_active_filters_by_alert_type(
    repo: PostgresAlertedIncidentRepository,
) -> None:
    await repo.create(_make_incident(id="inc-1", alert_type="security"))
    await repo.create(_make_incident(id="inc-2", alert_type="exposure"))

    security = await repo.list_active("child-001", "chat-001", "security")
    assert len(security) == 1
    assert security[0].alert_type == "security"

    exposure = await repo.list_active("child-001", "chat-001", "exposure")
    assert len(exposure) == 1
    assert exposure[0].alert_type == "exposure"


async def test_list_recently_closed(repo: PostgresAlertedIncidentRepository) -> None:
    closed_recently = _make_incident(
        id="inc-recent",
        closed_at=NOW - timedelta(hours=12),
    )
    closed_old = _make_incident(
        id="inc-old",
        closed_at=NOW - timedelta(days=10),
    )
    active = _make_incident(id="inc-active")
    await repo.create(closed_recently)
    await repo.create(closed_old)
    await repo.create(active)

    since = NOW - timedelta(days=1)
    closed = await repo.list_recently_closed("child-001", "chat-001", "security", since)
    assert len(closed) == 1
    assert closed[0].id == "inc-recent"


async def test_update(repo: PostgresAlertedIncidentRepository) -> None:
    await repo.create(_make_incident(id="inc-1", summary="Original"))
    incident = await repo.get("inc-1")
    assert incident is not None

    from dataclasses import replace

    updated = replace(
        incident,
        summary="Updated summary",
        severity_summary="Updated severity",
        evidence_message_ids=("msg-1", "msg-2"),
        assessment_ids=("assess-001", "assess-002"),
        updated_at=NOW,
    )
    await repo.update(updated)

    fetched = await repo.get("inc-1")
    assert fetched is not None
    assert fetched.summary == "Updated summary"
    assert fetched.severity_summary == "Updated severity"
    assert set(fetched.evidence_message_ids) == {"msg-1", "msg-2"}
    assert set(fetched.assessment_ids) == {"assess-001", "assess-002"}


async def test_close(repo: PostgresAlertedIncidentRepository) -> None:
    await repo.create(_make_incident(id="inc-1"))
    assert (await repo.get("inc-1")).closed_at is None

    close_time = NOW + timedelta(hours=1)
    await repo.close("inc-1", close_time)

    fetched = await repo.get("inc-1")
    assert fetched is not None
    assert fetched.closed_at == close_time

    # Should no longer appear in list_active
    active = await repo.list_active("child-001", "chat-001", "security")
    assert len(active) == 0


async def test_find_by_assessment_id(repo: PostgresAlertedIncidentRepository) -> None:
    await repo.create(
        _make_incident(id="inc-1", assessment_ids=("assess-001", "assess-002"))
    )
    await repo.create(
        _make_incident(id="inc-2", assessment_ids=("assess-003",))
    )

    found = await repo.find_by_assessment_id(
        "assess-002", "child-001", "chat-001", "security"
    )
    assert found is not None
    assert found.id == "inc-1"

    not_found = await repo.find_by_assessment_id(
        "assess-999", "child-001", "chat-001", "security"
    )
    assert not_found is None


async def test_acquire_lock(repo: PostgresAlertedIncidentRepository) -> None:
    """acquire_lock should work as an async context manager."""
    async with repo.acquire_lock("child-001", "chat-001", "security"):
        # Lock held — do something
        await repo.create(_make_incident(id="inc-1"))
    # Lock released
    assert await repo.get("inc-1") is not None


async def test_notification_version_preserved(
    repo: PostgresAlertedIncidentRepository,
) -> None:
    await repo.create(_make_incident(id="inc-1", notification_version=3))
    fetched = await repo.get("inc-1")
    assert fetched is not None
    assert fetched.notification_version == 3
