"""Postgres-backed webhook dedup store tests (requires Docker)."""

from __future__ import annotations

import pytest

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


async def test_claim_first_returns_true(webhooks_repo, session_factory):
    user_id = await insert_user(session_factory)
    conn_id = await _seed_connection(session_factory, user_id)

    claimed = await webhooks_repo.claim(
        "evt-1", provider="green", connection_id=conn_id, event_type="state"
    )
    assert claimed is True


async def test_claim_duplicate_returns_false(webhooks_repo, session_factory):
    user_id = await insert_user(session_factory)
    conn_id = await _seed_connection(session_factory, user_id)

    first = await webhooks_repo.claim("evt-2", provider="green", connection_id=conn_id)
    second = await webhooks_repo.claim("evt-2", provider="green", connection_id=conn_id)
    assert first is True
    assert second is False


async def test_claim_different_keys_both_succeed(webhooks_repo, session_factory):
    user_id = await insert_user(session_factory)
    conn_id = await _seed_connection(session_factory, user_id)

    a = await webhooks_repo.claim("evt-a", provider="green", connection_id=conn_id)
    b = await webhooks_repo.claim("evt-b", provider="green", connection_id=conn_id)
    assert a is True and b is True


async def test_claim_requires_connection_id(webhooks_repo):
    with pytest.raises(ValueError, match="connection_id"):
        await webhooks_repo.claim("evt-x", provider="green")


async def test_claim_requires_provider(webhooks_repo, session_factory):
    user_id = await insert_user(session_factory)
    conn_id = await _seed_connection(session_factory, user_id)
    with pytest.raises(ValueError, match="provider"):
        await webhooks_repo.claim("evt-y", connection_id=conn_id)


async def test_claim_without_connection_id_raises_integrity(webhooks_repo, session_factory):
    """If we bypass the ValueError guard, the NOT NULL constraint fires."""
    await insert_user(session_factory)
    # Manually call the insert without connection_id to confirm the schema rejects it.
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    async with session_factory() as session:
        with pytest.raises(IntegrityError):  # NOT NULL violation
            await session.execute(
                text(
                    "INSERT INTO provider_webhook_events (event_id, provider) "
                    "VALUES ('evt-null', 'green')"
                )
            )
            await session.commit()
