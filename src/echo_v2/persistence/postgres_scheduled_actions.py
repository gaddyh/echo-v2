"""Postgres-backed scheduled action repository.

Satisfies :class:`echo_v2.persistence.scheduled_actions.ScheduledActionRepository`
against PostgreSQL via SQLAlchemy 2 async. Uses ``FOR UPDATE SKIP LOCKED``
for safe concurrent claiming — multiple scheduler workers can call
``claim_due`` simultaneously without conflict; each gets a distinct row
or ``None``.

Session handling mirrors :class:`PostgresWhatsAppConnectionRepository`:
``session=None`` (default) → standalone mode; ``session=<shared>`` → UoW
mode where the enclosing unit of work owns the transaction boundary.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from echo_v2.domain.scheduling import (
    ScheduledAction,
    ScheduledActionStatus,
    ScheduledActionType,
)
from echo_v2.persistence.orm import ScheduledActionRow
from echo_v2.persistence.scheduled_actions import ScheduledActionRepository

__all__ = ["PostgresScheduledActionRepository"]


class PostgresScheduledActionRepository(ScheduledActionRepository):
    """PostgreSQL implementation of :class:`ScheduledActionRepository`."""

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

    async def save(self, action: ScheduledAction) -> None:
        async with self._session() as session:
            stmt = (
                pg_insert(ScheduledActionRow)
                .values(
                    id=action.id,
                    user_id=action.user_id,
                    type=action.type.value,
                    execute_at_utc=action.execute_at_utc,
                    timezone=action.timezone,
                    status=action.status.value,
                    payload=action.payload,
                    created_at=action.created_at,
                    updated_at=datetime.now(timezone.utc),
                    executed_at=action.executed_at,
                    result=action.result,
                    error=action.error,
                    claimed_at=action.claimed_at,
                )
                .on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "type": action.type.value,
                        "execute_at_utc": action.execute_at_utc,
                        "timezone": action.timezone,
                        "status": action.status.value,
                        "payload": action.payload,
                        "updated_at": datetime.now(timezone.utc),
                        "executed_at": action.executed_at,
                        "result": action.result,
                        "error": action.error,
                        "claimed_at": action.claimed_at,
                    },
                )
            )
            await session.execute(stmt)

    async def get(self, action_id: str) -> ScheduledAction | None:
        async with self._session() as session:
            stmt = select(ScheduledActionRow).where(
                ScheduledActionRow.id == action_id
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            return self._row_to_domain(row) if row else None

    async def list_pending(self, user_id: str) -> list[ScheduledAction]:
        async with self._session() as session:
            stmt = (
                select(ScheduledActionRow)
                .where(
                    ScheduledActionRow.user_id == user_id,
                    ScheduledActionRow.status == ScheduledActionStatus.PENDING.value,
                )
                .order_by(ScheduledActionRow.execute_at_utc)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [self._row_to_domain(r) for r in rows]

    async def claim_due(
        self,
        now: datetime,
        lease_seconds: float = 300.0,
    ) -> ScheduledAction | None:
        """Atomically claim one due PENDING action.

        Uses ``FOR UPDATE SKIP LOCKED`` so concurrent workers never block
        each other — each skips rows already locked by another worker.
        """
        async with self._session() as session:
            # Select the oldest due PENDING row, skipping locked rows.
            stmt = (
                select(ScheduledActionRow)
                .where(
                    ScheduledActionRow.status == ScheduledActionStatus.PENDING.value,
                    ScheduledActionRow.execute_at_utc <= now,
                )
                .order_by(ScheduledActionRow.execute_at_utc)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None

            # Transition to IN_PROGRESS within the same transaction.
            now_utc = datetime.now(timezone.utc)
            stmt_update = (
                update(ScheduledActionRow)
                .where(ScheduledActionRow.id == row.id)
                .values(
                    status=ScheduledActionStatus.IN_PROGRESS.value,
                    claimed_at=now_utc,
                    updated_at=now_utc,
                )
            )
            await session.execute(stmt_update)
            # Return a domain object reflecting the new state.
            return self._row_to_domain(row, override_status=ScheduledActionStatus.IN_PROGRESS, override_claimed_at=now_utc)

    async def mark_succeeded(
        self,
        action_id: str,
        result: dict[str, Any],
    ) -> None:
        await self._mark_terminal(
            action_id,
            status=ScheduledActionStatus.SUCCEEDED,
            result=result,
        )

    async def mark_failed(self, action_id: str, error: str) -> None:
        await self._mark_terminal(
            action_id,
            status=ScheduledActionStatus.FAILED,
            error=error,
        )

    async def mark_indeterminate(self, action_id: str, error: str) -> None:
        await self._mark_terminal(
            action_id,
            status=ScheduledActionStatus.INDETERMINATE,
            error=error,
        )

    async def cancel(self, action_id: str, user_id: str) -> bool:
        async with self._session() as session:
            # Conditional update: only cancels if PENDING + owned by user.
            stmt = (
                update(ScheduledActionRow)
                .where(
                    ScheduledActionRow.id == action_id,
                    ScheduledActionRow.user_id == user_id,
                    ScheduledActionRow.status == ScheduledActionStatus.PENDING.value,
                )
                .values(
                    status=ScheduledActionStatus.CANCELLED.value,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            result = await session.execute(stmt)
            return result.rowcount > 0

    async def recover_stale(
        self,
        now: datetime,
        lease_seconds: float = 300.0,
    ) -> int:
        threshold = now - timedelta(seconds=lease_seconds)
        async with self._session() as session:
            stmt = (
                update(ScheduledActionRow)
                .where(
                    ScheduledActionRow.status == ScheduledActionStatus.IN_PROGRESS.value,
                    ScheduledActionRow.claimed_at < threshold,
                )
                .values(
                    status=ScheduledActionStatus.PENDING.value,
                    claimed_at=None,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            result = await session.execute(stmt)
            return result.rowcount

    async def _mark_terminal(
        self,
        action_id: str,
        *,
        status: ScheduledActionStatus,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        async with self._session() as session:
            stmt = (
                update(ScheduledActionRow)
                .where(ScheduledActionRow.id == action_id)
                .values(
                    status=status.value,
                    result=result,
                    error=error,
                    executed_at=now,
                    updated_at=now,
                )
            )
            await session.execute(stmt)

    def _row_to_domain(
        self,
        row: ScheduledActionRow,
        *,
        override_status: ScheduledActionStatus | None = None,
        override_claimed_at: datetime | None = None,
    ) -> ScheduledAction:
        return ScheduledAction(
            id=str(row.id),
            user_id=str(row.user_id),
            type=ScheduledActionType(row.type),
            execute_at_utc=row.execute_at_utc,
            timezone=row.timezone,
            status=override_status or ScheduledActionStatus(row.status),
            payload=dict(row.payload) if row.payload else {},
            created_at=row.created_at,
            executed_at=row.executed_at,
            result=dict(row.result) if row.result else None,
            error=row.error,
            claimed_at=override_claimed_at or row.claimed_at,
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

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            if self._owns:
                if exc_type is None:
                    await self._session.commit()
                else:
                    await self._session.rollback()
        finally:
            if self._owns:
                await self._session.close()
