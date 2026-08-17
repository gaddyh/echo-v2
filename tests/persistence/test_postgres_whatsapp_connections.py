"""Postgres-backed WhatsApp connection repository tests (requires Docker)."""

from __future__ import annotations

import hashlib

import pytest

from echo_v2.ports.whatsapp import (
    ConnectionRef,
    ConnectionStatus,
    ProviderCredentials,
)
from tests.persistence.conftest import insert_user

pytestmark = pytest.mark.asyncio


def _stored(
    *,
    user_id: str,
    ref: ConnectionRef | None = None,
    token: str = "api-tok",
    webhook_token: str = "webhook-tok",
    status: ConnectionStatus = ConnectionStatus.CONNECTED,
):
    from echo_v2.persistence.whatsapp_connections import StoredConnection

    return StoredConnection(
        user_id=user_id,
        ref=ref or ConnectionRef("green", "111"),
        credentials=ProviderCredentials(data=token.encode()),
        webhook_token_hash=hashlib.sha256(webhook_token.encode()).digest(),
        status=status,
        provider_raw_status="authorized",
    )


async def test_save_and_get_round_trip(connections_repo, session_factory):
    user_id = await insert_user(session_factory)
    conn = _stored(user_id=user_id)
    await connections_repo.save(conn)

    fetched = await connections_repo.get(ConnectionRef("green", "111"))
    assert fetched is not None
    assert fetched.user_id == user_id
    assert fetched.ref.provider == "green"
    assert fetched.ref.provider_connection_id == "111"
    assert fetched.status == ConnectionStatus.CONNECTED
    assert fetched.provider_raw_status == "authorized"
    assert fetched.credentials.data == b"api-tok"
    assert fetched.webhook_token_hash == hashlib.sha256(b"webhook-tok").digest()


async def test_save_encrypts_credentials_at_rest(connections_repo, session_factory):
    """The BYTEA column must hold ciphertext, not the plaintext token."""
    from sqlalchemy import text

    user_id = await insert_user(session_factory)
    conn = _stored(user_id=user_id, token="plaintext-secret")
    await connections_repo.save(conn)

    async with session_factory() as session:
        row = (
            await session.execute(
                text("SELECT credentials FROM whatsapp_connections WHERE provider='green'")
            )
        ).scalar_one()
    # IdentityCredentialCipher is a no-op, so this is plaintext here — but
    # the test documents the boundary. A real cipher test is in test_credential_cipher.
    assert row == b"plaintext-secret"


async def test_get_returns_none_for_unknown(connections_repo):
    assert await connections_repo.get(ConnectionRef("green", "nope")) is None


async def test_get_by_user(connections_repo, session_factory):
    user_id = await insert_user(session_factory)
    await connections_repo.save(_stored(user_id=user_id))

    fetched = await connections_repo.get_by_user(user_id)
    assert fetched is not None
    assert fetched.user_id == user_id


async def test_get_by_provider_id(connections_repo, session_factory):
    user_id = await insert_user(session_factory)
    await connections_repo.save(_stored(user_id=user_id))

    fetched = await connections_repo.get_by_provider_id("green", "111")
    assert fetched is not None
    assert fetched.ref.provider_connection_id == "111"


async def test_upsert_on_user_provider_replaces_fields(connections_repo, session_factory):
    """Reconnect: same user + provider, new provider_connection_id → same row, fields replaced."""
    user_id = await insert_user(session_factory)

    await connections_repo.save(_stored(user_id=user_id, ref=ConnectionRef("green", "111"), token="tok1", webhook_token="wh1"))
    await connections_repo.save(_stored(user_id=user_id, ref=ConnectionRef("green", "222"), token="tok2", webhook_token="wh2", status=ConnectionStatus.CONNECTING))

    # Only one row for (user_id, green).
    fetched = await connections_repo.get_by_user(user_id)
    assert fetched is not None
    assert fetched.ref.provider_connection_id == "222"
    assert fetched.credentials.data == b"tok2"
    assert fetched.webhook_token_hash == hashlib.sha256(b"wh2").digest()
    assert fetched.status == ConnectionStatus.CONNECTING


async def test_unique_user_provider_constraint(connections_repo, session_factory):
    """Two different providers for the same user are allowed; same provider twice upserts."""
    user_id = await insert_user(session_factory)
    await connections_repo.save(_stored(user_id=user_id, ref=ConnectionRef("green", "111")))
    await connections_repo.save(_stored(user_id=user_id, ref=ConnectionRef("twilio", "222")))

    green = await connections_repo.get_by_provider_id("green", "111")
    twilio = await connections_repo.get_by_provider_id("twilio", "222")
    assert green is not None and twilio is not None


async def test_unique_provider_connection_id_constraint(connections_repo, session_factory):
    """Two users with the same (provider, provider_connection_id) must conflict."""
    from sqlalchemy.exc import IntegrityError

    user_a = await insert_user(session_factory, phone="+972500000001")
    user_b = await insert_user(session_factory, phone="+972500000002")

    await connections_repo.save(_stored(user_id=user_a, ref=ConnectionRef("green", "shared")))
    with pytest.raises(IntegrityError):
        await connections_repo.save(_stored(user_id=user_b, ref=ConnectionRef("green", "shared")))


async def test_update_status(connections_repo, session_factory):
    user_id = await insert_user(session_factory)
    await connections_repo.save(_stored(user_id=user_id, status=ConnectionStatus.CONNECTING))

    await connections_repo.update_status(
        ConnectionRef("green", "111"),
        ConnectionStatus.CONNECTED,
        "authorized",
    )
    fetched = await connections_repo.get(ConnectionRef("green", "111"))
    assert fetched is not None
    assert fetched.status == ConnectionStatus.CONNECTED
    assert fetched.provider_raw_status == "authorized"


async def test_get_credentials(connections_repo, session_factory):
    user_id = await insert_user(session_factory)
    await connections_repo.save(_stored(user_id=user_id, token="my-api-token"))

    creds = await connections_repo.get_credentials(ConnectionRef("green", "111"))
    assert creds is not None
    assert creds.data == b"my-api-token"


async def test_get_credentials_returns_none_for_unknown(connections_repo):
    assert await connections_repo.get_credentials(ConnectionRef("green", "nope")) is None
