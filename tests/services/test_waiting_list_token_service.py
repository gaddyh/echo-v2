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
