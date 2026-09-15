"""Waitlist signup repository — landing-page name + phone capture.

Deduplicated by canonical E.164 phone number: a repeat submission with the
same phone is a silent no-op (``add`` returns ``False``), so the landing
page can always show "you're on the list" without leaking whether the
number was already registered.

An optional ``willingness_to_pay`` signal (``free`` / ``under_30`` /
``30_70`` / ``70_120`` / ``120_plus``) is captured at signup for demand validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.persistence.orm import WaitlistSignupRow

__all__ = [
    "InMemoryWaitlistRepository",
    "PostgresWaitlistRepository",
    "WaitlistRepository",
    "WaitlistSignup",
]


@dataclass(frozen=True)
class WaitlistSignup:
    """A single waitlist signup."""

    id: str
    name: str
    phone_number: str
    created_at: datetime
    willingness_to_pay: str | None = None


@runtime_checkable
class WaitlistRepository(Protocol):
    """Store landing-page waitlist signups, deduplicated by phone."""

    async def add(
        self,
        *,
        name: str,
        phone_number: str,
        willingness_to_pay: str | None = None,
    ) -> bool:
        """Insert a signup. Returns ``True`` if inserted, ``False`` if the
        phone number is already on the list (no-op)."""
        ...

    async def list_all(self) -> list[WaitlistSignup]:
        """Return all signups, oldest first."""
        ...

    async def count(self) -> int:
        """Return the total number of signups."""
        ...


class InMemoryWaitlistRepository:
    """Process-local waitlist repository backed by a dict."""

    def __init__(self) -> None:
        self._rows: dict[str, WaitlistSignup] = {}

    async def add(
        self,
        *,
        name: str,
        phone_number: str,
        willingness_to_pay: str | None = None,
    ) -> bool:
        import uuid

        if phone_number in self._rows:
            return False
        self._rows[phone_number] = WaitlistSignup(
            id=str(uuid.uuid4()),
            name=name,
            phone_number=phone_number,
            created_at=datetime.now(timezone.utc),
            willingness_to_pay=willingness_to_pay,
        )
        return True

    async def list_all(self) -> list[WaitlistSignup]:
        return sorted(self._rows.values(), key=lambda s: s.created_at)

    async def count(self) -> int:
        return len(self._rows)


class PostgresWaitlistRepository:
    """PostgreSQL implementation of :class:`WaitlistRepository`.

    ``add`` uses ``INSERT ... ON CONFLICT (phone_number) DO NOTHING`` so
    duplicate submissions never raise and never overwrite the original
    signup timestamp.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(
        self,
        *,
        name: str,
        phone_number: str,
        willingness_to_pay: str | None = None,
    ) -> bool:
        async with self._session_factory() as session:
            stmt = (
                pg_insert(WaitlistSignupRow)
                .values(
                    name=name,
                    phone_number=phone_number,
                    willingness_to_pay=willingness_to_pay,
                )
                .on_conflict_do_nothing(constraint="uq_waitlist_phone")
                .returning(WaitlistSignupRow.id)
            )
            row_id = (await session.execute(stmt)).scalar_one_or_none()
            await session.commit()
            return row_id is not None

    async def list_all(self) -> list[WaitlistSignup]:
        async with self._session_factory() as session:
            stmt = select(WaitlistSignupRow).order_by(WaitlistSignupRow.created_at)
            rows = (await session.execute(stmt)).scalars().all()
            return [
                WaitlistSignup(
                    id=str(r.id),
                    name=r.name,
                    phone_number=r.phone_number,
                    created_at=r.created_at,
                    willingness_to_pay=r.willingness_to_pay,
                )
                for r in rows
            ]

    async def count(self) -> int:
        async with self._session_factory() as session:
            stmt = select(func.count()).select_from(WaitlistSignupRow)
            return (await session.execute(stmt)).scalar_one()
