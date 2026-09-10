"""In-memory chat repository contract tests (no Docker needed).

These tests verify the in-memory implementations satisfy the same
behavioral contract as the Postgres implementations. The Postgres
tests in ``test_postgres_chat.py`` run the same cases against a real
database via testcontainers.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.domain.chat import Message
from echo_v2.persistence.chat_repositories import (
    InMemoryChatStateRepository,
    InMemoryMessageRepository,
)
from echo_v2.ports.whatsapp import MessageDirection, MessageKind

pytestmark = pytest.mark.asyncio


def _make_message(
    *,
    user_id: str = "user-1",
    connection_id: str = "conn-1",
    chat_id: str = "972501234567@c.us",
    provider_message_id: str = "msg-1",
    direction: MessageDirection = MessageDirection.INBOUND,
) -> Message:
    return Message(
        id=str(uuid.uuid4()),
        user_id=user_id,
        connection_id=connection_id,
        chat_id=chat_id,
        provider_message_id=provider_message_id,
        direction=direction,
        sender_id=None,
        timestamp=datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc),
        message_type=MessageKind.TEXT.value,
        text="hello",
    )


# --- MessageRepository.save ------------------------------------------------


async def test_message_save_first_returns_true():
    repo = InMemoryMessageRepository()
    inserted = await repo.save(_make_message())
    assert inserted is True


async def test_message_save_duplicate_returns_false():
    repo = InMemoryMessageRepository()
    msg = _make_message()
    first = await repo.save(msg)
    second = await repo.save(msg)
    assert first is True
    assert second is False


async def test_message_save_different_provider_ids_both_succeed():
    repo = InMemoryMessageRepository()
    a = await repo.save(_make_message(provider_message_id="msg-a"))
    b = await repo.save(_make_message(provider_message_id="msg-b"))
    assert a is True and b is True


async def test_message_save_same_provider_id_different_connections_both_succeed():
    repo = InMemoryMessageRepository()
    a = await repo.save(_make_message(connection_id="conn-a"))
    b = await repo.save(_make_message(connection_id="conn-b"))
    assert a is True and b is True


# --- ChatStateRepository.get -----------------------------------------------


async def test_chat_get_returns_none_for_unknown():
    repo = InMemoryChatStateRepository()
    assert await repo.get("user-1", "unknown@c.us") is None


async def test_chat_get_returns_state_after_upsert():
    repo = InMemoryChatStateRepository()
    now = datetime.now(timezone.utc)
    await repo.upsert_on_message(
        user_id="user-1",
        chat_id="972501234567@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=now,
        next_analysis_at=now + timedelta(minutes=5),
    )
    chat = await repo.get("user-1", "972501234567@c.us")
    assert chat is not None
    assert chat.activity_version == 1


# --- ChatStateRepository.upsert_on_message --------------------------------


async def test_chat_upsert_inbound_sets_next_analysis_at():
    repo = InMemoryChatStateRepository()
    now = datetime.now(timezone.utc)
    due = now + timedelta(minutes=5)
    chat = await repo.upsert_on_message(
        user_id="user-1",
        chat_id="972501234567@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=now,
        next_analysis_at=due,
    )
    assert chat.activity_version == 1
    assert chat.last_direction == MessageDirection.INBOUND
    assert chat.next_analysis_at == due


async def test_chat_upsert_outbound_sets_next_analysis_at_null():
    repo = InMemoryChatStateRepository()
    now = datetime.now(timezone.utc)
    chat = await repo.upsert_on_message(
        user_id="user-1",
        chat_id="972501234567@c.us",
        direction=MessageDirection.OUTBOUND,
        observed_at=now,
        next_analysis_at=None,
    )
    assert chat.next_analysis_at is None


async def test_chat_upsert_increments_activity_version():
    repo = InMemoryChatStateRepository()
    now = datetime.now(timezone.utc)
    due = now + timedelta(minutes=5)
    chat1 = await repo.upsert_on_message(
        user_id="user-1",
        chat_id="972501234567@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=now,
        next_analysis_at=due,
    )
    chat2 = await repo.upsert_on_message(
        user_id="user-1",
        chat_id="972501234567@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=now,
        next_analysis_at=due,
    )
    assert chat1.activity_version == 1
    assert chat2.activity_version == 2


async def test_chat_upsert_outbound_after_inbound_cancels():
    repo = InMemoryChatStateRepository()
    now = datetime.now(timezone.utc)
    due = now + timedelta(minutes=5)
    chat1 = await repo.upsert_on_message(
        user_id="user-1",
        chat_id="972501234567@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=now,
        next_analysis_at=due,
    )
    chat2 = await repo.upsert_on_message(
        user_id="user-1",
        chat_id="972501234567@c.us",
        direction=MessageDirection.OUTBOUND,
        observed_at=now,
        next_analysis_at=None,
    )
    assert chat1.next_analysis_at is not None
    assert chat2.next_analysis_at is None


# --- ChatStateRepository.list_due ------------------------------------------


async def test_list_due_returns_due_inbound_chats():
    repo = InMemoryChatStateRepository()
    now = datetime.now(timezone.utc)
    past = now - timedelta(minutes=10)
    await repo.upsert_on_message(
        user_id="user-1",
        chat_id="972501234567@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=past,
        next_analysis_at=past + timedelta(minutes=5),
    )
    due = await repo.list_due(now)
    assert len(due) == 1
    assert due[0].chat_id == "972501234567@c.us"


async def test_list_due_excludes_not_yet_due():
    repo = InMemoryChatStateRepository()
    now = datetime.now(timezone.utc)
    await repo.upsert_on_message(
        user_id="user-1",
        chat_id="972501234567@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=now,
        next_analysis_at=now + timedelta(minutes=5),
    )
    assert await repo.list_due(now) == []


async def test_list_due_excludes_outbound():
    repo = InMemoryChatStateRepository()
    now = datetime.now(timezone.utc)
    past = now - timedelta(minutes=10)
    await repo.upsert_on_message(
        user_id="user-1",
        chat_id="972501234567@c.us",
        direction=MessageDirection.OUTBOUND,
        observed_at=past,
        next_analysis_at=None,
    )
    assert await repo.list_due(now) == []


async def test_list_due_excludes_already_processed():
    repo = InMemoryChatStateRepository()
    now = datetime.now(timezone.utc)
    past = now - timedelta(minutes=10)
    chat = await repo.upsert_on_message(
        user_id="user-1",
        chat_id="972501234567@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=past,
        next_analysis_at=past + timedelta(minutes=5),
    )
    await repo.mark_processed("user-1", "972501234567@c.us", chat.activity_version)
    assert await repo.list_due(now) == []


async def test_list_due_respects_limit():
    repo = InMemoryChatStateRepository()
    now = datetime.now(timezone.utc)
    past = now - timedelta(minutes=10)
    for i in range(5):
        await repo.upsert_on_message(
            user_id="user-1",
            chat_id=f"97250123456{i:02d}@c.us",
            direction=MessageDirection.INBOUND,
            observed_at=past,
            next_analysis_at=past + timedelta(minutes=5),
        )
    due = await repo.list_due(now, limit=3)
    assert len(due) == 3


async def test_list_due_orders_oldest_first():
    repo = InMemoryChatStateRepository()
    now = datetime.now(timezone.utc)
    # Three chats with different next_analysis_at times (all in the past).
    # offset=5 means now-5min (oldest), offset=1 means now-1min (newest).
    for i, offset in enumerate([5, 1, 3]):
        await repo.upsert_on_message(
            user_id="user-1",
            chat_id=f"chat-{i}@c.us",
            direction=MessageDirection.INBOUND,
            observed_at=now - timedelta(minutes=10),
            next_analysis_at=now - timedelta(minutes=offset),
        )
    due = await repo.list_due(now)
    assert len(due) == 3
    # Ordered by next_analysis_at ascending (oldest/most-past due first).
    assert due[0].chat_id == "chat-0@c.us"  # now-5min
    assert due[1].chat_id == "chat-2@c.us"  # now-3min
    assert due[2].chat_id == "chat-1@c.us"  # now-1min


# --- ChatStateRepository.mark_processed ------------------------------------


async def test_mark_processed_succeeds_when_version_matches():
    repo = InMemoryChatStateRepository()
    now = datetime.now(timezone.utc)
    past = now - timedelta(minutes=10)
    chat = await repo.upsert_on_message(
        user_id="user-1",
        chat_id="972501234567@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=past,
        next_analysis_at=past + timedelta(minutes=5),
    )
    updated = await repo.mark_processed("user-1", "972501234567@c.us", chat.activity_version)
    assert updated is True
    fresh = await repo.get("user-1", "972501234567@c.us")
    assert fresh is not None
    assert fresh.last_processed_version == chat.activity_version
    assert fresh.next_analysis_at is None


async def test_mark_processed_fails_when_version_changed():
    repo = InMemoryChatStateRepository()
    now = datetime.now(timezone.utc)
    past = now - timedelta(minutes=10)
    chat_v1 = await repo.upsert_on_message(
        user_id="user-1",
        chat_id="972501234567@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=past,
        next_analysis_at=past + timedelta(minutes=5),
    )
    chat_v2 = await repo.upsert_on_message(
        user_id="user-1",
        chat_id="972501234567@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=now,
        next_analysis_at=now + timedelta(minutes=5),
    )
    updated = await repo.mark_processed("user-1", "972501234567@c.us", chat_v1.activity_version)
    assert updated is False
    fresh = await repo.get("user-1", "972501234567@c.us")
    assert fresh is not None
    assert fresh.next_analysis_at is not None
    assert fresh.activity_version == chat_v2.activity_version


async def test_mark_processed_fails_for_unknown_chat():
    repo = InMemoryChatStateRepository()
    updated = await repo.mark_processed("unknown", "unknown@c.us", 1)
    assert updated is False
