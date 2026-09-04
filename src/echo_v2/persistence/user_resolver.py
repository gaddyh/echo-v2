"""User resolution from phone number to user_id + timezone.

The bot webhook receives events keyed by the sender's phone number. The
scheduling flow needs a ``user_id`` (for the ScheduledAction) and a
``timezone`` (for time parsing). This module resolves the phone number
against the ``users`` table.

Two implementations:
* :class:`InMemoryUserResolver` — for tests.
* :class:`PostgresUserResolver` — for production, queries the users table.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from echo_v2.persistence.identity import PhoneParseError, normalize_phone_e164
from echo_v2.persistence.orm import UserRow

__all__ = ["InMemoryUserResolver", "PostgresUserResolver", "UserResolver"]


@runtime_checkable
class UserResolver(Protocol):
    """Resolve a phone number to ``(user_id, timezone)``."""

    async def resolve(self, phone: str) -> tuple[str, str] | None: ...


class InMemoryUserResolver:
    """In-memory user resolver for tests."""

    def __init__(self) -> None:
        self._users: dict[str, tuple[str, str]] = {}

    def add_user(self, phone: str, user_id: str, timezone: str = "Asia/Jerusalem") -> None:
        """Register a user for testing."""
        self._users[normalize_phone_e164(phone)] = (user_id, timezone)

    async def resolve(self, phone: str) -> tuple[str, str] | None:
        try:
            normalized = normalize_phone_e164(phone)
        except PhoneParseError:
            return None
        return self._users.get(normalized)


class PostgresUserResolver:
    """Production user resolver against the users table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve(self, phone: str) -> tuple[str, str] | None:
        try:
            normalized = normalize_phone_e164(phone)
        except PhoneParseError:
            return None

        stmt = select(UserRow.id, UserRow.timezone).where(
            UserRow.phone_number == normalized
        )
        result = await self._session.execute(stmt)
        row = result.first()
        if row is None:
            return None
        return str(row.id), row.timezone or "UTC"
