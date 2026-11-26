"""Postgres-backed integration tests for the WAHA worker registry.

Starts a Postgres Testcontainer, runs migrations, and verifies the
idempotent registration upsert (first-registration timestamp preserved),
independent workers, health/capacity/pending updates, and read-time
availability derivation: fresh Echo-side heartbeat AND health == "ok",
with capacity eligibility kept separate.

Requires Docker. Skips locally if Docker is unavailable; fails in CI.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.waha import WahaWorkerRegistration
from echo_v2.persistence.postgres_waha_workers import PostgresWahaWorkerRegistry

NOW = datetime(2026, 11, 26, 18, 0, 0, tzinfo=timezone.utc)


def _registration(
    *,
    worker_id: str = "waha-01-8e3dfc4bab32",
    region: str = "uk-lon1",
    health: str = "ok",
    pending: int = 0,
    active_sessions: int = 1,
    max_sessions: int = 1,
    version: str = "0.2",
    reported_heartbeat: datetime | None = None,
) -> WahaWorkerRegistration:
    return WahaWorkerRegistration(
        worker_id=worker_id,
        region=region,
        endpoint="http://127.0.0.1:3000",
        max_sessions=max_sessions,
        active_sessions=active_sessions,
        version=version,
        health=health,
        reported_heartbeat=reported_heartbeat or NOW,
        pending=pending,
    )


@pytest_asyncio.fixture
async def registry(
    session_factory: async_sessionmaker[AsyncSession], clean_db: None
) -> PostgresWahaWorkerRegistry:
    return PostgresWahaWorkerRegistry(session_factory)


async def test_first_registration(
    registry: PostgresWahaWorkerRegistry,
) -> None:
    first = await registry.register(_registration())
    assert first is True
    workers = await registry.list_workers()
    assert len(workers) == 1
    worker = workers[0]
    assert worker.worker_id == "waha-01-8e3dfc4bab32"
    assert worker.region == "uk-lon1"
    assert worker.agent_version == "0.2"
    assert worker.health == "ok"
    assert worker.first_registered_at is not None
    assert worker.last_seen_at is not None
    assert worker.available is True


async def test_repeated_heartbeat_preserves_first_registration(
    registry: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert await registry.register(_registration()) is True
    before = (await registry.list_workers())[0]
    # Backdate the first registration so an overwrite would be detectable.
    async with session_factory() as db:
        await db.execute(
            text(
                "UPDATE waha_workers SET first_registered_at = "
                "first_registered_at - interval '1 hour'"
            )
        )
        await db.commit()
    backdated = (await registry.list_workers())[0].first_registered_at

    assert (
        await registry.register(
            _registration(health="degraded", pending=7, active_sessions=1)
        )
        is False
    )
    after = (await registry.list_workers())[0]
    assert after.first_registered_at == backdated  # preserved, not reset
    assert after.last_seen_at >= before.last_seen_at  # server-side refresh
    assert after.health == "degraded"
    assert after.pending == 7


async def test_updates_health_capacity_and_pending(
    registry: PostgresWahaWorkerRegistry,
) -> None:
    await registry.register(_registration(health="starting", pending=3))
    await registry.register(_registration(health="ok", pending=0))
    worker = (await registry.list_workers())[0]
    assert worker.health == "ok"
    assert worker.pending == 0


async def test_independent_workers(
    registry: PostgresWahaWorkerRegistry,
) -> None:
    await registry.register(_registration(worker_id="waha-01"))
    await registry.register(_registration(worker_id="waha-02", region="fi-hel1"))

    workers = await registry.list_workers()
    assert [w.worker_id for w in workers] == ["waha-01", "waha-02"]
    assert {w.region for w in workers} == {"uk-lon1", "fi-hel1"}


async def test_stale_heartbeat_unavailable(
    registry: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await registry.register(_registration())
    # No heartbeat for 120s (timeout is 90s).
    async with session_factory() as db:
        await db.execute(
            text("UPDATE waha_workers SET last_seen_at = now() - interval '120 seconds'")
        )
        await db.commit()

    worker = (await registry.list_workers(heartbeat_timeout_seconds=90))[0]
    assert worker.health == "ok"
    assert worker.available is False
    # A larger timeout window still considers it fresh.
    assert (
        await registry.list_workers(heartbeat_timeout_seconds=300)
    )[0].available is True


async def test_degraded_health_unavailable_despite_fresh_heartbeat(
    registry: PostgresWahaWorkerRegistry,
) -> None:
    await registry.register(
        _registration(health="degraded", active_sessions=0, max_sessions=1)
    )
    worker = (await registry.list_workers())[0]
    assert worker.available is False
    assert worker.has_capacity is True  # capacity separate from availability


async def test_starting_health_unavailable(
    registry: PostgresWahaWorkerRegistry,
) -> None:
    await registry.register(_registration(health="starting"))
    assert (await registry.list_workers())[0].available is False


async def test_capacity_separate_from_availability(
    registry: PostgresWahaWorkerRegistry,
) -> None:
    await registry.register(
        _registration(active_sessions=1, max_sessions=2, health="ok")
    )
    worker = (await registry.list_workers())[0]
    assert worker.available is True
    assert worker.has_capacity is True

    await registry.register(
        _registration(active_sessions=2, max_sessions=2, health="ok")
    )
    worker = (await registry.list_workers())[0]
    assert worker.available is True  # still fresh + healthy
    assert worker.has_capacity is False  # but full


async def test_clock_skew_reported_heartbeat_not_authoritative(
    registry: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Worker clock is a day ahead — availability must not be affected.
    await registry.register(
        _registration(reported_heartbeat=NOW + timedelta(days=1))
    )
    assert (await registry.list_workers())[0].available is True
    assert (await registry.list_workers())[0].reported_heartbeat == NOW + timedelta(
        days=1
    )

    # Worker clock a day behind — also irrelevant once Echo stops receiving
    # heartbeats.
    await registry.register(
        _registration(reported_heartbeat=NOW - timedelta(days=1))
    )
    async with session_factory() as db:
        await db.execute(
            text("UPDATE waha_workers SET last_seen_at = now() - interval '120 seconds'")
        )
        await db.commit()
    worker = (await registry.list_workers())[0]
    assert worker.available is False
    assert worker.reported_heartbeat == NOW - timedelta(days=1)


async def test_heartbeat_timeout_is_configurable(
    registry: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await registry.register(_registration())
    # Backdate by 2 seconds — inside a 5s window, outside a 1s window.
    async with session_factory() as db:
        await db.execute(
            text("UPDATE waha_workers SET last_seen_at = now() - interval '2 seconds'")
        )
        await db.commit()

    fresh = (await registry.list_workers(heartbeat_timeout_seconds=5))[0]
    assert fresh.available is True
    stale = (await registry.list_workers(heartbeat_timeout_seconds=1))[0]
    assert stale.available is False
