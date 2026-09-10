"""Postgres-backed user repository for onboarding.

Extends the existing :class:`PostgresUserResolver` with write operations
needed by the onboarding flow: create user, update onboarding status,
update first name.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.persistence.identity import normalize_phone_e164
from echo_v2.persistence.orm import UserRow
from echo_v2.persistence.user_resolver import PostgresUserResolver

__all__ = ["PostgresUserRepository"]


class PostgresUserRepository:
    """PostgreSQL implementation of the onboarding UserRepository protocol.

    Combines read (resolve) and write (create, update) operations.
    Uses the shared session factory for standalone calls.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def create_user(
        self,
        phone: str,
        *,
        timezone: str = "Asia/Jerusalem",
        first_name: str | None = None,
        onboarding_status: str = "pending",
    ) -> str:
        """Create a new user. Returns the user_id.

        Raises ``ValueError`` if the phone number already exists (unique
        constraint violation surfaces as IntegrityError, but we check
        first for a cleaner error).
        """
        normalized = normalize_phone_e164(phone)

        async with self._session_factory() as session:
            # Check existing first.
            existing = await session.execute(
                select(UserRow.id).where(UserRow.phone_number == normalized)
            )
            if existing.scalar_one_or_none() is not None:
                raise ValueError(f"User with phone {normalized} already exists")

            stmt = (
                pg_insert(UserRow)
                .values(
                    phone_number=normalized,
                    timezone=timezone,
                    first_name=first_name,
                    onboarding_status=onboarding_status,
                )
                .returning(UserRow.id)
            )
            result = await session.execute(stmt)
            user_id = result.scalar_one()
            await session.commit()
            return str(user_id)

    async def get_by_phone(
        self, phone: str
    ) -> tuple[str, str | None, str | None] | None:
        """Return (user_id, onboarding_status, first_name) or None."""
        try:
            normalized = normalize_phone_e164(phone)
        except (ValueError, TypeError):
            return None

        async with self._session_factory() as session:
            stmt = select(
                UserRow.id,
                UserRow.onboarding_status,
                UserRow.first_name,
            ).where(UserRow.phone_number == normalized)
            result = await session.execute(stmt)
            row = result.first()
            if row is None:
                return None
            return str(row.id), row.onboarding_status, row.first_name

    async def update_onboarding_status(
        self,
        user_id: str,
        status: str,
    ) -> None:
        """Update the user's onboarding_status."""
        async with self._session_factory() as session:
            stmt = (
                update(UserRow)
                .where(UserRow.id == user_id)
                .values(
                    onboarding_status=status,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await session.execute(stmt)
            await session.commit()

    async def update_first_name(
        self,
        user_id: str,
        first_name: str,
    ) -> None:
        """Update the user's first_name and set onboarding_status='active'."""
        async with self._session_factory() as session:
            stmt = (
                update(UserRow)
                .where(UserRow.id == user_id)
                .values(
                    first_name=first_name,
                    onboarding_status="active",
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await session.execute(stmt)
            await session.commit()

    async def resolve(self, phone: str) -> tuple[str, str] | None:
        """Resolve a phone number to (user_id, timezone).

        Delegates to :class:`PostgresUserResolver` for compatibility with
        the existing :class:`UserResolver` protocol.
        """
        async with self._session_factory() as session:
            return await PostgresUserResolver(session).resolve(phone)

    async def get_phone_by_id(self, user_id: str) -> str | None:
        """Look up a user's phone number by user_id."""
        async with self._session_factory() as session:
            stmt = select(UserRow.phone_number).where(UserRow.id == user_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return row if row else None
