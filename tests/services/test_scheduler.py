"""Tests for the Scheduler: claim+execute, stale recovery, loop behavior."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.domain.scheduling import (
    ScheduledAction,
    ScheduledActionStatus,
    ScheduledActionType,
)
from echo_v2.integrations.green.client import GreenApiIndeterminateError
from echo_v2.observability import InMemoryEventSink
from echo_v2.persistence.scheduled_actions import InMemoryScheduledActionRepository
from echo_v2.persistence.whatsapp_connections import (
    InMemoryWhatsAppConnectionRepository,
    StoredConnection,
)
from echo_v2.ports.whatsapp import (
    ConnectionRef,
    ConnectionStatus,
    ProviderCredentials,
)
from echo_v2.runtime.idempotency import InMemoryIdempotencyStore
from echo_v2.services.scheduling import SchedulingService
from echo_v2.services.scheduler import Scheduler


# --- helpers ---------------------------------------------------------------


class FakeMessaging:
    def __init__(self, *, msg_id: str = "MSG_1", fail_with: Exception | None = None):
        self.msg_id = msg_id
        self.fail_with = fail_with
        self.send_count = 0

    async def send_message(self, connection, chat_id, message) -> str:
        self.send_count += 1
        if self.fail_with is not None:
            raise self.fail_with
        return self.msg_id


def _make_scheduler(
    *,
    messaging: FakeMessaging | None = None,
    lease_seconds: float = 300.0,
    poll_interval: float = 1.0,
    user_id: str = "user-1",
) -> tuple[Scheduler, InMemoryScheduledActionRepository, FakeMessaging]:
    messaging = messaging or FakeMessaging()
    action_repo = InMemoryScheduledActionRepository()
    conn_repo = InMemoryWhatsAppConnectionRepository()
    conn_repo._by_ref[("green", "123")] = StoredConnection(
        user_id=user_id,
        ref=ConnectionRef("green", "123"),
        credentials=ProviderCredentials(b"api-tok"),
        webhook_token_hash=b"\x00" * 32,
        status=ConnectionStatus.CONNECTED,
    )
    conn_repo._by_user[user_id] = ("green", "123")
    service = SchedulingService(
        action_repo=action_repo,
        connection_repo=conn_repo,
        messaging=messaging,
        idempotency_store=InMemoryIdempotencyStore(),
        event_sink=InMemoryEventSink(),
    )
    scheduler = Scheduler(
        service,
        action_repo,
        lease_seconds=lease_seconds,
        poll_interval_seconds=poll_interval,
    )
    return scheduler, action_repo, messaging


def _due_action(
    *,
    id: str = "act-1",
    user_id: str = "user-1",
    status: ScheduledActionStatus = ScheduledActionStatus.PENDING,
    claimed_at: datetime | None = None,
    minutes_from_now: float = -1,
) -> ScheduledAction:
    return ScheduledAction(
        id=id,
        user_id=user_id,
        type=ScheduledActionType.SEND_WHATSAPP_MESSAGE,
        execute_at_utc=datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now),
        timezone="Asia/Jerusalem",
        status=status,
        payload={"chat_id": "972@c.us", "message": "hello"},
        claimed_at=claimed_at,
    )


# --- recover ---------------------------------------------------------------


async def test_recover_resets_stale_in_progress():
    scheduler, action_repo, _ = _make_scheduler(lease_seconds=60)
    old_claim = datetime.now(timezone.utc) - timedelta(seconds=120)
    await action_repo.save(_due_action(status=ScheduledActionStatus.IN_PROGRESS, claimed_at=old_claim))
    await action_repo.save(
        _due_action(
            id="act-2",
            status=ScheduledActionStatus.IN_PROGRESS,
            claimed_at=datetime.now(timezone.utc) - timedelta(seconds=10),
        )
    )

    recovered = await scheduler.recover()
    assert recovered == 1
    assert (await action_repo.get("act-1")).status is ScheduledActionStatus.PENDING
    assert (await action_repo.get("act-2")).status is ScheduledActionStatus.IN_PROGRESS


async def test_recover_returns_zero_when_none_stale():
    scheduler, action_repo, _ = _make_scheduler()
    await action_repo.save(_due_action())
    assert await scheduler.recover() == 0


# --- run_once --------------------------------------------------------------


async def test_run_once_claims_and_executes_due_action():
    scheduler, action_repo, messaging = _make_scheduler()
    await action_repo.save(_due_action())

    processed = await scheduler.run_once()
    assert processed is True
    assert messaging.send_count == 1
    action = await action_repo.get("act-1")
    assert action.status is ScheduledActionStatus.SUCCEEDED


async def test_run_once_returns_false_when_nothing_due():
    scheduler, action_repo, _ = _make_scheduler()
    # Future action — not due yet.
    await action_repo.save(_due_action(minutes_from_now=60))

    processed = await scheduler.run_once()
    assert processed is False


async def test_run_once_returns_false_when_empty():
    scheduler, _, _ = _make_scheduler()
    assert await scheduler.run_once() is False


async def test_run_once_handles_indeterminate_and_continues():
    messaging = FakeMessaging(fail_with=GreenApiIndeterminateError("timeout"))
    scheduler, action_repo, messaging = _make_scheduler(messaging=messaging)
    await action_repo.save(_due_action())

    processed = await scheduler.run_once()
    assert processed is True
    action = await action_repo.get("act-1")
    assert action.status is ScheduledActionStatus.INDETERMINATE


async def test_run_once_handles_permanent_failure_and_continues():
    from echo_v2.integrations.green.client import GreenApiError

    messaging = FakeMessaging(fail_with=GreenApiError("bad"))
    scheduler, action_repo, _ = _make_scheduler(messaging=messaging)
    await action_repo.save(_due_action())

    processed = await scheduler.run_once()
    assert processed is True
    action = await action_repo.get("act-1")
    assert action.status is ScheduledActionStatus.FAILED


async def test_run_once_processes_multiple_actions_sequentially():
    scheduler, action_repo, messaging = _make_scheduler()
    await action_repo.save(_due_action(id="a1", minutes_from_now=-2))
    await action_repo.save(_due_action(id="a2", minutes_from_now=-1))

    assert await scheduler.run_once() is True
    assert await scheduler.run_once() is True
    assert await scheduler.run_once() is False  # nothing left
    assert messaging.send_count == 2
    assert (await action_repo.get("a1")).status is ScheduledActionStatus.SUCCEEDED
    assert (await action_repo.get("a2")).status is ScheduledActionStatus.SUCCEEDED


# --- run_loop --------------------------------------------------------------


async def test_run_loop_executes_then_idles():
    """run_loop processes due actions, then idles until cancelled."""
    scheduler, action_repo, messaging = _make_scheduler(poll_interval=0.01)
    await action_repo.save(_due_action())

    # Run the loop briefly, then cancel.
    task = asyncio.create_task(scheduler.run_loop())
    # Give it time to process the action and start idling.
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert messaging.send_count == 1
    assert (await action_repo.get("act-1")).status is ScheduledActionStatus.SUCCEEDED


async def test_run_loop_continues_after_error():
    """An unexpected error in the loop should not kill the scheduler."""
    scheduler, action_repo, messaging = _make_scheduler(poll_interval=0.01)
    await action_repo.save(_due_action())

    # Patch run_once to raise once, then work.
    original = scheduler.run_once
    call_count = 0

    async def flaky_run_once():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("unexpected boom")
        return await original()

    scheduler.run_once = flaky_run_once  # type: ignore[method-assign]

    task = asyncio.create_task(scheduler.run_loop())
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The loop survived the error and eventually processed the action.
    assert messaging.send_count == 1
