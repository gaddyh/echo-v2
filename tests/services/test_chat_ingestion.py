"""ChatIngestionService tests (in-memory repos, no Docker needed)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.persistence.chat_repositories import (
    InMemoryChatStateRepository,
    InMemoryMessageRepository,
)
from echo_v2.ports.whatsapp import (
    ConnectionRef,
    MessageDirection,
    MessageKind,
    ProviderMessageEvent,
)
from echo_v2.services.chat_ingestion import ChatIngestionService

pytestmark = pytest.mark.asyncio


def _make_event(
    chat_id: str = "972501234567@c.us",
    provider_message_id: str = "msg-1",
    direction: MessageDirection = MessageDirection.INBOUND,
    text: str = "hello",
) -> ProviderMessageEvent:
    return ProviderMessageEvent(
        event_id=f"incomingMessageReceived:green:conn1:{provider_message_id}",
        connection=ConnectionRef(provider="green", provider_connection_id="conn1"),
        chat_id=chat_id,
        provider_message_id=provider_message_id,
        direction=direction,
        source=None,
        timestamp=datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc),
        kind=MessageKind.TEXT,
        text=text,
    )


def _make_service(
    *,
    quiet_period_seconds: float = 300.0,
    private_only: bool = True,
) -> ChatIngestionService:
    return ChatIngestionService(
        message_repo=InMemoryMessageRepository(),
        chat_state_repo=InMemoryChatStateRepository(),
        quiet_period_seconds=quiet_period_seconds,
        private_only=private_only,
    )


# --- New inbound message ---------------------------------------------------


async def test_inbound_new_message_saves_and_schedules():
    service = _make_service()
    event = _make_event()

    inserted = await service.ingest_message(
        event, user_id="user-1", connection_id="conn-uuid-1"
    )
    assert inserted is True

    chat = await service._chat_state_repo.get("user-1", "972501234567@c.us")
    assert chat is not None
    assert chat.activity_version == 1
    assert chat.last_direction == MessageDirection.INBOUND
    assert chat.next_analysis_at is not None


async def test_inbound_next_analysis_at_is_now_plus_quiet_period():
    service = _make_service(quiet_period_seconds=600)
    event = _make_event()

    before = datetime.now(timezone.utc)
    await service.ingest_message(
        event, user_id="user-1", connection_id="conn-uuid-1"
    )
    after = datetime.now(timezone.utc)

    chat = await service._chat_state_repo.get("user-1", "972501234567@c.us")
    assert chat is not None
    assert chat.next_analysis_at is not None
    # next_analysis_at should be ~now + 600s
    min_expected = before + timedelta(seconds=600)
    max_expected = after + timedelta(seconds=600)
    assert min_expected <= chat.next_analysis_at <= max_expected


# --- Duplicate message -----------------------------------------------------


async def test_duplicate_message_is_noop():
    service = _make_service()
    event = _make_event()

    first = await service.ingest_message(
        event, user_id="user-1", connection_id="conn-uuid-1"
    )
    second = await service.ingest_message(
        event, user_id="user-1", connection_id="conn-uuid-1"
    )
    assert first is True
    assert second is False

    chat = await service._chat_state_repo.get("user-1", "972501234567@c.us")
    assert chat is not None
    assert chat.activity_version == 1  # not incremented on duplicate


# --- Outbound message ------------------------------------------------------


async def test_outbound_message_cancels_analysis():
    service = _make_service()
    inbound = _make_event(direction=MessageDirection.INBOUND)
    outbound = _make_event(
        direction=MessageDirection.OUTBOUND,
        provider_message_id="msg-2",
    )

    await service.ingest_message(
        inbound, user_id="user-1", connection_id="conn-uuid-1"
    )
    await service.ingest_message(
        outbound, user_id="user-1", connection_id="conn-uuid-1"
    )

    chat = await service._chat_state_repo.get("user-1", "972501234567@c.us")
    assert chat is not None
    assert chat.activity_version == 2
    assert chat.last_direction == MessageDirection.OUTBOUND
    assert chat.next_analysis_at is None


# --- Private-only filtering ------------------------------------------------


async def test_group_chat_skipped_when_private_only_true():
    service = _make_service(private_only=True)
    event = _make_event(chat_id="group-123@g.us")

    inserted = await service.ingest_message(
        event, user_id="user-1", connection_id="conn-uuid-1"
    )
    assert inserted is False

    chat = await service._chat_state_repo.get("user-1", "group-123@g.us")
    assert chat is None


async def test_group_chat_processed_when_private_only_false():
    service = _make_service(private_only=False)
    event = _make_event(chat_id="group-123@g.us")

    inserted = await service.ingest_message(
        event, user_id="user-1", connection_id="conn-uuid-1"
    )
    assert inserted is True

    chat = await service._chat_state_repo.get("user-1", "group-123@g.us")
    assert chat is not None
    assert chat.activity_version == 1


async def test_private_chat_processed_when_private_only_true():
    service = _make_service(private_only=True)
    event = _make_event(chat_id="972501234567@c.us")

    inserted = await service.ingest_message(
        event, user_id="user-1", connection_id="conn-uuid-1"
    )
    assert inserted is True
