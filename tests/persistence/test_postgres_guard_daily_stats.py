"""Postgres-backed GuardDailyStats repository tests — upsert and idempotency."""

from __future__ import annotations

from datetime import date

import pytest

from echo_v2.domain.guard_daily_stats import CoverageStatus, GuardDailyStats
from echo_v2.persistence.postgres_guard_daily_stats import (
    PostgresGuardDailyStatsRepository,
)
from tests.persistence.conftest import insert_user

pytestmark = pytest.mark.asyncio


def _make_stats(
    *,
    child_user_id: str,
    local_date: date = date(2026, 10, 25),
    messages_received: int = 10,
    coverage_status: str = CoverageStatus.PROTECTED,
) -> GuardDailyStats:
    return GuardDailyStats(
        child_user_id=child_user_id,
        local_date=local_date,
        timezone="Asia/Jerusalem",
        messages_received=messages_received,
        private_messages=4,
        group_messages=6,
        active_private_chats=2,
        active_group_chats=1,
        images_received=3,
        videos_received=1,
        security_detections=1,
        security_alerts_sent=0,
        coverage_status=coverage_status,
    )


async def test_upsert_and_get(session_factory, clean_db) -> None:
    """Upserting a stats row and getting it back works."""
    child_id = await insert_user(session_factory, "+9725000001")
    repo = PostgresGuardDailyStatsRepository(session_factory)

    stats = _make_stats(child_user_id=child_id)
    await repo.upsert(stats)

    fetched = await repo.get(child_id, date(2026, 10, 25))
    assert fetched is not None
    assert fetched.child_user_id == child_id
    assert fetched.local_date == date(2026, 10, 25)
    assert fetched.messages_received == 10
    assert fetched.private_messages == 4
    assert fetched.group_messages == 6
    assert fetched.images_received == 3
    assert fetched.coverage_status == CoverageStatus.PROTECTED


async def test_upsert_is_idempotent(session_factory, clean_db) -> None:
    """Rebuilding the same day twice yields the same row (no inflation)."""
    child_id = await insert_user(session_factory, "+9725000001")
    repo = PostgresGuardDailyStatsRepository(session_factory)

    stats1 = _make_stats(child_user_id=child_id, messages_received=10)
    await repo.upsert(stats1)

    # Rebuild with different count — should overwrite, not duplicate
    stats2 = _make_stats(child_user_id=child_id, messages_received=15)
    await repo.upsert(stats2)

    fetched = await repo.get(child_id, date(2026, 10, 25))
    assert fetched is not None
    assert fetched.messages_received == 15  # updated, not duplicated

    # Only one row
    rows = await repo.list_for_range(
        child_user_id=child_id,
        date_from=date(2026, 10, 25),
        date_to=date(2026, 10, 25),
    )
    assert len(rows) == 1


async def test_list_for_range(session_factory, clean_db) -> None:
    """List multiple days for a child."""
    child_id = await insert_user(session_factory, "+9725000001")
    repo = PostgresGuardDailyStatsRepository(session_factory)

    for d in [date(2026, 10, 23), date(2026, 10, 24), date(2026, 10, 25)]:
        await repo.upsert(_make_stats(child_user_id=child_id, local_date=d))

    rows = await repo.list_for_range(
        child_user_id=child_id,
        date_from=date(2026, 10, 23),
        date_to=date(2026, 10, 25),
    )
    assert len(rows) == 3
    assert rows[0].local_date == date(2026, 10, 23)
    assert rows[2].local_date == date(2026, 10, 25)


async def test_list_for_range_all_children(session_factory, clean_db) -> None:
    """List across all children for a date range."""
    child1 = await insert_user(session_factory, "+9725000001")
    child2 = await insert_user(session_factory, "+9725000002")
    repo = PostgresGuardDailyStatsRepository(session_factory)

    await repo.upsert(_make_stats(child_user_id=child1, local_date=date(2026, 10, 25)))
    await repo.upsert(_make_stats(child_user_id=child2, local_date=date(2026, 10, 25)))

    rows = await repo.list_for_range(
        date_from=date(2026, 10, 25),
        date_to=date(2026, 10, 25),
    )
    assert len(rows) == 2


async def test_get_nonexistent_returns_none(session_factory, clean_db) -> None:
    """Getting a non-existent day returns None."""
    child_id = await insert_user(session_factory, "+9725000001")
    repo = PostgresGuardDailyStatsRepository(session_factory)

    fetched = await repo.get(child_id, date(2026, 1, 1))
    assert fetched is None
