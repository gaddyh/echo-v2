"""Green API instance pool repository.

Owns the ``green_instance_pool`` table: pre-created Green API instances
available for fast onboarding. Translates between the domain
:class:`PooledInstance` dataclass and :class:`GreenInstancePoolRow` ORM
objects; ORM objects never escape this module.

Lifecycle: ``creating`` → ``available`` → ``claimed`` → (deleted by
``finalize_claim``). ``failed`` is terminal (cleaned on next
``ensure_capacity``).

Capacity reservation: ``creating`` + ``available`` count toward the
target size; ``claimed`` does not (it's out of the reserve pool).

Crash safety: credentials are persisted immediately after ``createInstance``
(via ``mark_created``) — *before* the ready poll — so a crash during
waiting leaves a recoverable row with the Green instance id.

``api_token`` BYTEA stores **encrypted** bytes via the injected
:class:`CredentialCipher` (encrypt on write, decrypt on read). The
plaintext :class:`ProviderCredentials.data` never touches the column.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from echo_v2.persistence.credential_cipher import (
    CredentialCipher,
    IdentityCredentialCipher,
)
from echo_v2.persistence.orm import GreenInstancePoolRow
from echo_v2.ports.whatsapp import (
    ConnectionRef,
    ProviderCredentials,
)

__all__ = [
    "GreenInstancePoolRepository",
    "InMemoryGreenInstancePoolRepository",
    "PoolRow",
    "PooledInstance",
    "PostgresGreenInstancePoolRepository",
]


# --- Domain types ----------------------------------------------------------


@dataclass(frozen=True)
class PooledInstance:
    """A claimed pool instance ready to be turned into a connection.

    Returned by :meth:`GreenInstancePoolRepository.claim`. Carries the
    pool row id (for later ``finalize_claim``) plus the provider ref,
    decrypted credentials, and webhook token hash.
    """

    pool_row_id: str
    ref: ConnectionRef
    credentials: ProviderCredentials
    webhook_token_hash: bytes


@dataclass(frozen=True)
class PoolRow:
    """A raw pool row for recovery inspection.

    Carries the row id, state, provider_connection_id (may be None for
    ``creating`` rows before ``createInstance`` returned), and decrypted
    credentials (None when not yet persisted).
    """

    id: str
    state: str
    provider_connection_id: str | None
    credentials: ProviderCredentials | None
    webhook_token_hash: bytes | None
    claimed_by_user_id: str | None


# --- Repository protocol --------------------------------------------------


class GreenInstancePoolRepository:
    """Repository for the ``green_instance_pool`` table.

    Subclasses implement the async methods. Kept as a regular class (not
    ``Protocol``) so it can carry docstrings and be subclassed directly by
    the in-memory and Postgres impls.
    """

    async def reserve_creation_slot(self, target_size: int) -> str | None:
        """Atomically reserve a capacity slot and return a new ``creating`` row id.

        Returns ``None`` if ``count(creating + available) >= target_size`` —
        no slot available. The count + insert happen under a single lock so
        concurrent callers don't over-create.
        """

    async def mark_created(
        self,
        row_id: str,
        ref: ConnectionRef,
        credentials: ProviderCredentials,
        webhook_token_hash: bytes,
    ) -> None:
        """Persist the Green instance id + credentials while still ``creating``.

        Called immediately after ``createInstance`` returns, *before* the
        ready poll. State stays ``creating``.
        """

    async def mark_available(self, row_id: str) -> None:
        """Mark a ``creating`` row as ``available`` (Green confirmed notAuthorized)."""

    async def mark_failed(self, row_id: str) -> None:
        """Mark a row as ``failed`` (terminal — cleaned on next ensure_capacity)."""

    async def claim(self, user_id: str) -> PooledInstance | None:
        """Atomically claim one ``available`` row.

        Marks it ``claimed`` with ``claimed_by_user_id`` + ``claimed_at`` and
        returns the :class:`PooledInstance`. Returns ``None`` if no
        ``available`` row exists. Uses ``FOR UPDATE SKIP LOCKED`` so two
        concurrent claims never grab the same row.
        """

    async def finalize_claim(self, row_id: str) -> None:
        """Delete a ``claimed`` row after the connection has been persisted."""

    async def release_claim(self, row_id: str) -> None:
        """Return a ``claimed`` row back to ``available`` (recovery).

        Used when a claimed row has no matching connection AND Green still
        reports ``notAuthorized``.
        """

    async def delete_row(self, row_id: str) -> None:
        """Delete a row by id (used in recovery)."""

    async def get_by_state(self, state: str) -> list[PoolRow]:
        """Return all rows in the given state (for recovery inspection)."""
        raise NotImplementedError

    async def get_creating_with_credentials(self) -> list[PoolRow]:
        """``creating`` rows that have a provider_connection_id (crash during ready poll)."""
        raise NotImplementedError

    async def get_creating_without_credentials(self) -> list[PoolRow]:
        """``creating`` rows without a provider_connection_id (crash during createInstance)."""
        raise NotImplementedError

    async def get_failed_with_provider_id(self) -> list[PoolRow]:
        """``failed`` rows carrying a provider_connection_id (best-effort Green delete)."""
        raise NotImplementedError


# --- In-memory implementation (tests) -------------------------------------


class InMemoryGreenInstancePoolRepository(GreenInstancePoolRepository):
    """Process-local repository backed by dicts. Suitable for tests.

    Uses an :class:`asyncio.Lock` to make ``reserve_creation_slot`` atomic.
    """

    def __init__(self) -> None:
        self._rows: dict[str, PoolRow] = {}
        self._lock = asyncio.Lock()
        # Track encrypted credentials separately so PoolRow stays frozen
        # with decrypted credentials (or None).
        self._credentials: dict[str, ProviderCredentials] = {}

    async def reserve_creation_slot(self, target_size: int) -> str | None:
        async with self._lock:
            # Count creating + available + failed. Failed rows count toward
            # capacity to prevent infinite creation loops when Green
            # consistently returns an unusable state (e.g. authorized).
            # They're cleaned up on the next ensure_capacity (startup).
            active = sum(
                1 for r in self._rows.values()
                if r.state in ("creating", "available", "failed")
            )
            if active >= target_size:
                return None
            row_id = str(uuid.uuid4())
            self._rows[row_id] = PoolRow(
                id=row_id,
                state="creating",
                provider_connection_id=None,
                credentials=None,
                webhook_token_hash=None,
                claimed_by_user_id=None,
            )
            return row_id

    async def mark_created(
        self,
        row_id: str,
        ref: ConnectionRef,
        credentials: ProviderCredentials,
        webhook_token_hash: bytes,
    ) -> None:
        existing = self._rows.get(row_id)
        if existing is None:
            return
        self._credentials[row_id] = credentials
        self._rows[row_id] = PoolRow(
            id=row_id,
            state="creating",
            provider_connection_id=ref.provider_connection_id,
            credentials=credentials,
            webhook_token_hash=webhook_token_hash,
            claimed_by_user_id=None,
        )

    async def mark_available(self, row_id: str) -> None:
        existing = self._rows.get(row_id)
        if existing is None:
            return
        self._rows[row_id] = PoolRow(
            id=row_id,
            state="available",
            provider_connection_id=existing.provider_connection_id,
            credentials=existing.credentials,
            webhook_token_hash=existing.webhook_token_hash,
            claimed_by_user_id=None,
        )

    async def mark_failed(self, row_id: str) -> None:
        existing = self._rows.get(row_id)
        if existing is None:
            return
        self._rows[row_id] = PoolRow(
            id=row_id,
            state="failed",
            provider_connection_id=existing.provider_connection_id,
            credentials=existing.credentials,
            webhook_token_hash=existing.webhook_token_hash,
            claimed_by_user_id=None,
        )

    async def claim(self, user_id: str) -> PooledInstance | None:
        async with self._lock:
            for row in self._rows.values():
                if row.state != "available":
                    continue
                if row.provider_connection_id is None or row.credentials is None:
                    continue
                self._rows[row.id] = PoolRow(
                    id=row.id,
                    state="claimed",
                    provider_connection_id=row.provider_connection_id,
                    credentials=row.credentials,
                    webhook_token_hash=row.webhook_token_hash,
                    claimed_by_user_id=user_id,
                )
                return PooledInstance(
                    pool_row_id=row.id,
                    ref=ConnectionRef(
                        provider="green",
                        provider_connection_id=row.provider_connection_id,
                    ),
                    credentials=row.credentials,
                    webhook_token_hash=row.webhook_token_hash or b"",
                )
            return None

    async def finalize_claim(self, row_id: str) -> None:
        self._rows.pop(row_id, None)
        self._credentials.pop(row_id, None)

    async def release_claim(self, row_id: str) -> None:
        existing = self._rows.get(row_id)
        if existing is None or existing.state != "claimed":
            return
        self._rows[row_id] = PoolRow(
            id=row_id,
            state="available",
            provider_connection_id=existing.provider_connection_id,
            credentials=existing.credentials,
            webhook_token_hash=existing.webhook_token_hash,
            claimed_by_user_id=None,
        )

    async def delete_row(self, row_id: str) -> None:
        self._rows.pop(row_id, None)
        self._credentials.pop(row_id, None)

    async def get_by_state(self, state: str) -> list[PoolRow]:
        return [r for r in self._rows.values() if r.state == state]

    async def get_creating_with_credentials(self) -> list[PoolRow]:
        return [
            r for r in self._rows.values()
            if r.state == "creating" and r.provider_connection_id is not None
        ]

    async def get_creating_without_credentials(self) -> list[PoolRow]:
        return [
            r for r in self._rows.values()
            if r.state == "creating" and r.provider_connection_id is None
        ]

    async def get_failed_with_provider_id(self) -> list[PoolRow]:
        return [
            r for r in self._rows.values()
            if r.state == "failed" and r.provider_connection_id is not None
        ]


# --- Postgres implementation -----------------------------------------------


# Advisory lock key for capacity reservation. A stable hash of the
# pool name keeps concurrent reserve_creation_slot calls serialized
# without blocking other pool operations.
_POOL_RESERVE_LOCK_KEY = -902850313  # hashtext('green_pool_reserve') in PG


class PostgresGreenInstancePoolRepository(GreenInstancePoolRepository):
    """PostgreSQL implementation of :class:`GreenInstancePoolRepository`.

    Session handling mirrors :class:`PostgresWhatsAppConnectionRepository`:
    standalone mode (each method opens its own short session and commits)
    by default; UoW mode via a shared session is not needed here.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        cipher: CredentialCipher | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher or IdentityCredentialCipher()

    # --- protocol methods -------------------------------------------------

    async def reserve_creation_slot(self, target_size: int) -> str | None:
        """Atomically count + insert a ``creating`` row under an advisory lock.

        Uses ``pg_advisory_xact_lock`` so concurrent callers serialize on
        the reservation decision without blocking other pool operations.
        """
        async with self._session_factory() as session:
            # Serialize reservation decisions.
            await session.execute(
                select(func.pg_advisory_xact_lock(_POOL_RESERVE_LOCK_KEY))
            )
            active = (
                await session.execute(
                    select(func.count())
                    .select_from(GreenInstancePoolRow)
                    .where(GreenInstancePoolRow.state.in_(("creating", "available", "failed")))
                )
            ).scalar_one()
            if active >= target_size:
                return None
            row_id = str(uuid.uuid4())
            await session.execute(
                pg_insert(GreenInstancePoolRow)
                .values(id=row_id, state="creating")
            )
            await session.commit()
            return row_id

    async def mark_created(
        self,
        row_id: str,
        ref: ConnectionRef,
        credentials: ProviderCredentials,
        webhook_token_hash: bytes,
    ) -> None:
        encrypted = self._cipher.encrypt(credentials.data)
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            await session.execute(
                update(GreenInstancePoolRow)
                .where(GreenInstancePoolRow.id == row_id)
                .values(
                    provider_connection_id=ref.provider_connection_id,
                    api_token=encrypted,
                    webhook_token_hash=webhook_token_hash,
                    updated_at=now,
                )
            )
            await session.commit()

    async def mark_available(self, row_id: str) -> None:
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            await session.execute(
                update(GreenInstancePoolRow)
                .where(GreenInstancePoolRow.id == row_id)
                .values(state="available", updated_at=now)
            )
            await session.commit()

    async def mark_failed(self, row_id: str) -> None:
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            await session.execute(
                update(GreenInstancePoolRow)
                .where(GreenInstancePoolRow.id == row_id)
                .values(state="failed", updated_at=now)
            )
            await session.commit()

    async def claim(self, user_id: str) -> PooledInstance | None:
        """Atomically claim one ``available`` row using FOR UPDATE SKIP LOCKED."""
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            stmt = (
                select(GreenInstancePoolRow)
                .where(GreenInstancePoolRow.state == "available")
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                await session.commit()
                return None
            await session.execute(
                update(GreenInstancePoolRow)
                .where(GreenInstancePoolRow.id == row.id)
                .values(
                    state="claimed",
                    claimed_by_user_id=user_id,
                    claimed_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
            return PooledInstance(
                pool_row_id=str(row.id),
                ref=ConnectionRef(
                    provider="green",
                    provider_connection_id=row.provider_connection_id,  # type: ignore[arg-type]
                ),
                credentials=ProviderCredentials(
                    data=self._cipher.decrypt(row.api_token),  # type: ignore[arg-type]
                ),
                webhook_token_hash=row.webhook_token_hash,  # type: ignore[arg-type]
            )

    async def finalize_claim(self, row_id: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                delete(GreenInstancePoolRow).where(GreenInstancePoolRow.id == row_id)
            )
            await session.commit()

    async def release_claim(self, row_id: str) -> None:
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            await session.execute(
                update(GreenInstancePoolRow)
                .where(
                    GreenInstancePoolRow.id == row_id,
                    GreenInstancePoolRow.state == "claimed",
                )
                .values(
                    state="available",
                    claimed_by_user_id=None,
                    claimed_at=None,
                    updated_at=now,
                )
            )
            await session.commit()

    async def delete_row(self, row_id: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                delete(GreenInstancePoolRow).where(GreenInstancePoolRow.id == row_id)
            )
            await session.commit()

    async def get_by_state(self, state: str) -> list[PoolRow]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(GreenInstancePoolRow).where(
                        GreenInstancePoolRow.state == state
                    )
                )
            ).scalars().all()
            return [self._row_to_domain(r) for r in rows]

    async def get_creating_with_credentials(self) -> list[PoolRow]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(GreenInstancePoolRow).where(
                        GreenInstancePoolRow.state == "creating",
                        GreenInstancePoolRow.provider_connection_id.is_not(None),
                    )
                )
            ).scalars().all()
            return [self._row_to_domain(r) for r in rows]

    async def get_creating_without_credentials(self) -> list[PoolRow]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(GreenInstancePoolRow).where(
                        GreenInstancePoolRow.state == "creating",
                        GreenInstancePoolRow.provider_connection_id.is_(None),
                    )
                )
            ).scalars().all()
            return [self._row_to_domain(r) for r in rows]

    async def get_failed_with_provider_id(self) -> list[PoolRow]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(GreenInstancePoolRow).where(
                        GreenInstancePoolRow.state == "failed",
                        GreenInstancePoolRow.provider_connection_id.is_not(None),
                    )
                )
            ).scalars().all()
            return [self._row_to_domain(r) for r in rows]

    # --- mapping ----------------------------------------------------------

    def _row_to_domain(self, row: GreenInstancePoolRow) -> PoolRow:
        credentials: ProviderCredentials | None = None
        if row.api_token is not None:
            credentials = ProviderCredentials(data=self._cipher.decrypt(row.api_token))
        return PoolRow(
            id=str(row.id),
            state=row.state,
            provider_connection_id=row.provider_connection_id,
            credentials=credentials,
            webhook_token_hash=row.webhook_token_hash,
            claimed_by_user_id=(
                str(row.claimed_by_user_id) if row.claimed_by_user_id is not None else None
            ),
        )
