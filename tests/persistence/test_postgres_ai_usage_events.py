"""Postgres-backed AiUsageEvent repository tests — persistence and idempotency."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from echo_v2.domain.ai_usage_event import (
    AiCostSource,
    AiUsageEvent,
    AiUsageStatus,
    AiUsageType,
)
from echo_v2.persistence.postgres_ai_usage_events import (
    PostgresAiUsageEventRepository,
)
from tests.persistence.conftest import insert_user

pytestmark = pytest.mark.asyncio


def _make_event(
    *,
    invocation_id=None,
    child_user_id: str | None = None,
    status: str = AiUsageStatus.SUCCESS,
    operation: str = "guard_security",
) -> AiUsageEvent:
    return AiUsageEvent(
        invocation_id=invocation_id or uuid4(),
        provider="openai",
        operation=operation,
        model="gpt-5.4-mini",
        usage_type=AiUsageType.TOKENS,
        cost_source=AiCostSource.MEASURED_USAGE_PRICED,
        status=status,
        child_user_id=child_user_id,
        prompt_tokens=1000,
        completion_tokens=200,
        total_tokens=1200,
        estimated_cost_usd=None,
        price_version="2026-10-26.1",
    )


async def test_record_and_list_event(session_factory, clean_db) -> None:
    """A recorded event can be listed back."""
    child_id = await insert_user(session_factory, "+972546610653")
    repo = PostgresAiUsageEventRepository(session_factory)
    now = datetime.now(timezone.utc)
    event = _make_event(child_user_id=child_id)
    await repo.record(event)

    events = await repo.list_for_range(
        date_from=now - timedelta(minutes=1),
        date_to=now + timedelta(minutes=1),
    )
    assert len(events) == 1
    assert events[0].invocation_id == event.invocation_id
    assert events[0].operation == "guard_security"
    assert events[0].prompt_tokens == 1000


async def test_idempotent_persistence_same_invocation_id(session_factory, clean_db) -> None:
    """Recording the same invocation_id twice produces one row (idempotent)."""
    child_id = await insert_user(session_factory, "+972546610653")
    repo = PostgresAiUsageEventRepository(session_factory)
    now = datetime.now(timezone.utc)
    inv_id = uuid4()
    event = _make_event(invocation_id=inv_id, child_user_id=child_id)
    await repo.record(event)
    await repo.record(event)  # same invocation_id — should be a no-op

    events = await repo.list_for_range(
        date_from=now - timedelta(minutes=1),
        date_to=now + timedelta(minutes=1),
    )
    assert len(events) == 1


async def test_separate_invocation_ids_produce_separate_rows(
    session_factory, clean_db
) -> None:
    """Provider retries get fresh invocation_ids → separate rows."""
    child_id = await insert_user(session_factory, "+972546610653")
    repo = PostgresAiUsageEventRepository(session_factory)
    now = datetime.now(timezone.utc)
    await repo.record(_make_event(invocation_id=uuid4(), child_user_id=child_id))
    await repo.record(_make_event(invocation_id=uuid4(), child_user_id=child_id))

    events = await repo.list_for_range(
        date_from=now - timedelta(minutes=1),
        date_to=now + timedelta(minutes=1),
    )
    assert len(events) == 2


async def test_list_filtered_by_child(session_factory, clean_db) -> None:
    """list_for_range filters by child_user_id."""
    child_1 = await insert_user(session_factory, "+972546610653")
    child_2 = await insert_user(session_factory, "+972546610654")
    repo = PostgresAiUsageEventRepository(session_factory)
    now = datetime.now(timezone.utc)
    await repo.record(_make_event(child_user_id=child_1))
    await repo.record(_make_event(child_user_id=child_2))

    events = await repo.list_for_range(
        child_user_id=child_1,
        date_from=now - timedelta(minutes=1),
        date_to=now + timedelta(minutes=1),
    )
    assert len(events) == 1
    assert events[0].child_user_id == child_1


async def test_error_status_event_persisted(session_factory, clean_db) -> None:
    """A failed billable attempt is persisted with error status."""
    repo = PostgresAiUsageEventRepository(session_factory)
    now = datetime.now(timezone.utc)
    event = AiUsageEvent(
        invocation_id=uuid4(),
        provider="openai",
        operation="guard_security",
        model="gpt-5.4-mini",
        usage_type=AiUsageType.TOKENS,
        cost_source=AiCostSource.UNKNOWN,
        status=AiUsageStatus.ERROR,
        child_user_id=None,
        error_type="APIConnectionError",
        estimated_cost_usd=None,
        price_version="2026-10-26.1",
    )
    await repo.record(event)

    events = await repo.list_for_range(
        date_from=now - timedelta(minutes=1),
        date_to=now + timedelta(minutes=1),
    )
    assert len(events) == 1
    assert events[0].status == AiUsageStatus.ERROR
    assert events[0].error_type == "APIConnectionError"
    assert events[0].estimated_cost_usd is None


async def test_audio_event_persisted_with_usage_details(
    session_factory, clean_db
) -> None:
    """A Modal audio event stores usage_details with audio_duration_seconds."""
    child_id = await insert_user(session_factory, "+972546610653")
    repo = PostgresAiUsageEventRepository(session_factory)
    now = datetime.now(timezone.utc)
    event = AiUsageEvent(
        invocation_id=uuid4(),
        provider="modal",
        operation="media_audio",
        model="whisper-large-v3",
        usage_type=AiUsageType.AUDIO_SECONDS,
        cost_source=AiCostSource.FALLBACK_ESTIMATE,
        status=AiUsageStatus.SUCCESS,
        child_user_id=child_id,
        usage_details={
            "audio_duration_seconds": 120.0,
            "processing_seconds": 5.0,
        },
        estimated_cost_usd=None,
        price_version="2026-10-26.1",
    )
    await repo.record(event)

    events = await repo.list_for_range(
        date_from=now - timedelta(minutes=1),
        date_to=now + timedelta(minutes=1),
    )
    assert len(events) == 1
    assert events[0].usage_details["audio_duration_seconds"] == 120.0
    assert events[0].usage_details["processing_seconds"] == 5.0
    assert events[0].provider == "modal"
