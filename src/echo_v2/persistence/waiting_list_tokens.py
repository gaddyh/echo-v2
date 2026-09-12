"""Waiting-list session repository — opaque token hash storage.

Stores SHA-256 hashes of one-time URL tokens for the waiting-list mini
web app. The raw token is never persisted. A token is exchanged for a
Secure HttpOnly cookie on first page open; subsequent API calls use the
session id from the cookie.

Two implementations:
* :class:`InMemoryWaitingListSessionRepository` — for tests.
* :class:`PostgresWaitingListSessionRepository` — production.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.persistence.orm import WaitingListSessionRow

__all__ = [
    "InMemoryWaitingListSessionRepository",
    "PostgresWaitingListSessionRepository",
    "WaitingListSessionRepository",
]


def _hash_token(raw: str) -> bytes:
    """Hash a raw token with SHA-256. Returns 32 bytes."""
    return hashlib.sha256(raw.encode("utf-8")).digest()


def _generate_token() -> str:
    """Generate a cryptographically secure URL-safe token (~43 chars)."""
    return secrets.token_urlsafe(32)


@runtime_checkable
class WaitingListSessionRepository(Protocol):
    """Manage waiting-list web sessions backed by opaque token hashes."""

    async def create(
        self,
        *,
        user_id: str,
        ttl_hours: int = 48,
    ) -> tuple[str, str]:
        """Create a new session. Returns ``(session_id, raw_token)``.

        The raw token is returned once and never stored. Only its
        SHA-256 hash is persisted.
        """
        ...

    async def validate(self, raw_token: str) -> tuple[str, str] | None:
        """Validate a raw token. Returns ``(session_id, user_id)`` or ``None``.

        Checks: token hash exists, not revoked, not expired.
        Does NOT mark as opened — call :meth:`mark_opened` separately.
        """
        ...

    async def get_by_id(self, session_id: str) -> tuple[str, str] | None:
        """Look up a session by id (for cookie-based auth).

        Returns ``(session_id, user_id)`` if the session is active
        (not revoked, not expired), else ``None``.
        """
        ...

    async def mark_opened(self, session_id: str) -> None:
        """Set ``opened_at`` if NULL. Idempotent."""
        ...

    async def touch(self, session_id: str) -> None:
        """Set ``last_action_at`` to now."""
        ...

    async def revoke(self, session_id: str) -> None:
        """Revoke a session."""
        ...

    async def cleanup_expired(self, *, now: datetime, batch_size: int = 100) -> int:
        """Delete expired sessions. Returns count deleted.

        Called by a scheduled background task, NOT on every validation.
        """
        ...


# --- InMemoryWaitingListSessionRepository -----------------------------------


class InMemoryWaitingListSessionRepository:
    """In-memory implementation for tests."""

    def __init__(self) -> None:
        self._sessions: dict[str, dict] = {}  # session_id -> row dict

    async def create(
        self,
        *,
        user_id: str,
        ttl_hours: int = 48,
    ) -> tuple[str, str]:
        raw = _generate_token()
        token_hash = _hash_token(raw)
        session_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        self._sessions[session_id] = {
            "id": session_id,
            "token_hash": token_hash,
            "user_id": user_id,
            "created_at": now,
            "expires_at": now + timedelta(hours=ttl_hours),
            "opened_at": None,
            "last_action_at": None,
            "revoked_at": None,
        }
        return session_id, raw

    async def validate(self, raw_token: str) -> tuple[str, str] | None:
        token_hash = _hash_token(raw_token)
        now = datetime.now(timezone.utc)
        for row in self._sessions.values():
            if row["token_hash"] == token_hash:
                if row["revoked_at"] is not None:
                    return None
                if row["expires_at"] <= now:
                    return None
                return (row["id"], row["user_id"])
        return None

    async def get_by_id(self, session_id: str) -> tuple[str, str] | None:
        row = self._sessions.get(session_id)
        if row is None:
            return None
        now = datetime.now(timezone.utc)
        if row["revoked_at"] is not None:
            return None
        if row["expires_at"] <= now:
            return None
        return (row["id"], row["user_id"])

    async def mark_opened(self, session_id: str) -> None:
        row = self._sessions.get(session_id)
        if row is not None and row["opened_at"] is None:
            row["opened_at"] = datetime.now(timezone.utc)

    async def touch(self, session_id: str) -> None:
        row = self._sessions.get(session_id)
        if row is not None:
            row["last_action_at"] = datetime.now(timezone.utc)

    async def revoke(self, session_id: str) -> None:
        row = self._sessions.get(session_id)
        if row is not None and row["revoked_at"] is None:
            row["revoked_at"] = datetime.now(timezone.utc)

    async def cleanup_expired(self, *, now: datetime, batch_size: int = 100) -> int:
        expired = [
            sid for sid, row in self._sessions.items() if row["expires_at"] <= now
        ]
        deleted = 0
        for sid in expired[:batch_size]:
            del self._sessions[sid]
            deleted += 1
        return deleted


# --- PostgresWaitingListSessionRepository ------------------------------------


class _SessionContext:
    """Context manager for an AsyncSession."""

    def __init__(self, session: AsyncSession, *, owns: bool) -> None:
        self._session = session
        self._owns = owns

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc: object) -> None:
        if self._owns:
            await self._session.commit()
            await self._session.close()


class PostgresWaitingListSessionRepository:
    """PostgreSQL implementation of :class:`WaitingListSessionRepository`."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        session: AsyncSession | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._shared_session = session

    def _session(self) -> _SessionContext:
        if self._shared_session is not None:
            return _SessionContext(self._shared_session, owns=False)
        return _SessionContext(self._session_factory(), owns=True)

    async def create(
        self,
        *,
        user_id: str,
        ttl_hours: int = 48,
    ) -> tuple[str, str]:
        raw = _generate_token()
        token_hash = _hash_token(raw)
        session_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=ttl_hours)
        async with self._session() as session:
            stmt = (
                pg_insert(WaitingListSessionRow)
                .values(
                    id=session_id,
                    token_hash=token_hash,
                    user_id=user_id,
                    expires_at=expires_at,
                )
                .on_conflict_do_nothing(
                    index_elements=["token_hash"],
                )
                .returning(WaitingListSessionRow.id)
            )
            result = await session.execute(stmt)
            row_id = result.scalar_one_or_none()
            if row_id is None:
                # Extremely unlikely collision on token_hash — retry once.
                return await self.create(user_id=user_id, ttl_hours=ttl_hours)
            return (str(row_id), raw)

    async def validate(self, raw_token: str) -> tuple[str, str] | None:
        token_hash = _hash_token(raw_token)
        now = datetime.now(timezone.utc)
        async with self._session() as session:
            stmt = select(WaitingListSessionRow).where(
                WaitingListSessionRow.token_hash == token_hash,
                WaitingListSessionRow.revoked_at.is_(None),
                WaitingListSessionRow.expires_at > now,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            return (str(row.id), str(row.user_id))

    async def get_by_id(self, session_id: str) -> tuple[str, str] | None:
        now = datetime.now(timezone.utc)
        async with self._session() as session:
            stmt = select(WaitingListSessionRow).where(
                WaitingListSessionRow.id == session_id,
                WaitingListSessionRow.revoked_at.is_(None),
                WaitingListSessionRow.expires_at > now,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            return (str(row.id), str(row.user_id))

    async def mark_opened(self, session_id: str) -> None:
        async with self._session() as session:
            stmt = (
                select(WaitingListSessionRow)
                .where(
                    WaitingListSessionRow.id == session_id,
                    WaitingListSessionRow.opened_at.is_(None),
                )
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is not None:
                row.opened_at = datetime.now(timezone.utc)

    async def touch(self, session_id: str) -> None:
        async with self._session() as session:
            stmt = select(WaitingListSessionRow).where(
                WaitingListSessionRow.id == session_id
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is not None:
                row.last_action_at = datetime.now(timezone.utc)

    async def revoke(self, session_id: str) -> None:
        async with self._session() as session:
            stmt = select(WaitingListSessionRow).where(
                WaitingListSessionRow.id == session_id,
                WaitingListSessionRow.revoked_at.is_(None),
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is not None:
                row.revoked_at = datetime.now(timezone.utc)

    async def cleanup_expired(self, *, now: datetime, batch_size: int = 100) -> int:
        async with self._session() as session:
            # PostgreSQL supports DELETE ... USING ... LIMIT via a CTE.
            # SQLAlchemy's delete().limit() is not supported on PostgreSQL,
            # so we use a subquery to select the ids to delete.
            from sqlalchemy import select as sa_select

            ids_to_delete = (
                sa_select(WaitingListSessionRow.id)
                .where(WaitingListSessionRow.expires_at <= now)
                .limit(batch_size)
                .subquery()
            )
            stmt = sa_delete(WaitingListSessionRow).where(
                WaitingListSessionRow.id.in_(select(ids_to_delete.c.id))
            )
            result = await session.execute(stmt)
            return result.rowcount
