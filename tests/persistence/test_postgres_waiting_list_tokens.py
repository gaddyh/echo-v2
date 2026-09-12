"""Postgres integration tests for the waiting-list session repository.

Uses testcontainers — skips locally if Docker is unavailable.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from .conftest import insert_user

pytestmark = pytest.mark.asyncio


async def test_pg_session_create_and_validate(waiting_list_sessions_repo, session_factory):
    """Create a session, validate the raw token."""
    user_id = await insert_user(session_factory)
    session_id, raw_token = await waiting_list_sessions_repo.create(
        user_id=user_id, ttl_hours=48
    )
    assert isinstance(session_id, str)
    assert isinstance(raw_token, str)
    assert len(raw_token) >= 40

    result = await waiting_list_sessions_repo.validate(raw_token)
    assert result is not None
    assert result[0] == session_id
    assert result[1] == user_id


async def test_pg_session_validate_rejects_wrong_token(waiting_list_sessions_repo, session_factory):
    user_id = await insert_user(session_factory)
    await waiting_list_sessions_repo.create(user_id=user_id, ttl_hours=48)
    result = await waiting_list_sessions_repo.validate("wrong-token")
    assert result is None


async def test_pg_session_validate_rejects_expired(waiting_list_sessions_repo, session_factory):
    user_id = await insert_user(session_factory)
    session_id, raw_token = await waiting_list_sessions_repo.create(
        user_id=user_id, ttl_hours=48
    )
    # Manually expire via SQL.
    from sqlalchemy import text

    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE waiting_list_sessions SET expires_at = now() - interval '1 hour' "
                "WHERE id = :id"
            ),
            {"id": session_id},
        )
        await session.commit()
    result = await waiting_list_sessions_repo.validate(raw_token)
    assert result is None


async def test_pg_session_validate_rejects_revoked(waiting_list_sessions_repo, session_factory):
    user_id = await insert_user(session_factory)
    session_id, raw_token = await waiting_list_sessions_repo.create(
        user_id=user_id, ttl_hours=48
    )
    await waiting_list_sessions_repo.revoke(session_id)
    result = await waiting_list_sessions_repo.validate(raw_token)
    assert result is None


async def test_pg_session_get_by_id(waiting_list_sessions_repo, session_factory):
    user_id = await insert_user(session_factory)
    session_id, _ = await waiting_list_sessions_repo.create(
        user_id=user_id, ttl_hours=48
    )
    result = await waiting_list_sessions_repo.get_by_id(session_id)
    assert result is not None
    assert result[0] == session_id
    assert result[1] == user_id


async def test_pg_session_get_by_id_rejects_expired(waiting_list_sessions_repo, session_factory):
    user_id = await insert_user(session_factory)
    session_id, _ = await waiting_list_sessions_repo.create(
        user_id=user_id, ttl_hours=48
    )
    from sqlalchemy import text

    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE waiting_list_sessions SET expires_at = now() - interval '1 hour' "
                "WHERE id = :id"
            ),
            {"id": session_id},
        )
        await session.commit()
    result = await waiting_list_sessions_repo.get_by_id(session_id)
    assert result is None


async def test_pg_session_mark_opened(waiting_list_sessions_repo, session_factory):
    user_id = await insert_user(session_factory)
    session_id, _ = await waiting_list_sessions_repo.create(
        user_id=user_id, ttl_hours=48
    )
    await waiting_list_sessions_repo.mark_opened(session_id)
    # Idempotent — calling again should not error.
    await waiting_list_sessions_repo.mark_opened(session_id)


async def test_pg_session_touch(waiting_list_sessions_repo, session_factory):
    user_id = await insert_user(session_factory)
    session_id, _ = await waiting_list_sessions_repo.create(
        user_id=user_id, ttl_hours=48
    )
    await waiting_list_sessions_repo.touch(session_id)


async def test_pg_session_revoke(waiting_list_sessions_repo, session_factory):
    user_id = await insert_user(session_factory)
    session_id, _ = await waiting_list_sessions_repo.create(
        user_id=user_id, ttl_hours=48
    )
    await waiting_list_sessions_repo.revoke(session_id)
    # Idempotent — calling again should not error.
    await waiting_list_sessions_repo.revoke(session_id)


async def test_pg_session_cleanup_expired(waiting_list_sessions_repo, session_factory):
    user_id = await insert_user(session_factory)
    sid1, _ = await waiting_list_sessions_repo.create(user_id=user_id, ttl_hours=48)
    _sid2, _ = await waiting_list_sessions_repo.create(user_id=user_id, ttl_hours=48)
    # Expire sid1.
    from sqlalchemy import text

    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE waiting_list_sessions SET expires_at = now() - interval '1 hour' "
                "WHERE id = :id"
            ),
            {"id": sid1},
        )
        await session.commit()
    now = datetime.now(timezone.utc)
    deleted = await waiting_list_sessions_repo.cleanup_expired(now=now, batch_size=100)
    assert deleted == 1


async def test_pg_session_raw_token_not_stored(waiting_list_sessions_repo, session_factory):
    """Only the SHA-256 hash is stored, never the raw token."""
    user_id = await insert_user(session_factory)
    _session_id, raw_token = await waiting_list_sessions_repo.create(
        user_id=user_id, ttl_hours=48
    )
    from sqlalchemy import text

    async with session_factory() as session:
        result = await session.execute(
            text("SELECT token_hash FROM waiting_list_sessions LIMIT 1")
        )
        token_hash = result.scalar_one()
    assert isinstance(token_hash, (bytes, bytearray))
    assert token_hash != raw_token.encode("utf-8")
    assert raw_token.encode("utf-8") not in bytes(token_hash)
