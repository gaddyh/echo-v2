"""Postgres-backed chat ingestion repository tests (requires Docker)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
import pytest_asyncio

from echo_v2.domain.chat import Message
from echo_v2.domain.digest import DailyDigestStatus
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


async def test_chat_upsert_outbound_after_inbound_schedules_analysis(
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
        next_analysis_at=due,  # outbound also schedules now
    )
    assert chat1.next_analysis_at is not None
    assert chat2.next_analysis_at is not None
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


async def test_list_due_includes_outbound_with_next_analysis_at(
    chat_state_repo, session_factory
):
    """list_due no longer filters by direction — outbound chats with
    next_analysis_at set are included."""
    user_id = await insert_user(session_factory)
    now = datetime.now(timezone.utc)
    past = now - timedelta(minutes=10)

    await chat_state_repo.upsert_on_message(
        user_id=user_id,
        chat_id="972501234567@c.us",
        direction=MessageDirection.OUTBOUND,
        observed_at=past,
        next_analysis_at=past + timedelta(minutes=5),
    )
    due = await chat_state_repo.list_due(now)
    assert len(due) == 1
    assert due[0].chat_id == "972501234567@c.us"


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


# --- MessageRepository.list_for_analysis ------------------------------------


def _make_timed_message(
    user_id: str,
    connection_id: str,
    *,
    direction: MessageDirection,
    text: str,
    offset_minutes: int,
    chat_id: str = "972501234567@c.us",
    provider_message_id: str | None = None,
) -> Message:
    return Message(
        id=str(uuid.uuid4()),
        user_id=user_id,
        connection_id=connection_id,
        chat_id=chat_id,
        provider_message_id=provider_message_id or str(uuid.uuid4()),
        direction=direction,
        sender_id=None,
        timestamp=datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
        + timedelta(minutes=offset_minutes),
        message_type=MessageKind.TEXT.value,
        text=text,
    )


async def test_list_for_analysis_no_outbound_returns_last_n(
    messages_repo, session_factory
):
    user_id = await insert_user(session_factory)
    conn_id = await _seed_connection(session_factory, user_id)

    for i in range(25):
        msg = _make_timed_message(
            user_id, conn_id,
            direction=MessageDirection.INBOUND,
            text=f"msg-{i}",
            offset_minutes=i,
        )
        await messages_repo.save(msg)

    result = await messages_repo.list_for_analysis(
        user_id=user_id, chat_id="972501234567@c.us", max_no_outbound=20,
    )
    assert len(result) == 20
    assert result[0].text == "msg-5"
    assert result[-1].text == "msg-24"


async def test_list_for_analysis_with_outbound_returns_context_and_after(
    messages_repo, session_factory
):
    user_id = await insert_user(session_factory)
    conn_id = await _seed_connection(session_factory, user_id)

    msgs = [
        _make_timed_message(user_id, conn_id, direction=MessageDirection.INBOUND, text="ctx-1", offset_minutes=0),
        _make_timed_message(user_id, conn_id, direction=MessageDirection.INBOUND, text="ctx-2", offset_minutes=1),
        _make_timed_message(user_id, conn_id, direction=MessageDirection.INBOUND, text="ctx-3", offset_minutes=2),
        _make_timed_message(user_id, conn_id, direction=MessageDirection.INBOUND, text="ctx-4", offset_minutes=3),
        _make_timed_message(user_id, conn_id, direction=MessageDirection.INBOUND, text="ctx-5", offset_minutes=4),
        _make_timed_message(user_id, conn_id, direction=MessageDirection.INBOUND, text="ctx-6", offset_minutes=5),
        _make_timed_message(user_id, conn_id, direction=MessageDirection.OUTBOUND, text="reply", offset_minutes=6),
        _make_timed_message(user_id, conn_id, direction=MessageDirection.INBOUND, text="after-1", offset_minutes=7),
        _make_timed_message(user_id, conn_id, direction=MessageDirection.INBOUND, text="after-2", offset_minutes=8),
    ]
    for m in msgs:
        await messages_repo.save(m)

    result = await messages_repo.list_for_analysis(
        user_id=user_id, chat_id="972501234567@c.us", context_messages=5,
    )
    assert len(result) == 8
    assert result[0].text == "ctx-2"
    assert result[-1].text == "after-2"


async def test_list_for_analysis_empty_chat(messages_repo, session_factory):
    user_id = await insert_user(session_factory)
    await _seed_connection(session_factory, user_id)

    result = await messages_repo.list_for_analysis(
        user_id=user_id, chat_id="972501234567@c.us",
    )
    assert result == []


async def test_list_for_analysis_multiple_outbounds_uses_last(
    messages_repo, session_factory
):
    user_id = await insert_user(session_factory)
    conn_id = await _seed_connection(session_factory, user_id)

    msgs = [
        _make_timed_message(user_id, conn_id, direction=MessageDirection.INBOUND, text="old-1", offset_minutes=0),
        _make_timed_message(user_id, conn_id, direction=MessageDirection.OUTBOUND, text="old-reply", offset_minutes=1),
        _make_timed_message(user_id, conn_id, direction=MessageDirection.INBOUND, text="old-2", offset_minutes=2),
        _make_timed_message(user_id, conn_id, direction=MessageDirection.INBOUND, text="ctx-1", offset_minutes=3),
        _make_timed_message(user_id, conn_id, direction=MessageDirection.INBOUND, text="ctx-2", offset_minutes=4),
        _make_timed_message(user_id, conn_id, direction=MessageDirection.OUTBOUND, text="new-reply", offset_minutes=5),
        _make_timed_message(user_id, conn_id, direction=MessageDirection.INBOUND, text="after", offset_minutes=6),
    ]
    for m in msgs:
        await messages_repo.save(m)

    result = await messages_repo.list_for_analysis(
        user_id=user_id, chat_id="972501234567@c.us", context_messages=2,
    )
    assert len(result) == 4
    assert result[0].text == "ctx-1"
    assert result[1].text == "ctx-2"
    assert result[2].text == "new-reply"
    assert result[3].text == "after"


# --- PostgresWaitingForMeResultRepository -----------------------------------


@pytest_asyncio.fixture
async def wfm_repo(session_factory, clean_db):
    from echo_v2.persistence.postgres_chat import PostgresWaitingForMeResultRepository

    return PostgresWaitingForMeResultRepository(session_factory)


async def test_wfm_save_and_list_recent(wfm_repo, session_factory):
    from echo_v2.domain.waiting_for_me import WaitingForMeDecision, WaitingForMeResult

    user_id = await insert_user(session_factory)
    result1 = WaitingForMeResult(
        decision=WaitingForMeDecision.WAITING_FOR_ME,
        confidence=0.9,
        reason="open question",
        target_version=1,
    )
    result2 = WaitingForMeResult(
        decision=WaitingForMeDecision.NOT_WAITING_FOR_ME,
        confidence=0.8,
        reason="closed",
        target_version=2,
    )
    await wfm_repo.save(user_id=user_id, chat_id="chat-1@c.us", result=result1)
    await wfm_repo.save(user_id=user_id, chat_id="chat-1@c.us", result=result2)

    results = await wfm_repo.list_recent(user_id=user_id, chat_id="chat-1@c.us")
    assert len(results) == 2
    # Newest first
    assert results[0].target_version == 2
    assert results[0].decision == WaitingForMeDecision.NOT_WAITING_FOR_ME
    assert results[1].target_version == 1
    assert results[1].decision == WaitingForMeDecision.WAITING_FOR_ME


async def test_wfm_list_recent_filters_by_chat(wfm_repo, session_factory):
    from echo_v2.domain.waiting_for_me import WaitingForMeDecision, WaitingForMeResult

    user_id = await insert_user(session_factory)
    await wfm_repo.save(
        user_id=user_id,
        chat_id="chat-1@c.us",
        result=WaitingForMeResult(
            decision=WaitingForMeDecision.WAITING_FOR_ME,
            target_version=1,
        ),
    )
    await wfm_repo.save(
        user_id=user_id,
        chat_id="chat-2@c.us",
        result=WaitingForMeResult(
            decision=WaitingForMeDecision.NOT_WAITING_FOR_ME,
            target_version=1,
        ),
    )

    results = await wfm_repo.list_recent(user_id=user_id, chat_id="chat-1@c.us")
    assert len(results) == 1
    assert results[0].decision == WaitingForMeDecision.WAITING_FOR_ME


async def test_wfm_list_recent_respects_limit(wfm_repo, session_factory):
    from echo_v2.domain.waiting_for_me import WaitingForMeDecision, WaitingForMeResult

    user_id = await insert_user(session_factory)
    for i in range(5):
        await wfm_repo.save(
            user_id=user_id,
            chat_id="chat-1@c.us",
            result=WaitingForMeResult(
                decision=WaitingForMeDecision.WAITING_FOR_ME,
                target_version=i + 1,
            ),
        )

    results = await wfm_repo.list_recent(
        user_id=user_id, chat_id="chat-1@c.us", limit=3,
    )
    assert len(results) == 3
    assert results[0].target_version == 5


async def test_wfm_list_recent_empty(wfm_repo, session_factory):
    user_id = await insert_user(session_factory)
    results = await wfm_repo.list_recent(user_id=user_id, chat_id="chat-1@c.us")
    assert results == []


# --- PostgresWaitingForMeActiveRepository -----------------------------------


@pytest_asyncio.fixture
async def wfm_active_repo(session_factory, clean_db):
    from echo_v2.persistence.postgres_chat import (
        PostgresWaitingForMeActiveRepository,
    )

    return PostgresWaitingForMeActiveRepository(session_factory)


async def _seed_wfm_result(session_factory, user_id, chat_id="chat-1@c.us", version=1):
    """Insert a waiting_for_me_results row and return its ID."""
    from sqlalchemy import insert

    from echo_v2.persistence.orm import WaitingForMeResultRow

    async with session_factory() as session:
        stmt = (
            insert(WaitingForMeResultRow)
            .values(
                user_id=user_id,
                chat_id=chat_id,
                target_version=version,
                decision="waiting_for_me",
                confidence=0.9,
                reason="test",
            )
            .returning(WaitingForMeResultRow.id)
        )
        result_id = (await session.execute(stmt)).scalar_one()
        await session.commit()
        return str(result_id)


async def test_wfm_active_upsert_inserts_new(wfm_active_repo, session_factory):
    user_id = await insert_user(session_factory)
    result_id = await _seed_wfm_result(session_factory, user_id)

    await wfm_active_repo.upsert(
        user_id=user_id,
        chat_id="chat-1@c.us",
        target_version=1,
        result_id=result_id,
        waiting_since=datetime.now(timezone.utc),
    )
    row = await wfm_active_repo.get(user_id=user_id, chat_id="chat-1@c.us")
    assert row is not None
    assert row.target_version == 1
    assert row.result_id == result_id


async def test_wfm_active_upsert_preserves_waiting_since(wfm_active_repo, session_factory):
    user_id = await insert_user(session_factory)
    result_id_1 = await _seed_wfm_result(session_factory, user_id, version=1)
    result_id_2 = await _seed_wfm_result(session_factory, user_id, version=2)

    original_waiting_since = datetime(2026, 9, 12, 8, 0, 0, tzinfo=timezone.utc)

    # First upsert
    await wfm_active_repo.upsert(
        user_id=user_id,
        chat_id="chat-1@c.us",
        target_version=1,
        result_id=result_id_1,
        waiting_since=original_waiting_since,
    )

    # Second upsert — new version
    await wfm_active_repo.upsert(
        user_id=user_id,
        chat_id="chat-1@c.us",
        target_version=2,
        result_id=result_id_2,
        waiting_since=datetime.now(timezone.utc),  # different, but should be ignored
    )

    row = await wfm_active_repo.get(user_id=user_id, chat_id="chat-1@c.us")
    assert row is not None
    assert row.target_version == 2
    assert row.result_id == result_id_2
    assert row.waiting_since == original_waiting_since  # preserved


async def test_wfm_active_delete(wfm_active_repo, session_factory):
    user_id = await insert_user(session_factory)
    result_id = await _seed_wfm_result(session_factory, user_id)

    await wfm_active_repo.upsert(
        user_id=user_id,
        chat_id="chat-1@c.us",
        target_version=1,
        result_id=result_id,
        waiting_since=datetime.now(timezone.utc),
    )
    deleted = await wfm_active_repo.delete(user_id=user_id, chat_id="chat-1@c.us")
    assert deleted is True

    row = await wfm_active_repo.get(user_id=user_id, chat_id="chat-1@c.us")
    assert row is None


async def test_wfm_active_delete_returns_false_if_not_exists(wfm_active_repo, session_factory):
    user_id = await insert_user(session_factory)
    deleted = await wfm_active_repo.delete(user_id=user_id, chat_id="chat-1@c.us")
    assert deleted is False


async def test_wfm_active_list_active(wfm_active_repo, session_factory):
    user_id = await insert_user(session_factory)
    result_id_1 = await _seed_wfm_result(session_factory, user_id, chat_id="chat-1@c.us", version=5)
    result_id_2 = await _seed_wfm_result(session_factory, user_id, chat_id="chat-2@c.us", version=3)

    await wfm_active_repo.upsert(
        user_id=user_id,
        chat_id="chat-1@c.us",
        target_version=5,
        result_id=result_id_1,
        waiting_since=datetime.now(timezone.utc),
    )
    await wfm_active_repo.upsert(
        user_id=user_id,
        chat_id="chat-2@c.us",
        target_version=3,
        result_id=result_id_2,
        waiting_since=datetime.now(timezone.utc),
    )

    # Only chat-1 has matching version
    active = await wfm_active_repo.list_active(
        user_id=user_id,
        current_versions={"chat-1@c.us": 5, "chat-2@c.us": 4},
    )
    assert len(active) == 1
    assert active[0].chat_id == "chat-1@c.us"


async def test_wfm_active_delete_if_version_succeeds(wfm_active_repo, session_factory):
    """delete_if_version deletes when id + user + version match."""
    user_id = await insert_user(session_factory)
    result_id = await _seed_wfm_result(session_factory, user_id)

    await wfm_active_repo.upsert(
        user_id=user_id,
        chat_id="chat-1@c.us",
        target_version=1,
        result_id=result_id,
        waiting_since=datetime.now(timezone.utc),
    )
    active = await wfm_active_repo.get(user_id=user_id, chat_id="chat-1@c.us")
    assert active is not None

    deleted = await wfm_active_repo.delete_if_version(
        active_id=active.id,
        user_id=user_id,
        target_version=1,
    )
    assert deleted is not None
    assert deleted.chat_id == "chat-1@c.us"

    row = await wfm_active_repo.get(user_id=user_id, chat_id="chat-1@c.us")
    assert row is None


async def test_wfm_active_delete_if_version_stale_returns_none(wfm_active_repo, session_factory):
    """delete_if_version returns None when version doesn't match."""
    user_id = await insert_user(session_factory)
    result_id = await _seed_wfm_result(session_factory, user_id)

    await wfm_active_repo.upsert(
        user_id=user_id,
        chat_id="chat-1@c.us",
        target_version=2,
        result_id=result_id,
        waiting_since=datetime.now(timezone.utc),
    )
    active = await wfm_active_repo.get(user_id=user_id, chat_id="chat-1@c.us")
    assert active is not None

    # Wrong version → None.
    deleted = await wfm_active_repo.delete_if_version(
        active_id=active.id,
        user_id=user_id,
        target_version=1,
    )
    assert deleted is None
    # Row still exists.
    row = await wfm_active_repo.get(user_id=user_id, chat_id="chat-1@c.us")
    assert row is not None


async def test_wfm_active_delete_if_version_wrong_user_returns_none(wfm_active_repo, session_factory):
    """delete_if_version returns None when user_id doesn't match."""
    user_id = await insert_user(session_factory)
    result_id = await _seed_wfm_result(session_factory, user_id)

    await wfm_active_repo.upsert(
        user_id=user_id,
        chat_id="chat-1@c.us",
        target_version=1,
        result_id=result_id,
        waiting_since=datetime.now(timezone.utc),
    )
    active = await wfm_active_repo.get(user_id=user_id, chat_id="chat-1@c.us")
    assert active is not None

    # Wrong user → None.
    deleted = await wfm_active_repo.delete_if_version(
        active_id=active.id,
        user_id="00000000-0000-0000-0000-000000000000",
        target_version=1,
    )
    assert deleted is None


async def test_wfm_active_delete_if_version_nonexistent_returns_none(wfm_active_repo, session_factory):
    """delete_if_version returns None when the row doesn't exist."""
    user_id = await insert_user(session_factory)
    deleted = await wfm_active_repo.delete_if_version(
        active_id=str(uuid.uuid4()),
        user_id=user_id,
        target_version=1,
    )
    assert deleted is None


# --- PostgresDailyDigestRepository ------------------------------------------


@pytest_asyncio.fixture
async def digest_repo(session_factory, clean_db):
    from echo_v2.persistence.postgres_digest import PostgresDailyDigestRepository

    return PostgresDailyDigestRepository(session_factory)


async def test_digest_claim_inserts_new(digest_repo, session_factory):
    user_id = await insert_user(session_factory)
    digest = await digest_repo.claim_or_get(
        user_id=user_id,
        local_date=date(2026, 9, 12),
    )
    assert digest is not None
    assert digest.status == "processing"


async def test_digest_claim_returns_none_if_exists(digest_repo, session_factory):
    user_id = await insert_user(session_factory)
    first = await digest_repo.claim_or_get(
        user_id=user_id,
        local_date=date(2026, 9, 12),
    )
    assert first is not None
    second = await digest_repo.claim_or_get(
        user_id=user_id,
        local_date=date(2026, 9, 12),
    )
    assert second is None


async def test_digest_update_status_sent(digest_repo, session_factory):
    user_id = await insert_user(session_factory)
    digest = await digest_repo.claim_or_get(
        user_id=user_id,
        local_date=date(2026, 9, 12),
    )
    assert digest is not None
    sent_at = datetime(2026, 9, 12, 8, 0, 0, tzinfo=timezone.utc)
    updated = await digest_repo.update_status(
        digest_id=digest.id,
        status=DailyDigestStatus.SENT,
        sent_at=sent_at,
        provider_message_id="msg-123",
        item_count=3,
    )
    assert updated is True
    row = await digest_repo.get(user_id=user_id, local_date=date(2026, 9, 12))
    assert row is not None
    assert row.status == DailyDigestStatus.SENT
    assert row.sent_at is not None
    assert row.provider_message_id == "msg-123"
    assert row.item_count == 3


async def test_digest_update_status_empty(digest_repo, session_factory):
    user_id = await insert_user(session_factory)
    digest = await digest_repo.claim_or_get(
        user_id=user_id,
        local_date=date(2026, 9, 12),
    )
    assert digest is not None
    await digest_repo.update_status(
        digest_id=digest.id,
        status=DailyDigestStatus.EMPTY,
        item_count=0,
    )
    row = await digest_repo.get(user_id=user_id, local_date=date(2026, 9, 12))
    assert row is not None
    assert row.status == DailyDigestStatus.EMPTY


async def test_digest_get_returns_none_if_not_exists(digest_repo, session_factory):
    user_id = await insert_user(session_factory)
    row = await digest_repo.get(user_id=user_id, local_date=date(2026, 9, 12))
    assert row is None


async def test_digest_different_users_same_date(digest_repo, session_factory):
    user1 = await insert_user(session_factory, phone="972501111111")
    user2 = await insert_user(session_factory, phone="972502222222")
    d1 = await digest_repo.claim_or_get(user_id=user1, local_date=date(2026, 9, 12))
    d2 = await digest_repo.claim_or_get(user_id=user2, local_date=date(2026, 9, 12))
    assert d1 is not None
    assert d2 is not None
    assert d1.id != d2.id


async def test_message_get_latest_inbound(session_factory, clean_db):
    """Test PostgresMessageRepository.get_latest_inbound."""
    from echo_v2.persistence.postgres_chat import PostgresMessageRepository

    user_id = await insert_user(session_factory)
    conn_id = await _seed_connection(session_factory, user_id)
    message_repo = PostgresMessageRepository(session_factory)

    # Save an outbound message
    await message_repo.save(Message(
        id=str(uuid.uuid4()),
        user_id=user_id,
        connection_id=conn_id,
        chat_id="chat-1@c.us",
        provider_message_id="msg-1",
        direction=MessageDirection.OUTBOUND,
        sender_id="me",
        timestamp=datetime(2026, 9, 12, 10, 0, 0, tzinfo=timezone.utc),
        message_type="text",
        text="my reply",
    ))
    # Save an inbound message after it
    await message_repo.save(Message(
        id=str(uuid.uuid4()),
        user_id=user_id,
        connection_id=conn_id,
        chat_id="chat-1@c.us",
        provider_message_id="msg-2",
        direction=MessageDirection.INBOUND,
        sender_id="them",
        timestamp=datetime(2026, 9, 12, 10, 5, 0, tzinfo=timezone.utc),
        message_type="text",
        text="their reply",
    ))

    latest = await message_repo.get_latest_inbound(
        user_id=user_id,
        chat_id="chat-1@c.us",
    )
    assert latest is not None
    assert latest.text == "their reply"


async def test_message_get_latest_inbound_returns_none_if_empty(session_factory, clean_db):
    from echo_v2.persistence.postgres_chat import PostgresMessageRepository

    user_id = await insert_user(session_factory)
    message_repo = PostgresMessageRepository(session_factory)
    latest = await message_repo.get_latest_inbound(
        user_id=user_id,
        chat_id="chat-1@c.us",
    )
    assert latest is None
