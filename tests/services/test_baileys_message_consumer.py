from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from echo_v2.integrations.baileys.events import BaileysConnectionStateEvent
from echo_v2.persistence.baileys_events import BaileysMessageEnvelope
from echo_v2.persistence.chat_repositories import (
    InMemoryChatStateRepository,
    InMemoryIngestionRepository,
    InMemoryMessageRepository,
)
from echo_v2.ports.whatsapp import ConnectionStatus
from echo_v2.services.baileys_message_consumer import BaileysMessageConsumer
from echo_v2.services.chat_ingestion import ChatIngestionService


class FakeRepository:
    def __init__(
        self,
        envelope: BaileysMessageEnvelope,
        *,
        resolve_result: tuple[str, str] | None = ("user-1", "connection-db-id"),
        complete_result: bool = True,
    ) -> None:
        self.envelope = envelope
        self.resolve_result = resolve_result
        self.complete_result = complete_result
        self.completed: list[str] = []
        self.failed: list[tuple[str, str, str]] = []

    async def claim_batch(self, worker_id: str, **kwargs: object):
        return [type("Claimed", (), {"event": self.envelope, "worker_id": worker_id})()]

    async def resolve_message_user(self, event):
        return self.resolve_result

    async def complete_claimed_event(self, claimed):
        self.completed.append(claimed.event.event_id)
        return self.complete_result

    async def mark_failed(self, event_id: str, worker_id: str, error: str) -> None:
        self.failed.append((event_id, worker_id, error))


def _payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_type": "message",
        "event_id": "message:connection:message:in",
        "provider": "baileys",
        "connection_id": "connection",
        "chat_id": "15551234567@s.whatsapp.net",
        "is_group": False,
        "provider_message_id": "message",
        "direction": "inbound",
        "source": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "kind": "text",
        "text": "hello",
    }


async def test_message_consumer_ingests_and_completes_event() -> None:
    messages = InMemoryMessageRepository()
    chats = InMemoryChatStateRepository()
    ingestion = ChatIngestionService(
        InMemoryIngestionRepository(messages, chats),
        quiet_period_seconds=300,
        private_only=True,
    )
    envelope = BaileysMessageEnvelope(
        event_id="message:connection:message:in",
        inbox_id=1,
        connection_id="connection",
        payload=_payload(),
    )
    repository = FakeRepository(envelope)

    processed = await BaileysMessageConsumer(
        repository,  # type: ignore[arg-type]
        ingestion,
        worker_id="worker-1",
    ).run_once()

    assert processed == 1
    assert repository.completed == [envelope.event_id]
    assert repository.failed == []
    stored = await messages.list_recent_for_chat(
        user_id="user-1", chat_id="15551234567@s.whatsapp.net"
    )
    assert len(stored) == 1
    assert stored[0].text == "hello"
    state = await chats.get("user-1", "15551234567@s.whatsapp.net")
    assert state is not None
    assert state.activity_version == 1
    assert state.next_analysis_at is not None


async def test_message_consumer_ignores_state_envelope_as_failure() -> None:
    event = BaileysConnectionStateEvent(
        event_id="state:connection:1",
        inbox_id=4,
        connection_id="connection",
        status=ConnectionStatus.CONNECTED,
        provider_raw_status="open",
        timestamp=datetime.now(timezone.utc),
    )
    repository = FakeRepository(BaileysMessageEnvelope("message", 4, "connection", _payload()))
    repository.envelope = event  # type: ignore[assignment]
    ingestion = ChatIngestionService(
        InMemoryIngestionRepository(InMemoryMessageRepository(), InMemoryChatStateRepository()),
        private_only=True,
    )

    assert await BaileysMessageConsumer(repository, ingestion, worker_id="worker-1").run_once() == 0  # type: ignore[arg-type]
    assert repository.failed and "connection-state" in repository.failed[0][2]


async def test_message_consumer_loop_sleeps_and_propagates_cancel() -> None:
    envelope = BaileysMessageEnvelope("message", 5, "connection", _payload())
    repository = FakeRepository(envelope)
    ingestion = ChatIngestionService(
        InMemoryIngestionRepository(InMemoryMessageRepository(), InMemoryChatStateRepository()),
        private_only=True,
    )
    consumer = BaileysMessageConsumer(repository, ingestion, poll_interval_seconds=0)
    calls = 0

    async def run_once() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("poll failed")
        if calls == 2:
            return 1
        raise asyncio.CancelledError

    consumer.run_once = run_once  # type: ignore[method-assign]
    try:
        await consumer.run_loop()
    except asyncio.CancelledError:
        pass
    assert calls == 3


async def test_message_consumer_marks_unknown_connection_failed() -> None:
    envelope = BaileysMessageEnvelope(
        event_id="message:unknown:message:in",
        inbox_id=2,
        connection_id="unknown",
        payload=_payload(),
    )
    repository = FakeRepository(envelope, resolve_result=None)
    ingestion = ChatIngestionService(
        InMemoryIngestionRepository(InMemoryMessageRepository(), InMemoryChatStateRepository()),
        private_only=True,
    )

    assert await BaileysMessageConsumer(repository, ingestion, worker_id="worker-1").run_once() == 0  # type: ignore[arg-type]
    assert repository.completed == []
    assert repository.failed and "unknown Echo" in repository.failed[0][2]


async def test_message_consumer_marks_lost_completion_failed() -> None:
    envelope = BaileysMessageEnvelope(
        event_id="message:connection:message:in",
        inbox_id=3,
        connection_id="connection",
        payload=_payload(),
    )
    repository = FakeRepository(envelope, complete_result=False)
    ingestion = ChatIngestionService(
        InMemoryIngestionRepository(InMemoryMessageRepository(), InMemoryChatStateRepository()),
        private_only=True,
    )

    assert await BaileysMessageConsumer(repository, ingestion, worker_id="worker-1").run_once() == 0  # type: ignore[arg-type]
    assert repository.failed and "claim was lost" in repository.failed[0][2]
