"""Tests for the WaitingListTokenService."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.persistence.waiting_list_tokens import (
    InMemoryWaitingListSessionRepository,
)
from echo_v2.services.waiting_list_token_service import (
    ResolvedWaitingSession,
    WaitingListTokenService,
)

pytestmark = pytest.mark.asyncio
USER_ID = "user-1"


def _make_service(ttl_hours: int = 48) -> WaitingListTokenService:
    return WaitingListTokenService(
        InMemoryWaitingListSessionRepository(),
        ttl_hours=ttl_hours,
    )


async def test_issue_returns_session_id_and_raw_token():
    service = _make_service()
    session_id, raw_token = await service.issue(USER_ID)
    assert isinstance(session_id, str)
    assert isinstance(raw_token, str)
    assert len(raw_token) >= 40


async def test_resolve_returns_resolved_session():
    service = _make_service()
    session_id, raw_token = await service.issue(USER_ID)
    resolved = await service.resolve(raw_token)
    assert resolved is not None
    assert isinstance(resolved, ResolvedWaitingSession)
    assert resolved.session_id == session_id
    assert resolved.user_id == USER_ID


async def test_resolve_rejects_invalid_token():
    service = _make_service()
    resolved = await service.resolve("invalid-token")
    assert resolved is None


async def test_resolve_rejects_expired_token():
    service = _make_service(ttl_hours=0)
    _session_id, raw_token = await service.issue(USER_ID)
    # The session is already expired (ttl=0 means expires_at = now).
    # But the in-memory repo checks expires_at > now, so a 0-hour TTL
    # creates a session that expires immediately. Let's use a short TTL
    # and wait. Actually, with ttl_hours=0, expires_at = now, which is
    # not > now. So validate returns None.
    resolved = await service.resolve(raw_token)
    assert resolved is None


async def test_resolve_marks_session_opened():
    service = _make_service()
    session_id, raw_token = await service.issue(USER_ID)
    await service.resolve(raw_token)
    # The repo's opened_at should be set.
    repo = service._repo  # type: ignore[attr-defined]
    assert repo._sessions[session_id]["opened_at"] is not None  # type: ignore[index]


async def test_resolve_session_returns_resolved_session():
    service = _make_service()
    session_id, _ = await service.issue(USER_ID)
    resolved = await service.resolve_session(session_id)
    assert resolved is not None
    assert resolved.session_id == session_id
    assert resolved.user_id == USER_ID


async def test_resolve_session_rejects_invalid_id():
    service = _make_service()
    resolved = await service.resolve_session("nonexistent")
    assert resolved is None


async def test_resolve_session_does_not_mark_opened():
    service = _make_service()
    session_id, _ = await service.issue(USER_ID)
    await service.resolve_session(session_id)
    repo = service._repo  # type: ignore[attr-defined]
    # resolve_session should NOT mark opened (only resolve does).
    assert repo._sessions[session_id]["opened_at"] is None  # type: ignore[index]


async def test_touch_updates_last_action_at():
    service = _make_service()
    session_id, _ = await service.issue(USER_ID)
    await service.touch(session_id)
    repo = service._repo  # type: ignore[attr-defined]
    assert repo._sessions[session_id]["last_action_at"] is not None  # type: ignore[index]


async def test_revoke_invalidates_session():
    service = _make_service()
    session_id, _ = await service.issue(USER_ID)
    await service.revoke(session_id)
    resolved = await service.resolve_session(session_id)
    assert resolved is None


async def test_cleanup_expired_returns_count():
    service = _make_service(ttl_hours=0)
    await service.issue(USER_ID)
    await service.issue(USER_ID)
    now = datetime.now(timezone.utc) + timedelta(hours=1)
    deleted = await service.cleanup_expired(now=now, batch_size=100)
    assert deleted == 2


# --- Edge cases ---


async def test_resolve_after_revoke_returns_none():
    """A revoked token cannot be resolved."""
    service = _make_service()
    _session_id, raw_token = await service.issue(USER_ID)
    await service.revoke(_session_id)
    resolved = await service.resolve(raw_token)
    assert resolved is None


async def test_resolve_session_after_revoke_returns_none():
    """A revoked session cannot be resolved via cookie."""
    service = _make_service()
    session_id, _ = await service.issue(USER_ID)
    await service.revoke(session_id)
    resolved = await service.resolve_session(session_id)
    assert resolved is None


async def test_resolve_empty_token_returns_none():
    """An empty string token returns None."""
    service = _make_service()
    resolved = await service.resolve("")
    assert resolved is None


async def test_resolve_wrong_token_format_returns_none():
    """A token with wrong format returns None."""
    service = _make_service()
    resolved = await service.resolve("not-a-valid-base64url-token!")
    assert resolved is None


async def test_issue_multiple_tokens_for_same_user():
    """Multiple tokens for the same user are all valid."""
    service = _make_service()
    sid1, token1 = await service.issue(USER_ID)
    sid2, token2 = await service.issue(USER_ID)
    assert sid1 != sid2
    assert token1 != token2
    # Both resolve.
    r1 = await service.resolve(token1)
    r2 = await service.resolve(token2)
    assert r1 is not None and r2 is not None
    assert r1.session_id == sid1
    assert r2.session_id == sid2


async def test_resolve_token_for_different_users():
    """Tokens for different users resolve to their respective users."""
    service = _make_service()
    _, token_a = await service.issue("user-a")
    _, token_b = await service.issue("user-b")
    ra = await service.resolve(token_a)
    rb = await service.resolve(token_b)
    assert ra is not None and rb is not None
    assert ra.user_id == "user-a"
    assert rb.user_id == "user-b"


async def test_touch_nonexistent_session_is_noop():
    """Touching a non-existent session does not raise."""
    service = _make_service()
    await service.touch("nonexistent-session-id")


async def test_revoke_nonexistent_session_is_noop():
    """Revoking a non-existent session does not raise."""
    service = _make_service()
    await service.revoke("nonexistent-session-id")


async def test_resolve_session_with_none_id():
    """resolve_session with empty string returns None."""
    service = _make_service()
    resolved = await service.resolve_session("")
    assert resolved is None


async def test_cleanup_expired_with_no_sessions():
    """Cleanup with no sessions returns 0."""
    service = _make_service()
    now = datetime.now(timezone.utc)
    deleted = await service.cleanup_expired(now=now, batch_size=100)
    assert deleted == 0


async def test_cleanup_expired_preserves_active_sessions():
    """Cleanup only deletes expired sessions, not active ones."""
    service = _make_service(ttl_hours=48)
    await service.issue(USER_ID)
    now = datetime.now(timezone.utc)
    deleted = await service.cleanup_expired(now=now, batch_size=100)
    assert deleted == 0


async def test_cleanup_expired_with_batch_size():
    """Cleanup respects batch_size."""
    service = _make_service(ttl_hours=0)
    for _ in range(5):
        await service.issue(USER_ID)
    now = datetime.now(timezone.utc) + timedelta(hours=1)
    deleted = await service.cleanup_expired(now=now, batch_size=3)
    assert deleted == 3
    # Second call gets the rest.
    deleted2 = await service.cleanup_expired(now=now, batch_size=3)
    assert deleted2 == 2


async def test_mark_opened_is_idempotent():
    """Calling resolve twice marks opened only once (idempotent)."""
    service = _make_service()
    session_id, raw_token = await service.issue(USER_ID)
    await service.resolve(raw_token)
    first_opened = service._repo._sessions[session_id]["opened_at"]  # type: ignore[index]
    # Resolve again (token still valid).
    await service.resolve(raw_token)
    second_opened = service._repo._sessions[session_id]["opened_at"]  # type: ignore[index]
    assert first_opened == second_opened
