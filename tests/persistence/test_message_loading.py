"""Tests for MessageRepository.list_for_analysis (in-memory)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.domain.chat import Message
from echo_v2.persistence.chat_repositories import InMemoryMessageRepository
from echo_v2.ports.whatsapp import MessageDirection

pytestmark = pytest.mark.asyncio

BASE_TIME = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)


def _make_message(
    *,
    direction: MessageDirection,
    text: str,
    offset_minutes: int,
    user_id: str = "user-1",
    chat_id: str = "972501234567@c.us",
    connection_id: str = "conn-1",
) -> Message:
    return Message(
        id=str(uuid.uuid4()),
        user_id=user_id,
        connection_id=connection_id,
        chat_id=chat_id,
        provider_message_id=str(uuid.uuid4()),
        direction=direction,
        sender_id=None,
        timestamp=BASE_TIME + timedelta(minutes=offset_minutes),
        message_type="text",
        text=text,
    )


async def _seed_conversation(repo, messages):
    for m in messages:
        await repo.save(m)


# --- No outbound → last N messages ------------------------------------------


async def test_no_outbound_returns_last_n():
    repo = InMemoryMessageRepository()
    msgs = [_make_message(direction=MessageDirection.INBOUND, text=f"msg-{i}", offset_minutes=i) for i in range(25)]
    for m in msgs:
        await repo.save(m)

    result = await repo.list_for_analysis(
        user_id="user-1", chat_id="972501234567@c.us", max_no_outbound=20,
    )
    assert len(result) == 20
    # Ascending order
    assert result[0].text == "msg-5"
    assert result[-1].text == "msg-24"


async def test_no_outbound_fewer_than_max_returns_all():
    repo = InMemoryMessageRepository()
    msgs = [_make_message(direction=MessageDirection.INBOUND, text=f"msg-{i}", offset_minutes=i) for i in range(3)]
    for m in msgs:
        await repo.save(m)

    result = await repo.list_for_analysis(
        user_id="user-1", chat_id="972501234567@c.us", max_no_outbound=20,
    )
    assert len(result) == 3


# --- With outbound → context + all after ------------------------------------


async def test_with_outbound_returns_context_and_after():
    repo = InMemoryMessageRepository()
    msgs = [
        _make_message(direction=MessageDirection.INBOUND, text="ctx-1", offset_minutes=0),
        _make_message(direction=MessageDirection.INBOUND, text="ctx-2", offset_minutes=1),
        _make_message(direction=MessageDirection.INBOUND, text="ctx-3", offset_minutes=2),
        _make_message(direction=MessageDirection.INBOUND, text="ctx-4", offset_minutes=3),
        _make_message(direction=MessageDirection.INBOUND, text="ctx-5", offset_minutes=4),
        _make_message(direction=MessageDirection.INBOUND, text="ctx-6", offset_minutes=5),
        _make_message(direction=MessageDirection.OUTBOUND, text="reply", offset_minutes=6),
        _make_message(direction=MessageDirection.INBOUND, text="after-1", offset_minutes=7),
        _make_message(direction=MessageDirection.INBOUND, text="after-2", offset_minutes=8),
    ]
    for m in msgs:
        await repo.save(m)

    result = await repo.list_for_analysis(
        user_id="user-1", chat_id="972501234567@c.us", context_messages=5,
    )
    # 5 context (ctx-2..ctx-6) + outbound + 2 after = 8
    assert len(result) == 8
    assert result[0].text == "ctx-2"
    assert result[-1].text == "after-2"


async def test_context_messages_smaller_than_available():
    repo = InMemoryMessageRepository()
    msgs = [
        _make_message(direction=MessageDirection.INBOUND, text="pre-1", offset_minutes=0),
        _make_message(direction=MessageDirection.OUTBOUND, text="reply", offset_minutes=1),
        _make_message(direction=MessageDirection.INBOUND, text="after", offset_minutes=2),
    ]
    for m in msgs:
        await repo.save(m)

    result = await repo.list_for_analysis(
        user_id="user-1", chat_id="972501234567@c.us", context_messages=5,
    )
    # Only 1 message before outbound, so all 3 returned
    assert len(result) == 3


# --- Empty chat -------------------------------------------------------------


async def test_empty_chat_returns_empty():
    repo = InMemoryMessageRepository()
    result = await repo.list_for_analysis(
        user_id="user-1", chat_id="972501234567@c.us",
    )
    assert result == []


# --- Filters by user_id and chat_id -----------------------------------------


async def test_filters_by_user_and_chat():
    repo = InMemoryMessageRepository()
    msgs_user1 = [
        _make_message(direction=MessageDirection.INBOUND, text="u1-msg", offset_minutes=0),
    ]
    msgs_user2 = [
        _make_message(
            direction=MessageDirection.INBOUND,
            text="u2-msg",
            offset_minutes=0,
            user_id="user-2",
        ),
    ]
    for m in msgs_user1 + msgs_user2:
        await repo.save(m)

    result = await repo.list_for_analysis(
        user_id="user-1", chat_id="972501234567@c.us",
    )
    assert len(result) == 1
    assert result[0].text == "u1-msg"


# --- Multiple outbounds → only last one matters -----------------------------


async def test_multiple_outbounds_uses_last():
    repo = InMemoryMessageRepository()
    msgs = [
        _make_message(direction=MessageDirection.INBOUND, text="old-1", offset_minutes=0),
        _make_message(direction=MessageDirection.OUTBOUND, text="old-reply", offset_minutes=1),
        _make_message(direction=MessageDirection.INBOUND, text="old-2", offset_minutes=2),
        _make_message(direction=MessageDirection.INBOUND, text="ctx-1", offset_minutes=3),
        _make_message(direction=MessageDirection.INBOUND, text="ctx-2", offset_minutes=4),
        _make_message(direction=MessageDirection.OUTBOUND, text="new-reply", offset_minutes=5),
        _make_message(direction=MessageDirection.INBOUND, text="after", offset_minutes=6),
    ]
    for m in msgs:
        await repo.save(m)

    result = await repo.list_for_analysis(
        user_id="user-1", chat_id="972501234567@c.us", context_messages=2,
    )
    # 2 context (ctx-1, ctx-2) + new-reply + after = 4
    assert len(result) == 4
    assert result[0].text == "ctx-1"
    assert result[1].text == "ctx-2"
    assert result[2].text == "new-reply"
    assert result[3].text == "after"
