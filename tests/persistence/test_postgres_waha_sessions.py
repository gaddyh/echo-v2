"""Postgres-backed integration tests for WAHA sessions + the agent command
queue (Phase 3, agent-based management).

Covers the atomic allocate-and-enqueue (stable session identity,
management-protocol eligibility, advisory-lock serialization, lowest-ratio
tie-break), the command queue semantics (worker-restricted claims, lease
expiry, attempt caps, idempotent completion), the fenced lifecycle
transition state machine (out-of-order / duplicate events, pending effects)
and QR recording. Requires Docker; skips locally if unavailable.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.waha import (
    WahaAllocationError,
    WahaCommandAction,
    WahaCommandStatus,
    WahaLifecycleEffect,
    WahaWorkerRegistration,
    effect_for_waha_status,
)
from echo_v2.persistence.postgres_waha_sessions import (
    PostgresWahaSessionRepository,
)
from echo_v2.persistence.postgres_waha_workers import PostgresWahaWorkerRegistry
from tests.persistence.conftest import insert_user

NOW = datetime(2026, 11, 26, 18, 0, 0, tzinfo=timezone.utc)


def _registration(
    *,
    worker_id: str = "waha-1",
    health: str = "ok",
    max_sessions: int = 4,
    active_sessions: int = 0,
    management_protocol: int = 1,
) -> WahaWorkerRegistration:
    return WahaWorkerRegistration(
        worker_id=worker_id,
        region="uk-lon1",
        endpoint="http://127.0.0.1:3000",
        max_sessions=max_sessions,
        active_sessions=active_sessions,
        version="0.3",
        health=health,
        reported_heartbeat=NOW,
        pending=0,
        management_protocol=management_protocol,
    )


@pytest_asyncio.fixture
async def workers(
    session_factory: async_sessionmaker[AsyncSession], clean_db: None
) -> PostgresWahaWorkerRegistry:
    return PostgresWahaWorkerRegistry(session_factory)


@pytest_asyncio.fixture
async def sessions(
    session_factory: async_sessionmaker[AsyncSession], clean_db: None
) -> PostgresWahaSessionRepository:
    return PostgresWahaSessionRepository(session_factory)


async def _register_worker(
    workers: PostgresWahaWorkerRegistry,
    *,
    worker_id: str,
    health: str = "ok",
    max_sessions: int = 4,
    management_protocol: int = 1,
) -> None:
    await workers.register(
        _registration(
            worker_id=worker_id,
            health=health,
            max_sessions=max_sessions,
            management_protocol=management_protocol,
        )
    )


async def _user(
    session_factory: async_sessionmaker[AsyncSession], phone: str
) -> str:
    return await insert_user(session_factory, phone=phone)


# --- allocation + atomic create command -----------------------------------------


async def test_allocate_and_enqueue_creates_assignment_and_command(
    sessions: PostgresWahaSessionRepository,
    workers: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _register_worker(workers, worker_id="waha-1")
    user_id = await _user(session_factory, "+972501111111")
    assignment = await sessions.allocate_and_enqueue_create(user_id)
    assert assignment.session_name.startswith("echo-")
    assert assignment.worker_id == "waha-1"
    assert assignment.status == "provisioning"
    commands = await sessions.active_commands(assignment.session_name)
    assert [c.action for c in commands] == [WahaCommandAction.CREATE_AND_START]
    assert commands[0].worker_id == "waha-1"


async def test_allocate_is_stable_across_attempts(
    sessions: PostgresWahaSessionRepository,
    workers: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _register_worker(workers, worker_id="waha-1")
    user_id = await _user(session_factory, "+972501111111")
    first = await sessions.allocate_and_enqueue_create(user_id)
    # Retries reuse the assignment and never duplicate the command.
    await _register_worker(workers, worker_id="waha-2")
    second = await sessions.allocate_and_enqueue_create(user_id)
    assert second == first
    assert len(await sessions.active_commands(first.session_name)) == 1


async def test_allocate_picks_lowest_ratio_with_deterministic_tie_break(
    sessions: PostgresWahaSessionRepository,
    workers: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _register_worker(workers, worker_id="waha-b")
    await _register_worker(workers, worker_id="waha-a")
    seeded = await sessions.allocate_and_enqueue_create(
        await _user(session_factory, "+972501111111")
    )
    assert seeded.worker_id == "waha-a"  # tie → worker_id tie-break

    next_assignment = await sessions.allocate_and_enqueue_create(
        await _user(session_factory, "+972501111112")
    )
    assert next_assignment.worker_id == "waha-b"  # 0/4 beats 1/4

    third = await sessions.allocate_and_enqueue_create(
        await _user(session_factory, "+972501111113")
    )
    assert third.worker_id == "waha-a"  # 1/4 vs 1/4 → tie-break


async def test_allocate_excludes_batch_only_workers(
    sessions: PostgresWahaSessionRepository,
    workers: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Backward-compatible registration: batch-only agents
    (management_protocol absent/0) never receive onboarding assignments."""
    await _register_worker(workers, worker_id="waha-1", management_protocol=0)
    user_id = await _user(session_factory, "+972501111111")
    with pytest.raises(WahaAllocationError) as excinfo:
        await sessions.allocate_and_enqueue_create(user_id)
    assert excinfo.value.reason == "no_management_agents"


async def test_allocate_excludes_stale_workers(
    sessions: PostgresWahaSessionRepository,
    workers: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _register_worker(workers, worker_id="waha-1")
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE waha_workers SET last_seen_at = now() - interval '10 minutes'"
                " WHERE worker_id = 'waha-1'"
            )
        )
        await session.commit()
    user_id = await _user(session_factory, "+972501111111")
    with pytest.raises(WahaAllocationError) as excinfo:
        await sessions.allocate_and_enqueue_create(user_id)
    assert excinfo.value.reason == "no_healthy_workers"


async def test_allocate_excludes_unhealthy_and_full_workers(
    sessions: PostgresWahaSessionRepository,
    workers: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _register_worker(workers, worker_id="waha-1", health="degraded")
    await _register_worker(workers, worker_id="waha-2", max_sessions=1)
    user_a = await _user(session_factory, "+972501111111")
    await sessions.allocate_and_enqueue_create(user_a)  # takes waha-2's slot
    user_b = await _user(session_factory, "+972501111112")
    with pytest.raises(WahaAllocationError) as excinfo:
        await sessions.allocate_and_enqueue_create(user_b)
    assert excinfo.value.reason == "no_capacity"
    assert excinfo.value.with_capacity == 0


async def test_concurrent_allocation_does_not_overbook(
    sessions: PostgresWahaSessionRepository,
    workers: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _register_worker(workers, worker_id="waha-1", max_sessions=2)
    await _register_worker(workers, worker_id="waha-2", max_sessions=2)
    user_ids = [
        await _user(session_factory, f"+9725000000{i:02d}") for i in range(8)
    ]
    results = await asyncio.gather(
        *[sessions.allocate_and_enqueue_create(u) for u in user_ids],
        return_exceptions=True,
    )
    ok = [r for r in results if not isinstance(r, BaseException)]
    errors = [r for r in results if isinstance(r, WahaAllocationError)]
    assert len(ok) + len(errors) == 8
    assert len(ok) == 4  # capacity 2+2
    counts = await sessions.allocated_counts()
    assert counts["waha-1"] == 2
    assert counts["waha-2"] == 2


async def test_concurrent_allocation_same_user_single_assignment(
    sessions: PostgresWahaSessionRepository,
    workers: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _register_worker(workers, worker_id="waha-1")
    user_id = await _user(session_factory, "+972501111111")
    results = await asyncio.gather(
        *[sessions.allocate_and_enqueue_create(user_id) for _ in range(5)]
    )
    assert len({r.session_name for r in results}) == 1
    assert len(await sessions.active_commands(results[0].session_name)) == 1


# --- command queue -----------------------------------------------------------------


async def test_enqueue_deduplicates_active_commands(
    sessions: PostgresWahaSessionRepository,
    workers: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _register_worker(workers, worker_id="waha-1")
    user_id = await _user(session_factory, "+972501111111")
    assignment = await sessions.allocate_and_enqueue_create(user_id)
    assert (
        await sessions.enqueue(
            assignment.session_name,
            worker_id="waha-1",
            action=WahaCommandAction.CREATE_AND_START,
        )
        is False
    )  # active create command exists
    assert (
        await sessions.enqueue(
            assignment.session_name,
            worker_id="waha-1",
            action=WahaCommandAction.GET_QR,
        )
        is True
    )


async def test_claim_returns_only_own_workers_commands(
    sessions: PostgresWahaSessionRepository,
    workers: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _register_worker(workers, worker_id="waha-1")
    await _register_worker(workers, worker_id="waha-2")
    a = await sessions.allocate_and_enqueue_create(
        await _user(session_factory, "+972501111111")
    )
    b = await sessions.allocate_and_enqueue_create(
        await _user(session_factory, "+972501111112")
    )
    assert a.worker_id != b.worker_id
    claimed = await sessions.claim_for_worker(a.worker_id)
    assert len(claimed) == 1
    assert claimed[0].session_name == a.session_name
    assert claimed[0].status == WahaCommandStatus.CLAIMED
    assert claimed[0].attempts == 1


async def test_claim_survives_concurrent_polling(
    sessions: PostgresWahaSessionRepository,
    workers: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Concurrent polls never independently claim the same command."""
    await _register_worker(workers, worker_id="waha-1", max_sessions=20)
    users = [
        await _user(session_factory, f"+9725022{i:04d}") for i in range(10)
    ]
    await asyncio.gather(
        *[sessions.allocate_and_enqueue_create(u) for u in users]
    )
    batches = await asyncio.gather(
        *[
            sessions.claim_for_worker("waha-1", limit=10)
            for _ in range(4)
        ]
    )
    all_claimed = [c.command_id for batch in batches for c in batch]
    # Each command claimed exactly once per poll round: SKIP LOCKED
    # guarantees no independent double-claims within the same instant.
    assert len(all_claimed) == len(set(all_claimed))


async def test_lease_expiry_makes_unacknowledged_commands_claimable(
    sessions: PostgresWahaSessionRepository,
    workers: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Agent restart during command execution: the expired lease is
    reclaimable (worker outage and recovery)."""
    await _register_worker(workers, worker_id="waha-1")
    user_id = await _user(session_factory, "+972501111111")
    await sessions.allocate_and_enqueue_create(user_id)
    first = await sessions.claim_for_worker("waha-1")
    assert len(first) == 1
    # Lease still holds → not re-delivered.
    assert await sessions.claim_for_worker("waha-1") == []
    # Expire the lease → claimable again, attempts increase.
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE waha_commands SET lease_expires_at = now() - interval '1 second'"
                " WHERE command_id = :c"
            ),
            {"c": uuid.UUID(first[0].command_id)},
        )
        await session.commit()
    second = await sessions.claim_for_worker("waha-1")
    assert len(second) == 1
    assert second[0].command_id == first[0].command_id
    assert second[0].attempts == 2


async def test_attempt_cap_marks_command_failed(
    sessions: PostgresWahaSessionRepository,
    workers: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Retries are bounded and observable."""
    await _register_worker(workers, worker_id="waha-1")
    user_id = await _user(session_factory, "+972501111111")
    assignment = await sessions.allocate_and_enqueue_create(user_id)
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE waha_commands SET attempts = :attempts"
                " WHERE session_name = :s"
            ),
            {"attempts": 5, "s": assignment.session_name},
        )
        await session.commit()
    claimed = await sessions.claim_for_worker("waha-1", max_attempts=5)
    assert claimed == []  # attempts already at cap → failed, not re-claimed
    commands = await sessions.active_commands(assignment.session_name)
    assert commands == []
    async with session_factory() as session:
        status = (
            await session.execute(
                text(
                    "SELECT status, last_error FROM waha_commands"
                    " WHERE session_name = :s"
                ),
                {"s": assignment.session_name},
            )
        ).one()
    assert status.status == WahaCommandStatus.FAILED
    assert status.last_error == "attempt limit reached"


async def test_complete_command_idempotent_and_worker_guarded(
    sessions: PostgresWahaSessionRepository,
    workers: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _register_worker(workers, worker_id="waha-1")
    user_id = await _user(session_factory, "+972501111111")
    assignment = await sessions.allocate_and_enqueue_create(user_id)
    command = (await sessions.active_commands(assignment.session_name))[0]
    # Foreign worker cannot complete it.
    assert (
        await sessions.complete_command(
            command.command_id, worker_id="waha-other", success=True
        )
        is False
    )
    assert (
        await sessions.complete_command(
            command.command_id, worker_id="waha-1", success=True
        )
        is True
    )
    # Duplicate result is acknowledged idempotently.
    assert (
        await sessions.complete_command(
            command.command_id, worker_id="waha-1", success=True
        )
        is True
    )
    # Unknown command id.
    assert (
        await sessions.complete_command(
            str(uuid.uuid4()), worker_id="waha-1", success=True
        )
        is False
    )
    async with session_factory() as session:
        row = (
            await session.execute(
                text("SELECT status FROM waha_commands WHERE command_id = :c"),
                {"c": uuid.UUID(command.command_id)},
            )
        ).scalar_one()
    assert row == WahaCommandStatus.COMPLETED


# --- lifecycle transitions -------------------------------------------------------------


async def _allocate_simple(
    sessions: PostgresWahaSessionRepository,
    workers: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[str, str]:
    await _register_worker(workers, worker_id="waha-1")
    user_id = await _user(session_factory, "+972503333331")
    assignment = await sessions.allocate_and_enqueue_create(user_id)
    return user_id, assignment.session_name


async def test_apply_lifecycle_first_event_applies_and_queues_effect(
    sessions: PostgresWahaSessionRepository,
    workers: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _user_id, session_name = await _allocate_simple(sessions, workers, session_factory)
    event_at = datetime(2026, 11, 26, 18, 0, 5, tzinfo=timezone.utc)
    outcome = await sessions.apply_lifecycle(
        session_name, raw_status="STARTING", mapped_status="connecting",
        event_at=event_at,
    )
    assert outcome is not None
    assert outcome.applied is True
    assert outcome.previous_status == "provisioning"
    assert outcome.event_seq == 1
    assert outcome.pending_effect is None
    assert outcome.connection_updated is False  # no whatsapp_connections row yet


async def test_apply_lifecycle_duplicate_event_keeps_pending_effect(
    sessions: PostgresWahaSessionRepository,
    workers: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _user_id, session_name = await _allocate_simple(sessions, workers, session_factory)
    event_at = datetime(2026, 11, 26, 18, 0, 5, tzinfo=timezone.utc)
    first = await sessions.apply_lifecycle(
        session_name, raw_status="SCAN_QR_CODE", mapped_status="pairing_required",
        event_at=event_at,
    )
    assert first is not None and first.pending_effect == (
        WahaLifecycleEffect.DELIVER_QR
    )
    duplicate = await sessions.apply_lifecycle(
        session_name, raw_status="SCAN_QR_CODE", mapped_status="pairing_required",
        event_at=event_at,
    )
    assert duplicate is not None
    assert duplicate.applied is False
    assert duplicate.pending_effect == WahaLifecycleEffect.DELIVER_QR
    assert duplicate.event_seq == first.event_seq


async def test_apply_lifecycle_stale_event_is_ignored(
    sessions: PostgresWahaSessionRepository,
    workers: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _user_id, session_name = await _allocate_simple(sessions, workers, session_factory)
    t1 = datetime(2026, 11, 26, 18, 0, 10, tzinfo=timezone.utc)
    t0 = datetime(2026, 11, 26, 18, 0, 5, tzinfo=timezone.utc)
    await sessions.apply_lifecycle(
        session_name, raw_status="WORKING", mapped_status="connected", event_at=t1
    )
    stale = await sessions.apply_lifecycle(
        session_name, raw_status="STARTING", mapped_status="connecting", event_at=t0
    )
    assert stale is not None and stale.applied is False
    row = await sessions.get(session_name)
    assert row is not None and row.status == "connected"


async def test_apply_lifecycle_newer_event_replaces_pending_effect(
    sessions: PostgresWahaSessionRepository,
    workers: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _user_id, session_name = await _allocate_simple(sessions, workers, session_factory)
    t1 = datetime(2026, 11, 26, 18, 0, 5, tzinfo=timezone.utc)
    t2 = datetime(2026, 11, 26, 18, 0, 9, tzinfo=timezone.utc)
    await sessions.apply_lifecycle(
        session_name, raw_status="SCAN_QR_CODE", mapped_status="pairing_required",
        event_at=t1,
    )
    outcome = await sessions.apply_lifecycle(
        session_name, raw_status="WORKING", mapped_status="connected", event_at=t2
    )
    assert outcome is not None
    assert outcome.pending_effect == WahaLifecycleEffect.CONFIRM_WORKING
    assert outcome.pending_effect_at == t2


async def test_apply_lifecycle_unknown_session_returns_none(
    sessions: PostgresWahaSessionRepository,
) -> None:
    assert (
        await sessions.apply_lifecycle(
            "echo-nope", raw_status="WORKING", mapped_status="connected",
            event_at=NOW,
        )
        is None
    )


async def test_apply_lifecycle_mirrors_status_to_existing_connection_row(
    sessions: PostgresWahaSessionRepository,
    workers: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id, session_name = await _allocate_simple(sessions, workers, session_factory)
    t1 = datetime(2026, 11, 26, 18, 0, 5, tzinfo=timezone.utc)
    early = await sessions.apply_lifecycle(
        session_name, raw_status="WORKING", mapped_status="connected", event_at=t1
    )
    assert early is not None and early.connection_updated is False

    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO whatsapp_connections"
                " (user_id, provider, provider_connection_id, credentials,"
                "  webhook_token_hash, connection_status)"
                " VALUES (:u, 'waha', :s, 'x'::bytea, 'y'::bytea, 'provisioning')"
            ),
            {"u": uuid.UUID(user_id), "s": session_name},
        )
        await session.commit()

    duplicate = await sessions.apply_lifecycle(
        session_name, raw_status="WORKING", mapped_status="connected", event_at=t1
    )
    assert duplicate is not None
    assert duplicate.applied is False
    assert duplicate.connection_updated is True
    async with session_factory() as session:
        status = (
            await session.execute(
                text(
                    "SELECT connection_status, provider_raw_status"
                    " FROM whatsapp_connections"
                    " WHERE provider = 'waha' AND provider_connection_id = :s"
                ),
                {"s": session_name},
            )
        ).one()
    assert status.connection_status == "connected"
    assert status.provider_raw_status == "WORKING"


# --- effects + QR ----------------------------------------------------------------------


async def test_clear_pending_effect_is_fenced(
    sessions: PostgresWahaSessionRepository,
    workers: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _user_id, session_name = await _allocate_simple(sessions, workers, session_factory)
    t1 = datetime(2026, 11, 26, 18, 0, 5, tzinfo=timezone.utc)
    await sessions.apply_lifecycle(
        session_name, raw_status="WORKING", mapped_status="connected", event_at=t1
    )
    assert (
        await sessions.clear_pending_effect(
            session_name, effect=WahaLifecycleEffect.CONFIRM_WORKING,
            effect_at=t1 + timedelta(seconds=99),
        )
        is False
    )
    assert (
        await sessions.clear_pending_effect(
            session_name, effect=WahaLifecycleEffect.CONFIRM_WORKING, effect_at=t1
        )
        is True
    )
    row = await sessions.get(session_name)
    assert row is not None and row.pending_effect is None


async def test_record_qr_stores_hash_and_image_and_clears_effect(
    sessions: PostgresWahaSessionRepository,
    workers: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _user_id, session_name = await _allocate_simple(sessions, workers, session_factory)
    t1 = datetime(2026, 11, 26, 18, 0, 5, tzinfo=timezone.utc)
    await sessions.apply_lifecycle(
        session_name, raw_status="SCAN_QR_CODE", mapped_status="pairing_required",
        event_at=t1,
    )
    assert (
        await sessions.record_qr(
            session_name, qr_hash="abc123", effect_at=t1 + timedelta(seconds=1)
        )
        is False
    )  # fenced: different effect_at
    assert (
        await sessions.record_qr(
            session_name, qr_hash="abc123", qr_image="cXItMQ==", effect_at=t1
        )
        is True
    )
    row = await sessions.get(session_name)
    assert row is not None
    assert row.last_qr_hash == "abc123"
    assert row.last_qr_image == "cXItMQ=="
    assert row.qr_updated_at is not None
    assert row.pending_effect is None


async def test_get_by_user_round_trip(
    sessions: PostgresWahaSessionRepository,
    workers: PostgresWahaWorkerRegistry,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id, session_name = await _allocate_simple(sessions, workers, session_factory)
    fetched = await sessions.get_by_user(user_id)
    assert fetched is not None
    assert fetched.session_name == session_name
    assert (
        await sessions.get_by_user("99999999-9999-9999-9999-999999999999") is None
    )


def test_effect_mapping() -> None:
    assert effect_for_waha_status("WORKING") == WahaLifecycleEffect.CONFIRM_WORKING
    assert effect_for_waha_status("SCAN_QR_CODE") == WahaLifecycleEffect.DELIVER_QR
    assert effect_for_waha_status("STOPPED") == (
        WahaLifecycleEffect.NOTIFY_DISCONNECTED
    )
    assert effect_for_waha_status("FAILED") == WahaLifecycleEffect.NOTIFY_DISCONNECTED
    assert effect_for_waha_status("STARTING") is None
    assert effect_for_waha_status("WHATEVER") is None
