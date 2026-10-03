from __future__ import annotations

import asyncio
from datetime import datetime, timezone

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
