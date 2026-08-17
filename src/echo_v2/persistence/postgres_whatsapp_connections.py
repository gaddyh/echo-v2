"""Postgres-backed WhatsApp connection repository.

Satisfies :class:`echo_v2.persistence.whatsapp_connections.WhatsAppConnectionRepository`
against PostgreSQL via SQLAlchemy 2 async. Translates between the domain
:class:`StoredConnection` dataclass and :class:`WhatsAppConnectionRow` ORM
objects; ORM objects never escape this module.

Session handling (see plan, "Unit of Work"):
* ``session=None`` (default) → standalone mode: each method opens its own
  short session and commits. Used by simple callers and most tests.
* ``session=<shared>`` → UoW mode: methods use the shared session and do
  NOT commit/rollback; the enclosing :class:`PostgresUnitOfWork` owns the
  transaction boundary.

``save`` is an **upsert on ``(user_id, provider)``**: on reconnect (same
user + provider, new ``provider_connection_id``), the existing row is
updated — credentials, webhook hash, and status are replaced. This is the
MVP invariant: one connection per provider per user.

``credentials`` BYTEA stores **encrypted** bytes via the injected
:class:`CredentialCipher` (encrypt on save, decrypt on retrieve). The
plaintext :class:`ProviderCredentials.data` never touches the column.
``updated_at`` is set explicitly by every UPDATE (no trigger magic).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from echo_v2.persistence.credential_cipher import (
    CredentialCipher,
    IdentityCredentialCipher,
)
from echo_v2.persistence.orm import WhatsAppConnectionRow
from echo_v2.persistence.whatsapp_connections import (
    StoredConnection,
    WhatsAppConnectionRepository,
)
from echo_v2.ports.whatsapp import (
    ConnectionRef,
    ConnectionStatus,
    ProviderCredentials,
)

__all__ = ["PostgresWhatsAppConnectionRepository"]


class PostgresWhatsAppConnectionRepository(WhatsAppConnectionRepository):
    """PostgreSQL implementation of :class:`WhatsAppConnectionRepository`."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        cipher: CredentialCipher | None = None,
        *,
        session: AsyncSession | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher or IdentityCredentialCipher()
        self._shared_session = session

    # --- session helpers --------------------------------------------------

    def _session(self) -> _SessionContext:
        if self._shared_session is not None:
            return _SessionContext(self._shared_session, owns=False)
        return _SessionContext(self._session_factory(), owns=True)

    # --- protocol methods -------------------------------------------------

    async def save(self, conn: StoredConnection) -> None:
        """Upsert the connection on ``(user_id, provider)``.

        On conflict: replace ``provider_connection_id``, ``credentials``
        (encrypted), ``webhook_token_hash``, ``connection_status``,
        ``provider_raw_status``, and ``updated_at``.
        """
        encrypted = self._cipher.encrypt(conn.credentials.data)
        now = datetime.now(timezone.utc)

        async with self._session() as session:
            stmt = (
                pg_insert(WhatsAppConnectionRow)
                .values(
                    user_id=conn.user_id,
                    provider=conn.ref.provider,
                    provider_connection_id=conn.ref.provider_connection_id,
                    credentials=encrypted,
                    webhook_token_hash=conn.webhook_token_hash,
                    connection_status=conn.status.value,
                    provider_raw_status=conn.provider_raw_status,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    constraint="whatsapp_connections_user_provider_key",
                    set_={
                        "provider_connection_id": conn.ref.provider_connection_id,
                        "credentials": encrypted,
                        "webhook_token_hash": conn.webhook_token_hash,
                        "connection_status": conn.status.value,
                        "provider_raw_status": conn.provider_raw_status,
                        "updated_at": now,
                    },
                )
                .returning(WhatsAppConnectionRow.id)
            )
            result = await session.execute(stmt)
            row_id = result.scalar_one()
            # Keep the row's created_at; we only changed updated_at.
            if session.info is None:
                session.info = {}
            session.info["last_saved_id"] = str(row_id)

    async def get(self, ref: ConnectionRef) -> StoredConnection | None:
        async with self._session() as session:
            stmt = select(WhatsAppConnectionRow).where(
                WhatsAppConnectionRow.provider == ref.provider,
                WhatsAppConnectionRow.provider_connection_id == ref.provider_connection_id,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            return self._row_to_domain(row) if row else None

    async def get_by_user(self, user_id: str) -> StoredConnection | None:
        async with self._session() as session:
            stmt = select(WhatsAppConnectionRow).where(
                WhatsAppConnectionRow.user_id == user_id
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            return self._row_to_domain(row) if row else None

    async def get_by_provider_id(
        self,
        provider: str,
        provider_id: str,
    ) -> StoredConnection | None:
        async with self._session() as session:
            stmt = select(WhatsAppConnectionRow).where(
                WhatsAppConnectionRow.provider == provider,
                WhatsAppConnectionRow.provider_connection_id == provider_id,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            return self._row_to_domain(row) if row else None

    async def update_status(
        self,
        ref: ConnectionRef,
        status: ConnectionStatus,
        raw: str | None,
    ) -> None:
        now = datetime.now(timezone.utc)
        async with self._session() as session:
            stmt = (
                update(WhatsAppConnectionRow)
                .where(
                    WhatsAppConnectionRow.provider == ref.provider,
                    WhatsAppConnectionRow.provider_connection_id == ref.provider_connection_id,
                )
                .values(
                    connection_status=status.value,
                    provider_raw_status=raw,
                    updated_at=now,
                )
            )
            await session.execute(stmt)

    async def get_credentials(self, ref: ConnectionRef) -> ProviderCredentials | None:
        async with self._session() as session:
            stmt = select(
                WhatsAppConnectionRow.credentials
            ).where(
                WhatsAppConnectionRow.provider == ref.provider,
                WhatsAppConnectionRow.provider_connection_id == ref.provider_connection_id,
            )
            ciphertext = (await session.execute(stmt)).scalar_one_or_none()
            if ciphertext is None:
                return None
            return ProviderCredentials(data=self._cipher.decrypt(ciphertext))

    # --- mapping ----------------------------------------------------------

    def _row_to_domain(self, row: WhatsAppConnectionRow) -> StoredConnection:
        return StoredConnection(
            user_id=str(row.user_id),
            ref=ConnectionRef(
                provider=row.provider,
                provider_connection_id=row.provider_connection_id,
            ),
            credentials=ProviderCredentials(data=self._cipher.decrypt(row.credentials)),
            webhook_token_hash=row.webhook_token_hash,
            status=ConnectionStatus(row.connection_status),
            provider_raw_status=row.provider_raw_status,
            updated_at=row.updated_at,
        )


class _SessionContext:
    """Async context manager for session lifecycle.

    In standalone mode (owns=True), commits on clean exit and closes.
    In UoW mode (owns=False), does nothing — the UoW owns the transaction.
    """

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


# Structural check: PostgresWhatsAppConnectionRepository satisfies the protocol.
# (Instantiation requires a session_factory; checked at runtime in tests.)
