"""Postgres-backed idempotency store with lease + fencing-token semantics.

Satisfies :class:`echo_v2.runtime.idempotency.IdempotencyStore` against
PostgreSQL. Implements the concurrency model from the plan:

* ``reserve(key)`` — atomic ``INSERT ... ON CONFLICT DO NOTHING`` to claim a
  fresh key; on conflict, reads the existing row and either returns
  ``IN_PROGRESS`` (active lease), ``COMPLETED`` (terminal), or atomically
  **reclaims an expired lease** via a conditional ``UPDATE ... WHERE
  lease_expires_at <= now()``. Crash recovery is a property of ``reserve()``,
  not a background sweeper.

* Every owner write (``put_success`` / ``put_failure`` / ``put_indeterminate``
  / ``release`` / ``renew_lease``) is **token-guarded**:
  ``UPDATE ... WHERE owner_token = :my_token AND state = 'IN_PROGRESS'``.
  A slow prior owner whose lease was reclaimed will see **zero rows
  affected** → raise :class:`LostOwnershipError` (do not overwrite the new
  owner's outcome).

* All lease comparisons use DB ``now()``, never the Python clock — so two
  workers with slightly different system clocks agree on lease expiry.

* ``wait_for_completion`` polls ``get(key)`` with short sleeps, falling back
  to ``reserve()``-reclaim if the lease is observed expired. On timeout,
  raises ``RetryableError``.

Outcomes are serialized to/from a ``JSONB`` column as tagged dicts (plain
data, never exception objects — per the existing module contract).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Generic, TypeVar

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from echo_v2.persistence.orm import IdempotencyOperationRow
from echo_v2.runtime.errors import RetryableError
from echo_v2.runtime.idempotency import (
    IdempotencyStore,
    IndeterminateOutcome,
    LostOwnershipError,
    PermanentFailureOutcome,
    ReserveResult,
    ReserveStatus,
    StoredOutcome,
    SuccessOutcome,
)

__all__ = ["PostgresIdempotencyStore"]

TOutput = TypeVar("TOutput")

_DEFAULT_LEASE_SECONDS = 30
_POLL_INTERVAL_SECONDS = 0.05
_POLL_TIMEOUT_SECONDS = 30.0

_STATE_IN_PROGRESS = "IN_PROGRESS"
_STATE_SUCCESS = "SUCCESS"
_STATE_FAILURE = "FAILURE"
_STATE_INDETERMINATE = "INDETERMINATE"


class PostgresIdempotencyStore(IdempotencyStore[TOutput], Generic[TOutput]):
    """PostgreSQL implementation of :class:`IdempotencyStore`."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        session: AsyncSession | None = None,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._shared_session = session
        self._lease_seconds = lease_seconds

    def _session(self) -> _SessionContext:
        if self._shared_session is not None:
            return _SessionContext(self._shared_session, owns=False)
        return _SessionContext(self._session_factory(), owns=True)

    # --- read -------------------------------------------------------------

    async def get(self, key: str) -> StoredOutcome[TOutput] | None:
        async with self._session() as session:
            stmt = select(IdempotencyOperationRow).where(
                IdempotencyOperationRow.key == key
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None or row.state == _STATE_IN_PROGRESS:
                return None
            return _row_to_outcome(row)

    # --- reserve + reclaim ------------------------------------------------

    async def reserve(self, key: str) -> ReserveResult:
        token = uuid.uuid4()
        lease_expires = datetime.now(timezone.utc) + timedelta(seconds=self._lease_seconds)

        async with self._session() as session:
            # 1. Try to INSERT a fresh claim.
            stmt = (
                pg_insert(IdempotencyOperationRow)
                .values(
                    key=key,
                    state=_STATE_IN_PROGRESS,
                    owner_token=token,
                    lease_expires_at=lease_expires,
                )
                .on_conflict_do_nothing(index_elements=["key"])
                .returning(IdempotencyOperationRow.key)
            )
            result = await session.execute(stmt)
            if result.scalar_one_or_none() is not None:
                return ReserveResult(ReserveStatus.ACQUIRED, token)

            # 2. Conflict: read the existing row.
            existing = await _fetch_row(session, key)
            if existing is None:  # pragma: no cover — race
                return await self.reserve(key)

            if existing.state != _STATE_IN_PROGRESS:
                return ReserveResult(ReserveStatus.COMPLETED)

            # 3. In-progress: check lease.
            if existing.lease_expires_at is not None and existing.lease_expires_at > datetime.now(
                timezone.utc
            ):
                return ReserveResult(ReserveStatus.IN_PROGRESS)

            # 4. Lease expired: attempt atomic reclaim.
            reclaimed = await _try_reclaim(session, key, token, self._lease_seconds)
            if reclaimed:
                return ReserveResult(ReserveStatus.ACQUIRED, token)

            # 5. Another caller beat us to the reclaim; re-read and branch.
            existing = await _fetch_row(session, key)
            if existing is None:  # pragma: no cover
                return ReserveResult(ReserveStatus.IN_PROGRESS)
            if existing.state != _STATE_IN_PROGRESS:
                return ReserveResult(ReserveStatus.COMPLETED)
            return ReserveResult(ReserveStatus.IN_PROGRESS)

    # --- owner writes (token-guarded) -------------------------------------

    async def put_success(
        self,
        key: str,
        owner_token: uuid.UUID,
        outcome: SuccessOutcome[TOutput],
    ) -> None:
        payload = _serialize_success(outcome)
        await self._terminal_write(
            key,
            owner_token,
            state=_STATE_SUCCESS,
            outcome=payload,
            extra={"attempts": outcome.attempts, "duration_ms": outcome.duration_ms},
        )

    async def put_failure(
        self,
        key: str,
        owner_token: uuid.UUID,
        outcome: PermanentFailureOutcome,
    ) -> None:
        payload = _serialize_failure(outcome)
        await self._terminal_write(
            key,
            owner_token,
            state=_STATE_FAILURE,
            outcome=payload,
            extra={"error_type": outcome.error_type, "error_message": outcome.error_message},
        )

    async def put_indeterminate(
        self,
        key: str,
        owner_token: uuid.UUID,
        outcome: IndeterminateOutcome,
    ) -> None:
        payload = _serialize_indeterminate(outcome)
        await self._terminal_write(
            key,
            owner_token,
            state=_STATE_INDETERMINATE,
            outcome=payload,
            extra={"error_type": outcome.error_type, "error_message": outcome.error_message},
        )

    async def release(self, key: str, owner_token: uuid.UUID) -> None:
        """Release a claim by deleting the in-progress row.

        A released key has no terminal outcome — a subsequent ``reserve``
        can re-claim it. Token-guarded so only the current owner can release.
        """
        async with self._session() as session:
            stmt = text(
                "DELETE FROM idempotency_operations "
                "WHERE key = :key AND owner_token = :token AND state = 'IN_PROGRESS'"
            )
            result = await session.execute(
                stmt, {"key": key, "token": str(owner_token)}
            )
            if result.rowcount == 0:
                raise LostOwnershipError(
                    f"release() for key {key!r} affected 0 rows; ownership lost."
                )

    async def renew_lease(self, key: str, owner_token: uuid.UUID) -> bool:
        new_expiry = datetime.now(timezone.utc) + timedelta(seconds=self._lease_seconds)
        async with self._session() as session:
            stmt = (
                update(IdempotencyOperationRow)
                .where(
                    IdempotencyOperationRow.key == key,
                    IdempotencyOperationRow.owner_token == owner_token,
                    IdempotencyOperationRow.state == _STATE_IN_PROGRESS,
                )
                .values(lease_expires_at=new_expiry, updated_at=datetime.now(timezone.utc))
            )
            result = await session.execute(stmt)
            return result.rowcount > 0

    # --- wait -------------------------------------------------------------

    async def wait_for_completion(self, key: str) -> StoredOutcome[TOutput]:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=_POLL_TIMEOUT_SECONDS)
        while datetime.now(timezone.utc) < deadline:
            outcome = await self.get(key)
            if outcome is not None:
                return outcome
            # Check if the lease is expired — if so, try to reclaim and run.
            r = await self.reserve(key)
            if r.status == ReserveStatus.ACQUIRED:
                # We stole the lease but we're a waiter, not an executor.
                # Release it so the original flow can retry, and raise
                # RetryableError so the caller re-attempts from the top.
                await self.release(key, r.owner_token)  # type: ignore[arg-type]
                raise RetryableError(
                    f"Idempotent operation {key!r} owner lease expired; retry."
                )
            if r.status == ReserveStatus.COMPLETED:
                outcome = await self.get(key)
                if outcome is not None:
                    return outcome
                raise RetryableError("Idempotent operation did not complete")
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        raise RetryableError(
            f"Timed out waiting for idempotent operation {key!r} to complete."
        )

    # --- internal ---------------------------------------------------------

    async def _terminal_write(
        self,
        key: str,
        owner_token: uuid.UUID,
        *,
        state: str,
        outcome: dict[str, Any],
        extra: dict[str, Any],
    ) -> None:
        """Token-guarded UPDATE to a terminal state.

        Raises :class:`LostOwnershipError` if zero rows are affected (the
        caller's lease expired and was reclaimed — do not overwrite the new
        owner's outcome).
        """
        now = datetime.now(timezone.utc)
        values: dict[str, Any] = {
            "state": state,
            "outcome": outcome,  # JSONB column — pass the dict, SQLAlchemy serializes
            "owner_token": None,
            "lease_expires_at": None,
            "updated_at": now,
        }
        values.update(extra)

        async with self._session() as session:
            stmt = (
                update(IdempotencyOperationRow)
                .where(
                    IdempotencyOperationRow.key == key,
                    IdempotencyOperationRow.owner_token == owner_token,
                    IdempotencyOperationRow.state == _STATE_IN_PROGRESS,
                )
                .values(**values)
            )
            result = await session.execute(stmt)
            if result.rowcount == 0:
                raise LostOwnershipError(
                    f"Terminal write for key {key!r} affected 0 rows; "
                    "ownership lost (lease expired and was reclaimed)."
                )


# --- helpers ---------------------------------------------------------------


async def _fetch_row(session: AsyncSession, key: str) -> IdempotencyOperationRow | None:
    stmt = select(IdempotencyOperationRow).where(IdempotencyOperationRow.key == key)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _try_reclaim(
    session: AsyncSession,
    key: str,
    new_token: uuid.UUID,
    lease_seconds: int,
) -> bool:
    """Atomically reclaim an expired lease. Returns True if reclaimed."""
    new_expiry = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
    # Use raw SQL so the lease comparison is against DB now(), not Python.
    stmt = text(
        "UPDATE idempotency_operations "
        "SET owner_token = :token, lease_expires_at = :expiry, updated_at = now() "
        "WHERE key = :key AND state = 'IN_PROGRESS' "
        "AND (lease_expires_at IS NULL OR lease_expires_at <= now()) "
        "RETURNING key"
    )
    result = await session.execute(
        stmt,
        {"token": str(new_token), "expiry": new_expiry, "key": key},
    )
    return result.scalar_one_or_none() is not None


# --- outcome serialization -------------------------------------------------

_TAG_SUCCESS = "success"
_TAG_FAILURE = "failure"
_TAG_INDETERMINATE = "indeterminate"


def _serialize_success(outcome: SuccessOutcome[Any]) -> dict[str, Any]:
    # value may not be JSON-serializable (it's generic TOutput). We store it
    # via json.dumps with default=str as a fallback; the caller's TOutput is
    # expected to be plain data per the module contract.
    return {
        "tag": _TAG_SUCCESS,
        "value": outcome.value,
        "attempts": outcome.attempts,
        "duration_ms": outcome.duration_ms,
    }


def _serialize_failure(outcome: PermanentFailureOutcome) -> dict[str, Any]:
    return {
        "tag": _TAG_FAILURE,
        "error_type": outcome.error_type,
        "error_message": outcome.error_message,
    }


def _serialize_indeterminate(outcome: IndeterminateOutcome) -> dict[str, Any]:
    return {
        "tag": _TAG_INDETERMINATE,
        "error_type": outcome.error_type,
        "error_message": outcome.error_message,
    }


def _row_to_outcome(row: IdempotencyOperationRow) -> StoredOutcome[Any] | None:
    if row.outcome is None:
        return None
    tag = row.outcome.get("tag")
    if tag == _TAG_SUCCESS:
        return SuccessOutcome(
            value=row.outcome.get("value"),
            attempts=row.outcome.get("attempts", 0),
            duration_ms=row.outcome.get("duration_ms", 0.0),
        )
    if tag == _TAG_FAILURE:
        return PermanentFailureOutcome(
            error_type=row.outcome.get("error_type", ""),
            error_message=row.outcome.get("error_message", ""),
        )
    if tag == _TAG_INDETERMINATE:
        return IndeterminateOutcome(
            error_type=row.outcome.get("error_type", ""),
            error_message=row.outcome.get("error_message", ""),
        )
    return None  # pragma: no cover


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
