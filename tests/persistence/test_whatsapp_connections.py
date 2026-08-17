"""Tests for the in-memory WhatsApp connection repository."""

from __future__ import annotations

from echo_v2.persistence.whatsapp_connections import (
    InMemoryWhatsAppConnectionRepository,
    StoredConnection,
)
from echo_v2.ports.whatsapp import (
    ConnectionRef,
    ConnectionStatus,
    CredentialResolver,
    ProviderCredentials,
)


def _stored(
    *,
    user_id: str = "u1",
    ref: ConnectionRef | None = None,
    token: str = "api-tok",
    webhook_token: str = "webhook-tok",
    status: ConnectionStatus = ConnectionStatus.CONNECTED,
) -> StoredConnection:
    import hashlib

    return StoredConnection(
        user_id=user_id,
        ref=ref or ConnectionRef("green", "123"),
        credentials=ProviderCredentials(data=token.encode()),
        webhook_token_hash=hashlib.sha256(webhook_token.encode()).digest(),
        status=status,
        provider_raw_status="authorized",
    )


def test_repo_satisfies_credential_resolver_protocol():
    assert isinstance(InMemoryWhatsAppConnectionRepository(), CredentialResolver)


async def test_save_and_get_by_ref():
    repo = InMemoryWhatsAppConnectionRepository()
    conn = _stored()
    await repo.save(conn)
    fetched = await repo.get(conn.ref)
    assert fetched is conn


async def test_get_by_user():
    repo = InMemoryWhatsAppConnectionRepository()
    conn = _stored()
    await repo.save(conn)
    fetched = await repo.get_by_user("u1")
    assert fetched is conn


async def test_get_by_provider_id():
    repo = InMemoryWhatsAppConnectionRepository()
    conn = _stored()
    await repo.save(conn)
    fetched = await repo.get_by_provider_id("green", "123")
    assert fetched is conn


async def test_get_returns_none_when_absent():
    repo = InMemoryWhatsAppConnectionRepository()
    assert await repo.get(ConnectionRef("green", "nope")) is None
    assert await repo.get_by_user("nobody") is None
    assert await repo.get_by_provider_id("green", "nope") is None


async def test_get_credentials_returns_opaque_credentials():
    repo = InMemoryWhatsAppConnectionRepository()
    await repo.save(_stored(token="secret-tok"))
    creds = await repo.get_credentials(ConnectionRef("green", "123"))
    assert creds is not None
    assert creds.data == b"secret-tok"


async def test_get_credentials_returns_none_when_absent():
    repo = InMemoryWhatsAppConnectionRepository()
    assert await repo.get_credentials(ConnectionRef("green", "nope")) is None


async def test_update_status_replaces_status_and_raw():
    repo = InMemoryWhatsAppConnectionRepository()
    conn = _stored(
        status=ConnectionStatus.CONNECTED,
    )
    await repo.save(conn)
    before = await repo.get(conn.ref)
    assert before is not None
    before_updated = before.updated_at

    await repo.update_status(conn.ref, ConnectionStatus.DEGRADED, "sleepMode")
    after = await repo.get(conn.ref)
    assert after is not None
    assert after.status is ConnectionStatus.DEGRADED
    assert after.provider_raw_status == "sleepMode"
    assert after.updated_at >= before_updated


async def test_update_status_is_noop_when_absent():
    repo = InMemoryWhatsAppConnectionRepository()
    await repo.update_status(
        ConnectionRef("green", "nope"), ConnectionStatus.BLOCKED, "blocked"
    )
    # No error raised; nothing stored.
    assert await repo.get(ConnectionRef("green", "nope")) is None


async def test_save_overwrites_existing():
    repo = InMemoryWhatsAppConnectionRepository()
    await repo.save(_stored(status=ConnectionStatus.CONNECTED))
    await repo.save(_stored(status=ConnectionStatus.PAIRING_REQUIRED))
    fetched = await repo.get_by_user("u1")
    assert fetched is not None
    assert fetched.status is ConnectionStatus.PAIRING_REQUIRED


def test_stored_connection_webhook_token_hash_is_bytes_not_plaintext():
    conn = _stored(webhook_token="plaintext-secret")
    assert isinstance(conn.webhook_token_hash, bytes)
    # The hash is sha256, not the plaintext.
    assert b"plaintext-secret" not in conn.webhook_token_hash
    assert len(conn.webhook_token_hash) == 32


def test_stored_connection_credentials_are_redacted_in_repr():
    conn = _stored(token="super-secret")
    assert "super-secret" not in repr(conn)
