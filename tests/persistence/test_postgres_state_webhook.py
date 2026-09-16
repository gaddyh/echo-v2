"""Postgres integration tests for state webhook repository."""

from __future__ import annotations

import hashlib

import pytest
import pytest_asyncio

from echo_v2.persistence.postgres_state_webhook import (
    PostgresStateWebhookRepository,
)
from echo_v2.persistence.state_webhook import StateWebhookResult
from echo_v2.persistence.whatsapp_connections import StoredConnection
from echo_v2.ports.whatsapp import (
    ConnectionRef,
    ConnectionStatus,
    ProviderCredentials,
)
from tests.persistence.conftest import insert_user

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def state_webhook_repo(session_factory, clean_db):
    return PostgresStateWebhookRepository(session_factory)


def _stored(
    *,
    user_id: str,
    ref: ConnectionRef | None = None,
    token: str = "api-tok",
    webhook_token: str = "webhook-tok",
    status: ConnectionStatus = ConnectionStatus.CONNECTED,
) -> StoredConnection:
    return StoredConnection(
        user_id=user_id,
        ref=ref or ConnectionRef("green", "instance-123"),
        credentials=ProviderCredentials(data=token.encode()),
        webhook_token_hash=hashlib.sha256(webhook_token.encode()).digest(),
        status=status,
        provider_raw_status="authorized",
    )


async def _save_connection(
    connections_repo, user_id: str, ref: ConnectionRef, status: ConnectionStatus
) -> str:
    """Save a connection and return its DB UUID (the connection_id)."""
    await connections_repo.save(
        _stored(user_id=user_id, ref=ref, status=status)
    )
    conn = await connections_repo.get(ref)
    assert conn is not None
    return conn.id  # type: ignore[return-value]


async def test_claim_new_event_updates_status(
    state_webhook_repo, connections_repo, session_factory
):
    """First claim: claimed=True, was_connected reflects prior status."""
    user_id = await insert_user(session_factory)
    ref = ConnectionRef("green", "instance-123")
    conn_id = await _save_connection(
        connections_repo, user_id, ref, ConnectionStatus.PROVISIONING
    )

    result = await state_webhook_repo.claim_and_update_status(
        event_id="evt-1",
        provider="green",
        connection_id=conn_id,
        event_type="stateInstance",
        connection_ref=ref,
        status=ConnectionStatus.CONNECTED,
        raw="authorized",
    )

    assert result == StateWebhookResult(claimed=True, was_connected=False)

    # The connection status should now be updated to CONNECTED.
    conn = await connections_repo.get(ref)
    assert conn is not None
    assert conn.status == ConnectionStatus.CONNECTED
    assert conn.provider_raw_status == "authorized"


async def test_claim_duplicate_event_returns_not_claimed(
    state_webhook_repo, connections_repo, session_factory
):
    """Same event_id twice: second returns claimed=False."""
    user_id = await insert_user(session_factory)
    ref = ConnectionRef("green", "instance-123")
    conn_id = await _save_connection(
        connections_repo, user_id, ref, ConnectionStatus.CONNECTED
    )

    first = await state_webhook_repo.claim_and_update_status(
        event_id="evt-dup",
        provider="green",
        connection_id=conn_id,
        event_type="stateInstance",
        connection_ref=ref,
        status=ConnectionStatus.PROVISIONING,
        raw="not_authorized",
    )
    assert first.claimed is True

    # After the first (successful) claim, status is now PROVISIONING.
    conn_after_first = await connections_repo.get(ref)
    assert conn_after_first is not None
    assert conn_after_first.status == ConnectionStatus.PROVISIONING

    # Duplicate claim tries a *different* status — it must NOT be applied.
    second = await state_webhook_repo.claim_and_update_status(
        event_id="evt-dup",
        provider="green",
        connection_id=conn_id,
        event_type="stateInstance",
        connection_ref=ref,
        status=ConnectionStatus.CONNECTED,
        raw="authorized",
    )
    assert second.claimed is False

    # The duplicate claim must not have changed the status (still PROVISIONING
    # from the first claim, not CONNECTED from the duplicate call).
    conn = await connections_repo.get(ref)
    assert conn is not None
    assert conn.status == ConnectionStatus.PROVISIONING


async def test_claim_updates_was_connected_true(
    state_webhook_repo, connections_repo, session_factory
):
    """Connection was CONNECTED -> was_connected=True."""
    user_id = await insert_user(session_factory)
    ref = ConnectionRef("green", "instance-123")
    conn_id = await _save_connection(
        connections_repo, user_id, ref, ConnectionStatus.CONNECTED
    )

    result = await state_webhook_repo.claim_and_update_status(
        event_id="evt-2",
        provider="green",
        connection_id=conn_id,
        event_type="stateInstance",
        connection_ref=ref,
        status=ConnectionStatus.PROVISIONING,
        raw="not_authorized",
    )

    assert result.claimed is True
    assert result.was_connected is True


async def test_claim_updates_was_connected_false(
    state_webhook_repo, connections_repo, session_factory
):
    """Connection was not CONNECTED -> was_connected=False."""
    user_id = await insert_user(session_factory)
    ref = ConnectionRef("green", "instance-123")
    conn_id = await _save_connection(
        connections_repo, user_id, ref, ConnectionStatus.PROVISIONING
    )

    result = await state_webhook_repo.claim_and_update_status(
        event_id="evt-3",
        provider="green",
        connection_id=conn_id,
        event_type="stateInstance",
        connection_ref=ref,
        status=ConnectionStatus.CONNECTED,
        raw="authorized",
    )

    assert result.claimed is True
    assert result.was_connected is False


async def test_claim_nonexistent_connection(
    state_webhook_repo, connections_repo, session_factory
):
    """Connection_ref doesn't match a row: claimed=True, was_connected=False (status update is a no-op)."""
    user_id = await insert_user(session_factory)
    # Create a real connection so we have a valid UUID for connection_id (FK).
    real_ref = ConnectionRef("green", "instance-real")
    conn_id = await _save_connection(
        connections_repo, user_id, real_ref, ConnectionStatus.CONNECTED
    )

    # Use a connection_ref that matches no row — the status UPDATE is a no-op.
    missing_ref = ConnectionRef("green", "instance-missing")

    result = await state_webhook_repo.claim_and_update_status(
        event_id="evt-4",
        provider="green",
        connection_id=conn_id,
        event_type="stateInstance",
        connection_ref=missing_ref,
        status=ConnectionStatus.PROVISIONING,
        raw="not_authorized",
    )

    assert result.claimed is True
    assert result.was_connected is False

    # The real connection must be untouched (status update was a no-op).
    conn = await connections_repo.get(real_ref)
    assert conn is not None
    assert conn.status == ConnectionStatus.CONNECTED


async def test_claim_different_provider_same_event_id(
    state_webhook_repo, connections_repo, session_factory
):
    """Same event_id but different provider: should still be duplicate (event_id is the dedup key)."""
    user_id = await insert_user(session_factory)
    ref = ConnectionRef("green", "instance-123")
    conn_id = await _save_connection(
        connections_repo, user_id, ref, ConnectionStatus.CONNECTED
    )

    first = await state_webhook_repo.claim_and_update_status(
        event_id="evt-shared",
        provider="green",
        connection_id=conn_id,
        event_type="stateInstance",
        connection_ref=ref,
        status=ConnectionStatus.PROVISIONING,
        raw="not_authorized",
    )
    assert first.claimed is True

    # Same event_id, different provider — still a duplicate.
    second = await state_webhook_repo.claim_and_update_status(
        event_id="evt-shared",
        provider="meta",
        connection_id=conn_id,
        event_type="stateInstance",
        connection_ref=ConnectionRef("meta", "instance-123"),
        status=ConnectionStatus.CONNECTED,
        raw="authorized",
    )
    assert second.claimed is False
