"""WaitingListTokenService — issue and resolve waiting-list web sessions.

Wraps the :class:`WaitingListSessionRepository` to provide a clean
service-level API for the digest worker (issue) and the web app routes
(resolve). The raw token is used once (``GET /q/{token}``) to set a
cookie; subsequent calls use :meth:`resolve_session` with the session
id from the cookie.
"""

from __future__ import annotations

from dataclasses import dataclass

from echo_v2.persistence.waiting_list_tokens import WaitingListSessionRepository

__all__ = ["ResolvedWaitingSession", "WaitingListTokenService"]


@dataclass(frozen=True)
class ResolvedWaitingSession:
    """A validated waiting-list session — returned by resolve/resolve_session.

    Attributes:
        session_id: The surrogate UUID of the session row. Used as the
            cookie value and as the key for per-session summary counts.
        user_id: The user who owns this session. Used for ownership-
            scoped queries and action authorization.
    """

    session_id: str
    user_id: str


class WaitingListTokenService:
    """Issue and resolve waiting-list web sessions.

    Args:
        repo: The :class:`WaitingListSessionRepository`.
        ttl_hours: Session lifetime in hours (default 48).
    """

    def __init__(
        self,
        repo: WaitingListSessionRepository,
        *,
        ttl_hours: int = 48,
    ) -> None:
        self._repo = repo
        self._ttl_hours = ttl_hours

    async def issue(self, user_id: str) -> tuple[str, str]:
        """Create a new session for a user.

        Returns ``(session_id, raw_token)``. The raw token is embedded
        in the WhatsApp URL button. Only its SHA-256 hash is stored.
        """
        return await self._repo.create(user_id=user_id, ttl_hours=self._ttl_hours)

    async def resolve(self, raw_token: str) -> ResolvedWaitingSession | None:
        """Validate a raw token (one-time URL exchange).

        Returns the resolved session, or ``None`` if the token is
        invalid, expired, or revoked. Marks the session as opened.
        """
        result = await self._repo.validate(raw_token)
        if result is None:
            return None
        session_id, user_id = result
        await self._repo.mark_opened(session_id)
        return ResolvedWaitingSession(session_id=session_id, user_id=user_id)

    async def resolve_session(self, session_id: str) -> ResolvedWaitingSession | None:
        """Validate a session by id (cookie-based auth).

        Returns the resolved session, or ``None`` if the session is
        invalid, expired, or revoked. Does NOT mark as opened (the
        session was already opened during the token exchange).
        """
        result = await self._repo.get_by_id(session_id)
        if result is None:
            return None
        sid, user_id = result
        return ResolvedWaitingSession(session_id=sid, user_id=user_id)

    async def touch(self, session_id: str) -> None:
        """Update ``last_action_at`` on the session."""
        await self._repo.touch(session_id)

    async def revoke(self, session_id: str) -> None:
        """Revoke a session."""
        await self._repo.revoke(session_id)

    async def cleanup_expired(self, *, now, batch_size: int = 100) -> int:
        """Delete expired sessions. Returns count deleted."""
        return await self._repo.cleanup_expired(now=now, batch_size=batch_size)
