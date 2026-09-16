"""Tests for the atomic repository operations.

Covers the three atomicity fixes:

1. ``IngestionRepository.ingest_if_new`` — message + chat state in one tx.
2. ``WaitingForMeActionRepository`` atomic command methods — action record
   + state mutation in one tx.
3. ``StateWebhookRepository.claim_and_update_status`` — dedup claim +
   connection status update in one tx.

These tests use the in-memory implementations, which provide process-local
parity with the Postgres implementations. The Postgres implementations
follow the same logic with explicit transactions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.app.webhooks.dedup import InMemoryWebhookDedupStore
from echo_v2.domain.feedback import HandlingOutcome
from echo_v2.persistence.chat_repositories import (
    InMemoryChatStateRepository,
    InMemoryIngestionRepository,
    InMemoryMessageRepository,
    InMemoryWaitingForMeActiveRepository,
)
from echo_v2.persistence.feedback_repositories import (
    InMemoryChatMuteRepository,
    InMemoryWaitingForMeActionRepository,
)
from echo_v2.persistence.state_webhook import (
    InMemoryStateWebhookRepository,
)
from echo_v2.persistence.whatsapp_connections import (
    InMemoryWhatsAppConnectionRepository,
    StoredConnection,
)
from echo_v2.ports.whatsapp import (
    ConnectionRef,
    ConnectionStatus,
    MessageDirection,
    ProviderConnectionStateChanged,
    ProviderCredentials,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)


# --- IngestionRepository.ingest_if_new -------------------------------------


def _make_message(
    *,
    user_id: str = "user-1",
    connection_id: str = "conn-1",
    chat_id: str = "972501234567@c.us",
    provider_message_id: str = "msg-1",
):
    from echo_v2.domain.chat import Message

    return Message(
        id="msg-uuid-1",
        user_id=user_id,
        connection_id=connection_id,
        chat_id=chat_id,
        provider_message_id=provider_message_id,
        direction=MessageDirection.INBOUND,
        sender_id="972501234567",
        sender_name=None,
        chat_name=None,
        timestamp=NOW,
        message_type="text",
        text="hello",
    )


async def test_ingest_if_new_inserts_message_and_updates_state():
    msg_repo = InMemoryMessageRepository()
    state_repo = InMemoryChatStateRepository()
    repo = InMemoryIngestionRepository(msg_repo, state_repo)

    message = _make_message()
    inserted = await repo.ingest_if_new(
        message=message,
        direction=MessageDirection.INBOUND,
        observed_at=NOW,
        next_analysis_at=NOW + timedelta(minutes=5),
        chat_name=None,
    )
    assert inserted is True

    chat = await state_repo.get("user-1", "972501234567@c.us")
    assert chat is not None
    assert chat.activity_version == 1
    assert chat.last_direction == MessageDirection.INBOUND


async def test_ingest_if_new_duplicate_does_not_update_state():
    msg_repo = InMemoryMessageRepository()
    state_repo = InMemoryChatStateRepository()
    repo = InMemoryIngestionRepository(msg_repo, state_repo)

    message = _make_message()
    first = await repo.ingest_if_new(
        message=message,
        direction=MessageDirection.INBOUND,
        observed_at=NOW,
        next_analysis_at=NOW + timedelta(minutes=5),
    )
    second = await repo.ingest_if_new(
        message=message,
        direction=MessageDirection.INBOUND,
        observed_at=NOW,
        next_analysis_at=NOW + timedelta(minutes=5),
    )
    assert first is True
    assert second is False

    chat = await state_repo.get("user-1", "972501234567@c.us")
    assert chat is not None
    assert chat.activity_version == 1  # not incremented on duplicate


# --- WaitingForMeActionRepository atomic commands --------------------------


def _make_action_repo(
    *,
    active_repo: InMemoryWaitingForMeActiveRepository | None = None,
    mute_repo: InMemoryChatMuteRepository | None = None,
) -> InMemoryWaitingForMeActionRepository:
    return InMemoryWaitingForMeActionRepository(
        active_repo=active_repo or InMemoryWaitingForMeActiveRepository(),
        mute_repo=mute_repo or InMemoryChatMuteRepository(),
    )


async def _setup_active(
    active_repo: InMemoryWaitingForMeActiveRepository,
    *,
    target_version: int = 1,
    user_id: str = "user-1",
    chat_id: str = "chat-1",
) -> str:
    await active_repo.upsert(
        user_id=user_id,
        chat_id=chat_id,
        target_version=target_version,
        result_id="result-1",
        waiting_since=NOW,
    )
    active = await active_repo.get(user_id=user_id, chat_id=chat_id)
    return active.id


async def test_resolve_and_delete_by_version_applied():
    active_repo = InMemoryWaitingForMeActiveRepository()
    repo = _make_action_repo(active_repo=active_repo)
    active_id = await _setup_active(active_repo)

    result = await repo.resolve_and_delete_by_version(
        user_id="user-1",
        active_id=active_id,
        target_version=1,
        provider_message_id="cb-1",
    )
    assert result.outcome is HandlingOutcome.APPLIED
    assert result.chat_id == "chat-1"
    assert result.result_id == "result-1"

    # Active item deleted.
    active = await active_repo.get_by_id(active_id)
    assert active is None


async def test_resolve_and_delete_by_version_duplicate():
    active_repo = InMemoryWaitingForMeActiveRepository()
    repo = _make_action_repo(active_repo=active_repo)
    active_id = await _setup_active(active_repo)

    first = await repo.resolve_and_delete_by_version(
        user_id="user-1",
        active_id=active_id,
        target_version=1,
        provider_message_id="cb-1",
    )
    second = await repo.resolve_and_delete_by_version(
        user_id="user-1",
        active_id=active_id,
        target_version=1,
        provider_message_id="cb-1",
    )
    assert first.outcome is HandlingOutcome.APPLIED
    assert second.outcome is HandlingOutcome.DUPLICATE


async def test_resolve_and_delete_by_version_stale():
    active_repo = InMemoryWaitingForMeActiveRepository()
    repo = _make_action_repo(active_repo=active_repo)
    active_id = await _setup_active(active_repo, target_version=2)

    result = await repo.resolve_and_delete_by_version(
        user_id="user-1",
        active_id=active_id,
        target_version=1,  # wrong version
        provider_message_id="cb-1",
    )
    assert result.outcome is HandlingOutcome.STALE
    # Active item NOT deleted.
    active = await active_repo.get_by_id(active_id)
    assert active is not None


async def test_resolve_and_delete_by_version_not_found():
    repo = _make_action_repo()
    result = await repo.resolve_and_delete_by_version(
        user_id="user-1",
        active_id="nonexistent",
        target_version=1,
        provider_message_id="cb-1",
    )
    assert result.outcome is HandlingOutcome.NOT_FOUND


async def test_resolve_and_delete_by_chat_applied():
    active_repo = InMemoryWaitingForMeActiveRepository()
    repo = _make_action_repo(active_repo=active_repo)
    active_id = await _setup_active(active_repo)

    result = await repo.resolve_and_delete_by_chat(
        user_id="user-1",
        active_id=active_id,
        target_version=1,
        provider_message_id="cb-1",
    )
    assert result.outcome is HandlingOutcome.APPLIED
    assert result.chat_id == "chat-1"
    assert result.result_id == "result-1"


async def test_resolve_and_delete_by_chat_stale():
    active_repo = InMemoryWaitingForMeActiveRepository()
    repo = _make_action_repo(active_repo=active_repo)
    active_id = await _setup_active(active_repo, target_version=2)

    result = await repo.resolve_and_delete_by_chat(
        user_id="user-1",
        active_id=active_id,
        target_version=1,
        provider_message_id="cb-1",
    )
    assert result.outcome is HandlingOutcome.STALE


async def test_snooze_active_applied():
    active_repo = InMemoryWaitingForMeActiveRepository()
    repo = _make_action_repo(active_repo=active_repo)
    active_id = await _setup_active(active_repo)

    snoozed_until = NOW + timedelta(hours=1)
    result = await repo.snooze_active(
        user_id="user-1",
        active_id=active_id,
        target_version=1,
        snoozed_until=snoozed_until,
        provider_message_id="cb-1",
    )
    assert result.outcome is HandlingOutcome.APPLIED
    assert result.chat_id == "chat-1"

    active = await active_repo.get_by_id(active_id)
    assert active is not None
    assert active.snoozed_until == snoozed_until


async def test_snooze_active_stale():
    active_repo = InMemoryWaitingForMeActiveRepository()
    repo = _make_action_repo(active_repo=active_repo)
    active_id = await _setup_active(active_repo, target_version=2)

    result = await repo.snooze_active(
        user_id="user-1",
        active_id=active_id,
        target_version=1,
        snoozed_until=NOW + timedelta(hours=1),
        provider_message_id="cb-1",
    )
    assert result.outcome is HandlingOutcome.STALE


async def test_snooze_active_duplicate():
    active_repo = InMemoryWaitingForMeActiveRepository()
    repo = _make_action_repo(active_repo=active_repo)
    active_id = await _setup_active(active_repo)

    await repo.snooze_active(
        user_id="user-1",
        active_id=active_id,
        target_version=1,
        snoozed_until=NOW + timedelta(hours=1),
        provider_message_id="cb-1",
    )
    second = await repo.snooze_active(
        user_id="user-1",
        active_id=active_id,
        target_version=1,
        snoozed_until=NOW + timedelta(hours=2),
        provider_message_id="cb-1",
    )
    assert second.outcome is HandlingOutcome.DUPLICATE


async def test_mute_chat_atomic_applied_temporary():
    mute_repo = InMemoryChatMuteRepository()
    repo = _make_action_repo(mute_repo=mute_repo)

    muted_until = NOW + timedelta(hours=48)
    result = await repo.mute_chat_atomic(
        user_id="user-1",
        chat_id="chat-1",
        permanent=False,
        muted_until=muted_until,
        provider_message_id="cb-1",
    )
    assert result.outcome is HandlingOutcome.APPLIED

    is_muted = await mute_repo.is_muted(
        user_id="user-1", chat_id="chat-1", now=NOW,
    )
    assert is_muted is True


async def test_mute_chat_atomic_applied_permanent():
    mute_repo = InMemoryChatMuteRepository()
    repo = _make_action_repo(mute_repo=mute_repo)

    result = await repo.mute_chat_atomic(
        user_id="user-1",
        chat_id="chat-1",
        permanent=True,
        muted_until=None,
        provider_message_id="cb-1",
    )
    assert result.outcome is HandlingOutcome.APPLIED

    is_muted = await mute_repo.is_muted(
        user_id="user-1", chat_id="chat-1", now=NOW + timedelta(days=365),
    )
    assert is_muted is True


async def test_mute_chat_atomic_duplicate():
    mute_repo = InMemoryChatMuteRepository()
    repo = _make_action_repo(mute_repo=mute_repo)

    await repo.mute_chat_atomic(
        user_id="user-1",
        chat_id="chat-1",
        permanent=True,
        muted_until=None,
        provider_message_id="cb-1",
    )
    second = await repo.mute_chat_atomic(
        user_id="user-1",
        chat_id="chat-1",
        permanent=True,
        muted_until=None,
        provider_message_id="cb-1",
    )
    assert second.outcome is HandlingOutcome.DUPLICATE


async def test_unmute_chat_atomic_applied():
    mute_repo = InMemoryChatMuteRepository()
    repo = _make_action_repo(mute_repo=mute_repo)

    await repo.mute_chat_atomic(
        user_id="user-1",
        chat_id="chat-1",
        permanent=True,
        muted_until=None,
        provider_message_id="cb-1",
    )
    result = await repo.unmute_chat_atomic(
        user_id="user-1",
        chat_id="chat-1",
        provider_message_id="cb-2",
    )
    assert result.outcome is HandlingOutcome.APPLIED

    is_muted = await mute_repo.is_muted(
        user_id="user-1", chat_id="chat-1", now=NOW,
    )
    assert is_muted is False


async def test_unmute_chat_atomic_duplicate():
    mute_repo = InMemoryChatMuteRepository()
    repo = _make_action_repo(mute_repo=mute_repo)

    await repo.unmute_chat_atomic(
        user_id="user-1",
        chat_id="chat-1",
        provider_message_id="cb-1",
    )
    second = await repo.unmute_chat_atomic(
        user_id="user-1",
        chat_id="chat-1",
        provider_message_id="cb-1",
    )
    assert second.outcome is HandlingOutcome.DUPLICATE


# --- StateWebhookRepository.claim_and_update_status ------------------------


async def _make_connection_repo() -> InMemoryWhatsAppConnectionRepository:
    repo = InMemoryWhatsAppConnectionRepository()
    conn = StoredConnection(
        user_id="user-1",
        ref=ConnectionRef(provider="green", provider_connection_id="inst-1"),
        credentials=ProviderCredentials(b"api-tok"),
        webhook_token_hash=b"\x00" * 32,
        status=ConnectionStatus.CONNECTED,
    )
    await repo.save(conn)
    return repo


def _make_state_event(
    *,
    status: ConnectionStatus = ConnectionStatus.PAIRING_REQUIRED,
    event_id: str = "evt-1",
) -> ProviderConnectionStateChanged:
    return ProviderConnectionStateChanged(
        event_id=event_id,
        connection=ConnectionRef(provider="green", provider_connection_id="inst-1"),
        status=status,
        provider_raw_status="notAuthorized",
        timestamp=NOW,
    )


async def test_claim_and_update_status_first_event_claims_and_updates():
    dedup = InMemoryWebhookDedupStore()
    conn_repo = await _make_connection_repo()
    repo = InMemoryStateWebhookRepository(dedup, conn_repo)

    event = _make_state_event()
    result = await repo.claim_and_update_status(
        event_id=event.event_id,
        provider="green",
        connection_id="conn-uuid-1",
        event_type=type(event).__name__,
        connection_ref=event.connection,
        status=event.status,
        raw=event.provider_raw_status,
    )
    assert result.claimed is True
    assert result.was_connected is True

    conn = await conn_repo.get(event.connection)
    assert conn is not None
    assert conn.status == ConnectionStatus.PAIRING_REQUIRED


async def test_claim_and_update_status_duplicate_does_not_update():
    dedup = InMemoryWebhookDedupStore()
    conn_repo = await _make_connection_repo()
    repo = InMemoryStateWebhookRepository(dedup, conn_repo)

    event = _make_state_event()
    first = await repo.claim_and_update_status(
        event_id=event.event_id,
        provider="green",
        connection_id="conn-uuid-1",
        event_type=type(event).__name__,
        connection_ref=event.connection,
        status=event.status,
        raw=event.provider_raw_status,
    )
    second = await repo.claim_and_update_status(
        event_id=event.event_id,
        provider="green",
        connection_id="conn-uuid-1",
        event_type=type(event).__name__,
        connection_ref=event.connection,
        status=ConnectionStatus.CONNECTED,
        raw="authorized",
    )
    assert first.claimed is True
    assert second.claimed is False

    # Status NOT updated by the duplicate.
    conn = await conn_repo.get(event.connection)
    assert conn is not None
    assert conn.status == ConnectionStatus.PAIRING_REQUIRED


async def test_claim_and_update_status_was_connected_false_for_initial():
    dedup = InMemoryWebhookDedupStore()
    conn_repo = InMemoryWhatsAppConnectionRepository()
    conn = StoredConnection(
        user_id="user-1",
        ref=ConnectionRef(provider="green", provider_connection_id="inst-1"),
        credentials=ProviderCredentials(b"api-tok"),
        webhook_token_hash=b"\x00" * 32,
        status=ConnectionStatus.PAIRING_REQUIRED,
    )
    await conn_repo.save(conn)

    repo = InMemoryStateWebhookRepository(dedup, conn_repo)
    event = _make_state_event(status=ConnectionStatus.CONNECTED)
    result = await repo.claim_and_update_status(
        event_id=event.event_id,
        provider="green",
        connection_id="conn-uuid-1",
        event_type=type(event).__name__,
        connection_ref=event.connection,
        status=event.status,
        raw=event.provider_raw_status,
    )
    assert result.claimed is True
    assert result.was_connected is False  # was PAIRING_REQUIRED before
