from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from echo_v2.integrations.baileys.events import BaileysConnectionStateEvent
from echo_v2.persistence.baileys_events import ClaimedBaileysEvent
from echo_v2.ports.whatsapp import ConnectionStatus
from echo_v2.services.baileys_connection_state_consumer import (
    BaileysConnectionStateConsumer,
)


class FakeEventRepository:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.processed: list[str] = []
        self.failed: list[tuple[str, str, str]] = []
        self.calls = 0

    async def claim_batch(self, worker_id: str, **kwargs):
        self.calls += 1
        if self.calls > 1:
            return []
        event = BaileysConnectionStateEvent(
            event_id="state:c:1",
            inbox_id=1,
            connection_id="c",
            status=ConnectionStatus.PAIRING_REQUIRED,
            provider_raw_status="logged_out:401",
            timestamp=datetime.now(timezone.utc),
        )
        return [ClaimedBaileysEvent(event=event, worker_id=worker_id)]

    async def process_claimed_event(self, item, *, notification_message: str):
        if self.fail:
            raise RuntimeError("database unavailable")
        self.processed.append(item.event.event_id)
        return True

    async def mark_failed(self, event_id: str, worker_id: str, error: str):
        self.failed.append((event_id, worker_id, error))

    async def reconnect_child_for_event(
        self, connection_id: str, state_version: int | None
    ) -> str | None:
        return "child-1"


class ExplodingEventRepository(FakeEventRepository):
    def __init__(self) -> None:
        super().__init__()
        self.exploded = False

    async def claim_batch(self, worker_id: str, **kwargs):
        if not self.exploded:
            self.exploded = True
            raise RuntimeError("poll failed")
        return []


@pytest.mark.asyncio
async def test_run_once_processes_claimed_event() -> None:
    repository = FakeEventRepository()
    consumer = BaileysConnectionStateConsumer(repository, worker_id="worker-1")

    assert await consumer.run_once() == 1
    assert repository.processed == ["state:c:1"]


@pytest.mark.asyncio
async def test_connected_event_verifies_reconnect_before_durable_completion() -> None:
    repository = FakeEventRepository()
    order: list[str] = []
    repository.processed = order
    onboarding = AsyncMock()
    onboarding.handle_connection_established_by_id.side_effect = lambda child_id: order.append(
        f"verified:{child_id}"
    )
    consumer = BaileysConnectionStateConsumer(
        repository, worker_id="worker-1", onboarding_service=onboarding
    )
    original_claim_batch = repository.claim_batch

    async def connected_batch(worker_id: str, **kwargs):
        claimed = await original_claim_batch(worker_id, **kwargs)
        if claimed:
            event = claimed[0].event
            claimed[0] = ClaimedBaileysEvent(
                event=BaileysConnectionStateEvent(
                    event_id=event.event_id, inbox_id=event.inbox_id,
                    connection_id=event.connection_id, status=ConnectionStatus.CONNECTED,
                    provider_raw_status="open", timestamp=event.timestamp, state_version=1,
                ), worker_id=worker_id,
            )
        return claimed

    repository.claim_batch = connected_batch
    assert await consumer.run_once() == 1
    assert order == ["verified:child-1", "state:c:1"]


async def test_connected_verification_error_keeps_event_retryable() -> None:
    repository = FakeEventRepository()
    onboarding = AsyncMock()
    onboarding.handle_connection_established_by_id.side_effect = RuntimeError("verification unavailable")
    consumer = BaileysConnectionStateConsumer(
        repository, worker_id="worker-1", onboarding_service=onboarding
    )
    original_claim_batch = repository.claim_batch

    async def connected_batch(worker_id: str, **kwargs):
        claimed = await original_claim_batch(worker_id, **kwargs)
        if claimed:
            event = claimed[0].event
            claimed[0] = ClaimedBaileysEvent(
                event=BaileysConnectionStateEvent(
                    event_id=event.event_id, inbox_id=event.inbox_id,
                    connection_id=event.connection_id, status=ConnectionStatus.CONNECTED,
                    provider_raw_status="open", timestamp=event.timestamp, state_version=1,
                ), worker_id=worker_id,
            )
        return claimed

    repository.claim_batch = connected_batch
    assert await consumer.run_once() == 0
    assert repository.processed == []
    assert repository.failed == [("state:c:1", "worker-1", "verification unavailable")]


async def test_run_once_records_event_failure_and_continues() -> None:
    repository = FakeEventRepository(fail=True)
    consumer = BaileysConnectionStateConsumer(repository, worker_id="worker-1")

    assert await consumer.run_once() == 0
    assert repository.failed == [
        ("state:c:1", "worker-1", "database unavailable")
    ]


@pytest.mark.asyncio
async def test_run_once_returns_zero_when_no_events() -> None:
    repository = FakeEventRepository()
    consumer = BaileysConnectionStateConsumer(repository)

    await consumer.run_once()
    assert await consumer.run_once() == 0


@pytest.mark.asyncio
async def test_run_loop_recovers_from_poll_error() -> None:
    repository = ExplodingEventRepository()
    consumer = BaileysConnectionStateConsumer(repository, poll_interval_seconds=0.01)
    task = asyncio.create_task(consumer.run_loop())
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert repository.exploded is True


@pytest.mark.asyncio
async def test_run_loop_can_be_cancelled() -> None:
    repository = FakeEventRepository()
    consumer = BaileysConnectionStateConsumer(repository, poll_interval_seconds=0.01)
    task = asyncio.create_task(consumer.run_loop())
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
