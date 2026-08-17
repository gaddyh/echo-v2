"""Postgres-backed webhook deduplication store.

Satisfies :class:`echo_v2.app.webhooks.dedup.WebhookDedupStore` against
PostgreSQL. ``claim(key)`` is an ``INSERT ... ON CONFLICT DO NOTHING`` —
returns ``True`` iff a row was inserted (first claim), ``False`` on
duplicate.

``connection_id`` is NOT NULL: an unknown instance is rejected at auth
(no matching ``webhook_token_hash``) before reaching the dedup store, so
every authenticated dedupe record has a connection. The optional metadata
kwargs (``provider``, ``connection_id``, ``event_type``) are stored for
operational visibility; the single-arg ``claim(key)`` form remains
backward compatible (metadata defaults to ``None`` — but ``connection_id``
is required by the schema, so callers using the Postgres store must supply
it).

Session handling mirrors the connection repository: standalone by default,
shared session in UoW mode (so ``claim`` + ``update_status`` can be atomic).
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from echo_v2.persistence.orm import ProviderWebhookEventRow

__all__ = ["PostgresWebhookDedupStore"]


class PostgresWebhookDedupStore:
    """PostgreSQL implementation of :class:`WebhookDedupStore`."""

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

    async def claim(
        self,
        key: str,
        *,
        provider: str | None = None,
        connection_id: str | None = None,
        event_type: str | None = None,
    ) -> bool:
        """Atomically claim ``key``. ``True`` iff this is the first claim.

        For the Postgres store, ``connection_id`` is required by the schema
        (NOT NULL). Callers using the Postgres store must supply it; the
        single-arg form is kept for backward compatibility with the protocol
        but will raise an integrity error if ``connection_id`` is omitted.
        """
        if connection_id is None:
            raise ValueError(
                "PostgresWebhookDedupStore.claim requires connection_id "
                "(the provider_webhook_events.connection_id column is NOT NULL)."
            )
        if provider is None:
            raise ValueError(
                "PostgresWebhookDedupStore.claim requires provider."
            )

        async with self._session() as session:
            stmt = (
                pg_insert(ProviderWebhookEventRow)
                .values(
                    event_id=key,
                    provider=provider,
                    connection_id=connection_id,
                    event_type=event_type,
                )
                .on_conflict_do_nothing(index_elements=["event_id"])
                .returning(ProviderWebhookEventRow.event_id)
            )
            result = await session.execute(stmt)
            inserted = result.scalar_one_or_none()
            return inserted is not None


class _SessionContext:
    """Async context manager for session lifecycle (standalone vs UoW)."""

    def __init__(self, session: AsyncSession, *, owns: bool) -> None:
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


# Structural check: PostgresWebhookDedupStore satisfies the protocol.
# (Instantiation requires a session_factory; checked at runtime in tests.)
