"""Persistent webhook inbox with processing/processed/failed statuses.

The 360dialog (Echo Business Bot) webhook previously deduplicated in memory:
``claim`` inserted the event_id into a set *before* processing. If processing
crashed mid-way, the in-memory claim was lost on restart, but a provider
retry would still see "duplicate" (if the process survived) or re-process
from scratch (if it restarted) — neither is safe.

A :class:`WebhookInbox` tracks the processing lifecycle per event:

    processing → processed   (success — future retries are duplicates)
    processing → failed      (exception — next retry can re-claim)

A stale ``processing`` entry past the lease timeout is reclaimable by
``reclaim_stale`` so a crashed worker doesn't block a provider retry forever.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.persistence.orm import BotWebhookEventRow

__all__ = [
    "InMemoryWebhookInbox",
    "PostgresWebhookInbox",
    "WebhookInbox",
]

# Status constants.
PROCESSING = "processing"
PROCESSED = "processed"
FAILED = "failed"


@runtime_checkable
class WebhookInbox(Protocol):
    """Persistent inbox tracking the processing lifecycle of webhook events.

    ``claim`` is the first call. It returns ``True`` if this caller should
    process the event (first time, or a previously-failed retry), ``False``
    if the event is already ``processed`` (a true duplicate). After
    processing, the caller must call ``succeed`` or ``fail``.
    """

    async def claim(self, key: str) -> bool:
        """Atomically claim ``key`` for processing.

        Returns ``True`` if this caller should process the event. Returns
        ``False`` if the event was already ``processed`` (a duplicate that
        should not be reprocessed).
        """
        ...

    async def succeed(self, key: str) -> None:
        """Mark ``key`` as ``processed`` (terminal success)."""
        ...

    async def fail(self, key: str, error: str | None = None) -> None:
        """Mark ``key`` as ``failed`` (re-claimable on the next retry)."""
        ...

    async def reclaim_stale(self, lease_timeout: timedelta) -> int:
        """Reclaim ``processing`` entries older than ``lease_timeout``.

        Returns the number of entries reclaimed (set back to ``failed`` so
        the next provider retry can re-claim them).
        """
        ...


class InMemoryWebhookInbox:
    """Process-local inbox for tests.

    Tracks status per event_id. ``claim`` returns ``True`` for a new key or
    a previously-failed key, ``False`` for a ``processed`` key. A
    ``processing`` key is treated as a duplicate (already being handled in
    this process).
    """

    def __init__(self) -> None:
        self._status: dict[str, str] = {}

    async def claim(self, key: str) -> bool:
        status = self._status.get(key)
        if status == PROCESSED:
            return False
        if status == PROCESSING:
            return False
        # New or previously failed — claim it.
        self._status[key] = PROCESSING
        return True

    async def succeed(self, key: str) -> None:
        self._status[key] = PROCESSED

    async def fail(self, key: str, error: str | None = None) -> None:
        self._status[key] = FAILED

    async def reclaim_stale(self, lease_timeout: timedelta) -> int:
        # In-memory: no timestamps, nothing to reclaim.
        return 0


class PostgresWebhookInbox:
    """PostgreSQL implementation of :class:`WebhookInbox`.

    Uses ``INSERT ... ON CONFLICT`` for atomic claim, and ``UPDATE ... WHERE
    status = 'processing'`` for terminal transitions (so only the owner
    transitions the row).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        session: AsyncSession | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._shared_session = session

    def _session_ctx(self) -> _SessionContext:
        if self._shared_session is not None:
            return _SessionContext(self._shared_session, owns=False)
        return _SessionContext(self._session_factory(), owns=True)

    async def claim(self, key: str) -> bool:
        """Atomically claim ``key`` for processing.

        - New key: INSERT with status=processing → returns True.
        - Existing ``failed``: UPDATE to processing → returns True.
        - Existing ``processing`` or ``processed``: returns False.
        """
        async with self._session_ctx() as session:
            # Try to insert. If the row doesn't exist, we own it.
            stmt = (
                pg_insert(BotWebhookEventRow)
                .values(event_id=key, status=PROCESSING, error=None)
                .on_conflict_do_nothing(index_elements=["event_id"])
                .returning(BotWebhookEventRow.event_id)
            )
            result = await session.execute(stmt)
            if result.scalar_one_or_none() is not None:
                return True

            # Row exists. Reclaim if it's failed.
            update_stmt = (
                text(
                    "UPDATE bot_webhook_events SET status = 'processing', "
                    "error = NULL, updated_at = now() "
                    "WHERE event_id = :key AND status = 'failed'"
                )
                .bindparams(key=key)
            )
            result = await session.execute(update_stmt)
            return result.rowcount > 0

    async def succeed(self, key: str) -> None:
        async with self._session_ctx() as session:
            stmt = (
                text(
                    "UPDATE bot_webhook_events SET status = 'processed', "
                    "error = NULL, updated_at = now() "
                    "WHERE event_id = :key AND status = 'processing'"
                )
                .bindparams(key=key)
            )
            await session.execute(stmt)

    async def fail(self, key: str, error: str | None = None) -> None:
        async with self._session_ctx() as session:
            stmt = (
                text(
                    "UPDATE bot_webhook_events SET status = 'failed', "
                    "error = :error, updated_at = now() "
                    "WHERE event_id = :key AND status = 'processing'"
                )
                .bindparams(key=key, error=error)
            )
            await session.execute(stmt)

    async def reclaim_stale(self, lease_timeout: timedelta) -> int:
        cutoff = datetime.now(timezone.utc) - lease_timeout
        async with self._session_ctx() as session:
            stmt = (
                text(
                    "UPDATE bot_webhook_events SET status = 'failed', "
                    "error = 'reclaimed stale processing lease', "
                    "updated_at = now() "
                    "WHERE status = 'processing' AND updated_at < :cutoff"
                )
                .bindparams(cutoff=cutoff)
            )
            result = await session.execute(stmt)
            return result.rowcount


class _SessionContext:
    """Async context manager for session lifecycle (standalone vs shared)."""

    def __init__(self, session: AsyncSession | None, *, owns: bool) -> None:
        if session is None:  # pragma: no cover
            raise RuntimeError("No session available")
        self._session = session
        self._owns = owns

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if not self._owns:
            return
        try:
            if exc_type is None:
                await self._session.commit()
            else:
                await self._session.rollback()
        finally:
            await self._session.close()


# Structural check: InMemoryWebhookInbox satisfies the protocol.
_: WebhookInbox = InMemoryWebhookInbox()  # type: ignore[assignment]
