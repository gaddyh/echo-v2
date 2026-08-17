"""Unit of Work: one AsyncSession/transaction shared across repositories.

Used when an application operation must atomically touch more than one
repository. The canonical example is the Green state-webhook flow:

    webhook arrives
        ↓
    claim provider_webhook_event        ← webhook repo
        ↓
    update whatsapp_connection status   ← connection repo

If ``claim()`` commits and the process crashes before ``update_status()``,
Green retries the webhook but we reject it as a duplicate — the state
transition is permanently lost. ``PostgresUnitOfWork`` makes the two
operations one transaction.

Repositories in UoW mode (constructed with a shared session) do NOT commit
individually; the UoW owns the transaction boundary (commit on clean exit,
rollback on exception). Repositories still offer standalone methods (which
open their own short session) for callers that don't need cross-repo
atomicity.

A proper inbox/outbox pattern is a deliberate later evolution; the shared
transaction UoW is the right size for MVP and doesn't preclude it.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from echo_v2.persistence.credential_cipher import (
    CredentialCipher,
    IdentityCredentialCipher,
)
from echo_v2.persistence.postgres_idempotency import PostgresIdempotencyStore
from echo_v2.persistence.postgres_webhook_dedup import PostgresWebhookDedupStore
from echo_v2.persistence.postgres_whatsapp_connections import (
    PostgresWhatsAppConnectionRepository,
)

__all__ = ["PostgresUnitOfWork", "UnitOfWorkRepos"]


@dataclass
class UnitOfWorkRepos:
    """Repositories bound to a single shared session.

    Constructed by :class:`PostgresUnitOfWork`; callers access repos via
    ``uow.connections``, ``uow.webhooks``, ``uow.idempotency``.
    """

    connections: PostgresWhatsAppConnectionRepository
    webhooks: PostgresWebhookDedupStore
    idempotency: PostgresIdempotencyStore


class PostgresUnitOfWork:
    """One AsyncSession + one transaction, shared across repositories.

    Usage::

        async with PostgresUnitOfWork(session_factory) as uow:
            claimed = await uow.webhooks.claim(event_id, ...)
            if not claimed:
                return  # duplicate
            await uow.connections.update_status(ref, status, raw)
        # commit happens on clean __aexit__; rollback on exception.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        cipher: CredentialCipher | None = None,
        lease_seconds: int = 30,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher or IdentityCredentialCipher()
        self._lease_seconds = lease_seconds
        self._session: AsyncSession | None = None
        self.repos: UnitOfWorkRepos | None = None

    async def __aenter__(self) -> PostgresUnitOfWork:  # noqa: PYI034
        self._session = self._session_factory()
        assert self._session is not None
        self.repos = UnitOfWorkRepos(
            connections=PostgresWhatsAppConnectionRepository(
                self._session_factory,
                self._cipher,
                session=self._session,
            ),
            webhooks=PostgresWebhookDedupStore(
                self._session_factory,
                session=self._session,
            ),
            idempotency=PostgresIdempotencyStore(
                self._session_factory,
                session=self._session,
                lease_seconds=self._lease_seconds,
            ),
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        assert self._session is not None
        try:
            if exc_type is None:
                await self._session.commit()
            else:
                await self._session.rollback()
        finally:
            await self._session.close()
            self._session = None
            self.repos = None

    # Convenience properties so callers can write `uow.connections` directly.
    @property
    def connections(self) -> PostgresWhatsAppConnectionRepository:
        assert self.repos is not None, "UnitOfWork not entered"
        return self.repos.connections

    @property
    def webhooks(self) -> PostgresWebhookDedupStore:
        assert self.repos is not None, "UnitOfWork not entered"
        return self.repos.webhooks

    @property
    def idempotency(self) -> PostgresIdempotencyStore:
        assert self.repos is not None, "UnitOfWork not entered"
        return self.repos.idempotency
