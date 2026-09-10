"""Postgres-backed chat ingestion repository tests (requires Docker)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.domain.chat import Message
from echo_v2.ports.whatsapp import MessageDirection, MessageKind
from tests.persistence.conftest import insert_user

pytestmark = pytest.mark.asyncio


async def _seed_connection(session_factory, user_id: str, provider_id: str = "111") -> str:
    """Insert a whatsapp_connections row and return its id."""
    from sqlalchemy import text

    async with session_factory() as session:
        result = await session.execute(
            text(
                "INSERT INTO whatsapp_connections "
                "(user_id, provider, provider_connection_id, credentials, "
                " webhook_token_hash, connection_status) "
                "VALUES (:uid, 'green', :pid, :creds, :wh, 'connected') "
                "RETURNING id"
            ),
            {
                "uid": user_id,
                "pid": provider_id,
                "creds": b"encrypted",
                "wh": b"hash",
            },
        )
        conn_id = str(result.scalar_one())
        await session.commit()
    return conn_id


def _make_message(
    user_id: str,
    connection_id: str,
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


async def test_message_save_first_returns_true(messages_repo, session_factory):
    user_id = await insert_user(session_factory)
    conn_id = await _seed_connection(session_factory, user_id)

    msg = _make_message(user_id, conn_id)
    inserted = await messages_repo.save(msg)
    assert inserted is True


async def test_message_save_duplicate_returns_false(messages_repo, session_factory):
    user_id = await insert_user(session_factory)
    conn_id = await _seed_connection(session_factory, user_id)

    msg = _make_message(user_id, conn_id)
    first = await messages_repo.save(msg)
    second = await messages_repo.save(msg)
    assert first is True
    assert second is False


async def test_message_save_different_provider_ids_both_succeed(
    messages_repo, session_factory
):
    user_id = await insert_user(session_factory)
    conn_id = await _seed_connection(session_factory, user_id)

    msg_a = _make_message(user_id, conn_id, provider_message_id="msg-a")
    msg_b = _make_message(user_id, conn_id, provider_message_id="msg-b")
    a = await messages_repo.save(msg_a)
    b = await messages_repo.save(msg_b)
    assert a is True and b is True


# --- ChatStateRepository.upsert_on_message --------------------------------


async def test_chat_upsert_inbound_sets_next_analysis_at(
    chat_state_repo, session_factory
):
    user_id = await insert_user(session_factory)
    now = datetime.now(timezone.utc)
    due = now + timedelta(minutes=5)

    chat = await chat_state_repo.upsert_on_message(
        user_id=user_id,
        chat_id="972501234567@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=now,
        next_analysis_at=due,
    )
    assert chat.activity_version == 1
    assert chat.last_direction == MessageDirection.INBOUND
    assert chat.last_message_at == now
    assert chat.next_analysis_at == due
    assert chat.last_processed_version == 0


async def test_chat_upsert_outbound_sets_next_analysis_at_null(
    chat_state_repo, session_factory
):
    user_id = await insert_user(session_factory)
    now = datetime.now(timezone.utc)

    chat = await chat_state_repo.upsert_on_message(
        user_id=user_id,
        chat_id="972501234567@c.us",
        direction=MessageDirection.OUTBOUND,
        observed_at=now,
        next_analysis_at=None,
    )
    assert chat.activity_version == 1
    assert chat.last_direction == MessageDirection.OUTBOUND
    assert chat.next_analysis_at is None


async def test_chat_upsert_increments_activity_version(chat_state_repo, session_factory):
    user_id = await insert_user(session_factory)
    now = datetime.now(timezone.utc)
    due = now + timedelta(minutes=5)

    chat1 = await chat_state_repo.upsert_on_message(
        user_id=user_id,
        chat_id="972501234567@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=now,
        next_analysis_at=due,
    )
    chat2 = await chat_state_repo.upsert_on_message(
        user_id=user_id,
        chat_id="972501234567@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=now,
        next_analysis_at=due,
    )
    assert chat1.activity_version == 1
    assert chat2.activity_version == 2


async def test_chat_upsert_outbound_after_inbound_cancels(
    chat_state_repo, session_factory
):
    user_id = await insert_user(session_factory)
    now = datetime.now(timezone.utc)
    due = now + timedelta(minutes=5)

    chat1 = await chat_state_repo.upsert_on_message(
        user_id=user_id,
        chat_id="972501234567@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=now,
        next_analysis_at=due,
    )
    chat2 = await chat_state_repo.upsert_on_message(
        user_id=user_id,
        chat_id="972501234567@c.us",
        direction=MessageDirection.OUTBOUND,
        observed_at=now,
        next_analysis_at=None,
    )
    assert chat1.next_analysis_at is not None
    assert chat2.next_analysis_at is None
    assert chat2.last_direction == MessageDirection.OUTBOUND


# --- ChatStateRepository.get -----------------------------------------------


async def test_chat_get_returns_none_for_unknown(chat_state_repo):
    result = await chat_state_repo.get("00000000-0000-0000-0000-000000000000", "unknown@c.us")
    assert result is None


async def test_chat_get_returns_state_after_upsert(chat_state_repo, session_factory):
    user_id = await insert_user(session_factory)
    now = datetime.now(timezone.utc)
    due = now + timedelta(minutes=5)

    await chat_state_repo.upsert_on_message(
        user_id=user_id,
        chat_id="972501234567@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=now,
        next_analysis_at=due,
    )
    chat = await chat_state_repo.get(user_id, "972501234567@c.us")
    assert chat is not None
    assert chat.activity_version == 1


# --- ChatStateRepository.list_due ------------------------------------------


async def test_list_due_returns_due_inbound_chats(chat_state_repo, session_factory):
    user_id = await insert_user(session_factory)
    now = datetime.now(timezone.utc)
    past = now - timedelta(minutes=10)

    await chat_state_repo.upsert_on_message(
        user_id=user_id,
        chat_id="972501234567@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=past,
        next_analysis_at=past + timedelta(minutes=5),  # due in the past
    )
    due = await chat_state_repo.list_due(now)
    assert len(due) == 1
    assert due[0].chat_id == "972501234567@c.us"


async def test_list_due_excludes_not_yet_due(chat_state_repo, session_factory):
    user_id = await insert_user(session_factory)
    now = datetime.now(timezone.utc)
    future = now + timedelta(minutes=5)

    await chat_state_repo.upsert_on_message(
        user_id=user_id,
        chat_id="972501234567@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=now,
        next_analysis_at=future,
    )
    due = await chat_state_repo.list_due(now)
    assert len(due) == 0


async def test_list_due_excludes_outbound(chat_state_repo, session_factory):
    user_id = await insert_user(session_factory)
    now = datetime.now(timezone.utc)
    past = now - timedelta(minutes=10)

    await chat_state_repo.upsert_on_message(
        user_id=user_id,
        chat_id="972501234567@c.us",
        direction=MessageDirection.OUTBOUND,
        observed_at=past,
        next_analysis_at=None,
    )
    due = await chat_state_repo.list_due(now)
    assert len(due) == 0


async def test_list_due_excludes_already_processed(chat_state_repo, session_factory):
    user_id = await insert_user(session_factory)
    now = datetime.now(timezone.utc)
    past = now - timedelta(minutes=10)

    chat = await chat_state_repo.upsert_on_message(
        user_id=user_id,
        chat_id="972501234567@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=past,
        next_analysis_at=past + timedelta(minutes=5),
    )
    # Mark as processed
    await chat_state_repo.mark_processed(user_id, "972501234567@c.us", chat.activity_version)
    due = await chat_state_repo.list_due(now)
    assert len(due) == 0


async def test_list_due_respects_limit(chat_state_repo, session_factory):
    user_id = await insert_user(session_factory)
    now = datetime.now(timezone.utc)
    past = now - timedelta(minutes=10)

    for i in range(5):
        await chat_state_repo.upsert_on_message(
            user_id=user_id,
            chat_id=f"97250123456{i:02d}@c.us",
            direction=MessageDirection.INBOUND,
            observed_at=past,
            next_analysis_at=past + timedelta(minutes=5),
        )
    due = await chat_state_repo.list_due(now, limit=3)
    assert len(due) == 3


# --- ChatStateRepository.mark_processed ------------------------------------


async def test_mark_processed_succeeds_when_version_matches(
    chat_state_repo, session_factory
):
    user_id = await insert_user(session_factory)
    now = datetime.now(timezone.utc)
    past = now - timedelta(minutes=10)

    chat = await chat_state_repo.upsert_on_message(
        user_id=user_id,
        chat_id="972501234567@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=past,
        next_analysis_at=past + timedelta(minutes=5),
    )
    updated = await chat_state_repo.mark_processed(
        user_id, "972501234567@c.us", chat.activity_version
    )
    assert updated is True

    fresh = await chat_state_repo.get(user_id, "972501234567@c.us")
    assert fresh is not None
    assert fresh.last_processed_version == chat.activity_version
    assert fresh.next_analysis_at is None


async def test_mark_processed_fails_when_version_changed(
    chat_state_repo, session_factory
):
    user_id = await insert_user(session_factory)
    now = datetime.now(timezone.utc)
    past = now - timedelta(minutes=10)

    chat_v1 = await chat_state_repo.upsert_on_message(
        user_id=user_id,
        chat_id="972501234567@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=past,
        next_analysis_at=past + timedelta(minutes=5),
    )
    # New message arrives, version increments
    chat_v2 = await chat_state_repo.upsert_on_message(
        user_id=user_id,
        chat_id="972501234567@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=now,
        next_analysis_at=now + timedelta(minutes=5),
    )
    # Try to mark the old version as processed — should fail
    updated = await chat_state_repo.mark_processed(
        user_id, "972501234567@c.us", chat_v1.activity_version
    )
    assert updated is False

    fresh = await chat_state_repo.get(user_id, "972501234567@c.us")
    assert fresh is not None
    assert fresh.next_analysis_at is not None  # still due
    assert fresh.activity_version == chat_v2.activity_version


async def test_mark_processed_fails_for_unknown_chat(chat_state_repo):
    updated = await chat_state_repo.mark_processed(
        "00000000-0000-0000-0000-000000000000", "unknown@c.us", 1
    )
    assert updated is False


# --- list_due ordering (PostgreSQL) -----------------------------------------


async def test_list_due_orders_oldest_first(chat_state_repo, session_factory):
    """Due chats are returned oldest-first (by next_analysis_at ascending)."""
    user_id = await insert_user(session_factory)
    now = datetime.now(timezone.utc)
    # Three chats with different next_analysis_at times (all in the past).
    # offset=5 means now-5min (oldest/most-overdue), offset=1 means now-1min (newest).
    for i, offset in enumerate([5, 1, 3]):
        await chat_state_repo.upsert_on_message(
            user_id=user_id,
            chat_id=f"chat-{i}@c.us",
            direction=MessageDirection.INBOUND,
            observed_at=now - timedelta(minutes=10),
            next_analysis_at=now - timedelta(minutes=offset),
        )
    due = await chat_state_repo.list_due(now)
    assert len(due) == 3
    # Ordered by next_analysis_at ascending (oldest due first).
    assert due[0].chat_id == "chat-0@c.us"  # now-5min
    assert due[1].chat_id == "chat-2@c.us"  # now-3min
    assert due[2].chat_id == "chat-1@c.us"  # now-1min


# --- Transaction rollback (PostgreSQL) --------------------------------------


async def test_transaction_rollback_leaves_no_partial_state(
    messages_repo, chat_state_repo, session_factory
):
    """If chat_state.upsert_on_message() fails, the chat row should not exist.
    The message was saved in a separate session (independent), but the chat
    state for the invalid user must not be created."""
    user_id = await insert_user(session_factory)
    connection_id = await _seed_connection(session_factory, user_id)

    # Save a message first (succeeds — separate session).
    msg = _make_message(user_id=user_id, connection_id=connection_id)
    inserted = await messages_repo.save(msg)
    assert inserted is True

    # Now make the chat state upsert fail by passing an invalid user_id
    # (not a UUID) — this will cause a DataError.
    from sqlalchemy.exc import DataError

    with pytest.raises(DataError):
        await chat_state_repo.upsert_on_message(
            user_id="not-a-uuid",
            chat_id="972501234567@c.us",
            direction=MessageDirection.INBOUND,
            observed_at=datetime.now(timezone.utc),
            next_analysis_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

    # Verify the chat row was NOT created for the valid user.
    chat = await chat_state_repo.get(user_id, "972501234567@c.us")
    assert chat is None


async def test_uow_transaction_rollback_on_chat_state_failure(
    messages_repo, chat_state_repo, session_factory, unit_of_work_factory
):
    """Full UoW rollback: if chat_state.upsert fails after messages.save
    succeeds, neither the message nor the chat state should be committed."""
    user_id = await insert_user(session_factory)
    connection_id = await _seed_connection(session_factory, user_id)

    # Attempt a UoW transaction that will fail at chat_state.upsert.
    msg = _make_message(user_id=user_id, connection_id=connection_id)
    from sqlalchemy.exc import DataError

    try:
        uow = unit_of_work_factory()
        async with uow:
            await uow.messages.save(msg)
            # Force a failure: pass an invalid user_id to upsert.
            await uow.chat_state.upsert_on_message(
                user_id="not-a-uuid",
                chat_id="972501234567@c.us",
                direction=MessageDirection.INBOUND,
                observed_at=datetime.now(timezone.utc),
                next_analysis_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
    except DataError:
        pass  # expected — invalid UUID triggers rollback

    # Verify the message was NOT committed (transaction rolled back).
    from sqlalchemy import select

    from echo_v2.persistence.orm import MessageRow

    async with session_factory() as session:
        result = await session.execute(
            select(MessageRow).where(MessageRow.id == msg.id)
        )
        assert result.scalar_one_or_none() is None

    # Verify the chat state was NOT created.
    chat = await chat_state_repo.get(user_id, "972501234567@c.us")
    assert chat is None

    # Retry the webhook — should succeed normally.
    msg2 = _make_message(user_id=user_id, connection_id=connection_id)
    uow2 = unit_of_work_factory()
    async with uow2:
        inserted = await uow2.messages.save(msg2)
        assert inserted is True
        await uow2.chat_state.upsert_on_message(
            user_id=user_id,
            chat_id="972501234567@c.us",
            direction=MessageDirection.INBOUND,
            observed_at=datetime.now(timezone.utc),
            next_analysis_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

    # Now both should exist.
    chat = await chat_state_repo.get(user_id, "972501234567@c.us")
    assert chat is not None
    assert chat.activity_version == 1


# --- Conditional mark_processed race (PostgreSQL) --------------------------


async def test_conditional_mark_processed_loses_race_safely(
    chat_state_repo, session_factory
):
    """If activity_version changes between read and mark_processed,
    the conditional UPDATE must not match — the chat stays due."""
    user_id = await insert_user(session_factory)
    now = datetime.now(timezone.utc)
    past = now - timedelta(minutes=10)

    # Create a chat (version 1, due).
    chat_v1 = await chat_state_repo.upsert_on_message(
        user_id=user_id,
        chat_id="972501234567@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=past,
        next_analysis_at=past + timedelta(minutes=5),
    )

    # A new message arrives (version 2, new next_analysis_at).
    chat_v2 = await chat_state_repo.upsert_on_message(
        user_id=user_id,
        chat_id="972501234567@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=now,
        next_analysis_at=now + timedelta(minutes=5),
    )
    assert chat_v2.activity_version == 2

    # Worker tries to mark_processed with the stale version 1.
    updated = await chat_state_repo.mark_processed(
        user_id, "972501234567@c.us", chat_v1.activity_version
    )
    assert updated is False

    # Chat should still be due with version 2.
    fresh = await chat_state_repo.get(user_id, "972501234567@c.us")
    assert fresh is not None
    assert fresh.activity_version == 2
    assert fresh.last_processed_version == 0
    assert fresh.next_analysis_at is not None
