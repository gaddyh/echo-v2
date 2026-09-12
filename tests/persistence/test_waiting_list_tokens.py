"""Tests for the waiting-list session token repository (in-memory)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.persistence.waiting_list_tokens import (
    InMemoryWaitingListSessionRepository,
)

pytestmark = pytest.mark.asyncio
USER_ID = "user-1"


async def test_create_returns_session_id_and_raw_token():
    repo = InMemoryWaitingListSessionRepository()
    session_id, raw_token = await repo.create(user_id=USER_ID, ttl_hours=48)
    assert isinstance(session_id, str)
    assert len(session_id) > 0
    assert isinstance(raw_token, str)
    # token_urlsafe(32) is ~43 chars.
    assert len(raw_token) >= 40


async def test_validate_returns_session_for_correct_token():
    repo = InMemoryWaitingListSessionRepository()
    session_id, raw_token = await repo.create(user_id=USER_ID, ttl_hours=48)
    result = await repo.validate(raw_token)
    assert result is not None
    assert result[0] == session_id
    assert result[1] == USER_ID


async def test_validate_rejects_wrong_token():
    repo = InMemoryWaitingListSessionRepository()
    await repo.create(user_id=USER_ID, ttl_hours=48)
    result = await repo.validate("wrong-token")
    assert result is None


async def test_validate_rejects_expired_token():
    repo = InMemoryWaitingListSessionRepository()
    session_id, raw_token = await repo.create(user_id=USER_ID, ttl_hours=48)
    # Manually expire.
    repo._sessions[session_id]["expires_at"] = datetime.now(timezone.utc) - timedelta(hours=1)
    result = await repo.validate(raw_token)
    assert result is None


async def test_validate_rejects_revoked_token():
    repo = InMemoryWaitingListSessionRepository()
    session_id, raw_token = await repo.create(user_id=USER_ID, ttl_hours=48)
    await repo.revoke(session_id)
    result = await repo.validate(raw_token)
    assert result is None


async def test_get_by_id_returns_session_for_active():
    repo = InMemoryWaitingListSessionRepository()
    session_id, _ = await repo.create(user_id=USER_ID, ttl_hours=48)
    result = await repo.get_by_id(session_id)
    assert result is not None
    assert result[0] == session_id
    assert result[1] == USER_ID


async def test_get_by_id_rejects_expired():
    repo = InMemoryWaitingListSessionRepository()
    session_id, _ = await repo.create(user_id=USER_ID, ttl_hours=48)
    repo._sessions[session_id]["expires_at"] = datetime.now(timezone.utc) - timedelta(hours=1)
    result = await repo.get_by_id(session_id)
    assert result is None


async def test_get_by_id_rejects_revoked():
    repo = InMemoryWaitingListSessionRepository()
    session_id, _ = await repo.create(user_id=USER_ID, ttl_hours=48)
    await repo.revoke(session_id)
    result = await repo.get_by_id(session_id)
    assert result is None


async def test_mark_opened_sets_timestamp_once():
    repo = InMemoryWaitingListSessionRepository()
    session_id, _ = await repo.create(user_id=USER_ID, ttl_hours=48)
    assert repo._sessions[session_id]["opened_at"] is None
    await repo.mark_opened(session_id)
    first = repo._sessions[session_id]["opened_at"]
    assert first is not None
    await repo.mark_opened(session_id)
    # Idempotent — same value.
    assert repo._sessions[session_id]["opened_at"] == first


async def test_touch_updates_last_action_at():
    repo = InMemoryWaitingListSessionRepository()
    session_id, _ = await repo.create(user_id=USER_ID, ttl_hours=48)
    assert repo._sessions[session_id]["last_action_at"] is None
    await repo.touch(session_id)
    assert repo._sessions[session_id]["last_action_at"] is not None


async def test_revoke_sets_revoked_at():
    repo = InMemoryWaitingListSessionRepository()
    session_id, _ = await repo.create(user_id=USER_ID, ttl_hours=48)
    assert repo._sessions[session_id]["revoked_at"] is None
    await repo.revoke(session_id)
    assert repo._sessions[session_id]["revoked_at"] is not None


async def test_cleanup_expired_deletes_expired_sessions():
    repo = InMemoryWaitingListSessionRepository()
    sid1, _ = await repo.create(user_id=USER_ID, ttl_hours=48)
    sid2, _ = await repo.create(user_id=USER_ID, ttl_hours=48)
    # Expire sid1.
    repo._sessions[sid1]["expires_at"] = datetime.now(timezone.utc) - timedelta(hours=1)
    now = datetime.now(timezone.utc)
    deleted = await repo.cleanup_expired(now=now, batch_size=100)
    assert deleted == 1
    assert sid1 not in repo._sessions
    assert sid2 in repo._sessions


async def test_raw_token_not_stored():
    """Only the hash is stored, never the raw token."""
    repo = InMemoryWaitingListSessionRepository()
    session_id, raw_token = await repo.create(user_id=USER_ID, ttl_hours=48)
    row = repo._sessions[session_id]
    # The stored hash is bytes, not the raw string.
    assert isinstance(row["token_hash"], bytes)
    assert row["token_hash"] != raw_token.encode("utf-8")
    # The raw token should not appear anywhere in the row.
    for v in row.values():
        if isinstance(v, bytes):
            assert raw_token.encode("utf-8") not in v
