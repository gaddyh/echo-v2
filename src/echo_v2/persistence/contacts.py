"""Contact repository — saves and looks up user contacts from vCards.

Supports two flows:
* ``save`` — upsert on ``(user_id, phone_number)``, updating display_name.
* ``find_by_name`` — case-insensitive lookup by display_name for a user.
* ``find_by_phone`` — lookup by phone number for a user.

In-memory implementation for tests; Postgres implementation for production.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.persistence.orm import ContactRow

__all__ = [
    "ContactRecord",
    "ContactRepository",
    "InMemoryContactRepository",
    "PostgresContactRepository",
]


@dataclass(frozen=True)
class ContactRecord:
    """A saved contact."""

    user_id: str
    display_name: str
    phone_number: str


class ContactRepository:
    """Protocol-style base class for contact repositories."""

    async def save(self, contact: ContactRecord) -> None:
        """Upsert a contact (update name if phone already exists)."""
        raise NotImplementedError

    async def find_by_name(self, user_id: str, name: str) -> ContactRecord | None:
        """Case-insensitive lookup by display_name for a user."""
        raise NotImplementedError

    async def find_by_phone(self, user_id: str, phone: str) -> ContactRecord | None:
        """Lookup by phone number for a user."""
        raise NotImplementedError


class InMemoryContactRepository(ContactRepository):
    """In-memory contact store for tests."""

    def __init__(self) -> None:
        self._contacts: dict[tuple[str, str], ContactRecord] = {}

    async def save(self, contact: ContactRecord) -> None:
        self._contacts[(contact.user_id, contact.phone_number)] = contact

    async def find_by_name(self, user_id: str, name: str) -> ContactRecord | None:
        name_lower = name.lower().strip()
        for (uid, _phone), contact in self._contacts.items():
            if uid == user_id and contact.display_name.lower().strip().startswith(name_lower):
                return contact
        return None

    async def find_by_phone(self, user_id: str, phone: str) -> ContactRecord | None:
        return self._contacts.get((user_id, phone))


class PostgresContactRepository(ContactRepository):
    """PostgreSQL implementation of :class:`ContactRepository`."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save(self, contact: ContactRecord) -> None:
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            stmt = (
                pg_insert(ContactRow)
                .values(
                    user_id=contact.user_id,
                    display_name=contact.display_name,
                    phone_number=contact.phone_number,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    constraint="contacts_user_phone_key",
                    set_={
                        "display_name": contact.display_name,
                        "updated_at": now,
                    },
                )
            )
            await session.execute(stmt)
            await session.commit()

    async def find_by_name(self, user_id: str, name: str) -> ContactRecord | None:
        async with self._session_factory() as session:
            # Partial match: user types "זיפוש", matches "זיפוש המהממת".
            # ILIKE with wildcard: prefix match, case-insensitive.
            pattern = f"{name.strip()}%"
            stmt = select(ContactRow).where(
                ContactRow.user_id == user_id,
                ContactRow.display_name.ilike(pattern),
            ).limit(1)
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            return ContactRecord(
                user_id=str(row.user_id),
                display_name=row.display_name,
                phone_number=row.phone_number,
            )

    async def find_by_phone(self, user_id: str, phone: str) -> ContactRecord | None:
        async with self._session_factory() as session:
            stmt = select(ContactRow).where(
                ContactRow.user_id == user_id,
                ContactRow.phone_number == phone,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            return ContactRecord(
                user_id=str(row.user_id),
                display_name=row.display_name,
                phone_number=row.phone_number,
            )
