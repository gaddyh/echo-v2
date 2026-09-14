"""Contact repository — saves and looks up user contacts from vCards.

Supports two flows:
* ``save`` — upsert on ``(user_id, phone_number)``, updating display_name.
  Preserves ``is_starred`` on update (does not reset it).
* ``find_by_name`` — case-insensitive lookup by display_name for a user.
* ``find_by_phone`` — lookup by phone number for a user.
* ``set_starred`` — upsert a contact, setting only ``is_starred`` (and
  ``updated_at``). If the contact does not exist, creates it with the
  given ``display_name``.
* ``list_starred_phones`` — returns the set of starred phone numbers
  for a user (single query for sorting the waiting list).

In-memory implementation for tests; Postgres implementation for production.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.persistence.orm import ContactRow

__all__ = [
    "ContactRecord",
    "ContactRepository",
    "InMemoryContactRepository",
    "PostgresContactRepository",
]

# Valid color labels for the CHECK constraint.
VALID_COLOR_LABELS = frozenset({"red", "yellow", "green", "blue", "purple"})

# Tag normalization limits.
MAX_TAG_LENGTH = 40
MAX_TAGS_PER_CONTACT = 10


@dataclass(frozen=True)
class ContactRecord:
    """A saved contact.

    Attributes:
        user_id: The user who owns this contact.
        display_name: The contact's display name (from vCard or chat).
        phone_number: The contact's phone number (no @c.us suffix).
        is_starred: Whether the user has marked this contact as
            important. Defaults to ``False``. Starred contacts are
            sorted first in the waiting-list mini app.
        color_label: Optional color label from a fixed palette
            (``"red"``, ``"yellow"``, ``"green"``, ``"blue"``,
            ``"purple"``). ``None`` means no label.
        tags: Free-text tags for semantic grouping. Defaults to empty.
    """

    user_id: str
    display_name: str
    phone_number: str
    is_starred: bool = False
    color_label: str | None = None
    tags: list[str] = field(default_factory=list)


def normalize_tags(tags: list[str]) -> list[str]:
    """Normalize a list of tags.

    - Strip whitespace.
    - Reject empty strings.
    - Truncate each tag to :data:`MAX_TAG_LENGTH` chars.
    - Dedupe case-insensitively, preserving the first-seen casing.
    - Limit to :data:`MAX_TAGS_PER_CONTACT` tags.
    """
    seen: set[str] = set()
    result: list[str] = []
    for tag in tags:
        t = tag.strip()[:MAX_TAG_LENGTH]
        if not t:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(t)
        if len(result) >= MAX_TAGS_PER_CONTACT:
            break
    return result


class ContactRepository:
    """Protocol-style base class for contact repositories."""

    async def save(self, contact: ContactRecord) -> None:
        """Upsert a contact (update name if phone already exists).

        Preserves ``is_starred``, ``color_label``, and ``tags`` on update
        — does not reset them.
        """
        raise NotImplementedError

    async def find_by_name(self, user_id: str, name: str) -> ContactRecord | None:
        """Case-insensitive lookup by display_name for a user."""
        raise NotImplementedError

    async def find_by_phone(self, user_id: str, phone: str) -> ContactRecord | None:
        """Lookup by phone number for a user."""
        raise NotImplementedError

    async def set_starred(
        self,
        user_id: str,
        phone: str,
        *,
        is_starred: bool,
        display_name: str,
    ) -> ContactRecord:
        """Set ``is_starred`` on a contact, creating it if necessary.

        If the contact exists, only ``is_starred`` and ``updated_at`` are
        updated — ``display_name``, ``color_label``, and ``tags`` are
        preserved. If the contact does not exist, it is created with the
        given ``display_name`` and ``is_starred`` value.

        Returns the final :class:`ContactRecord`.
        """
        raise NotImplementedError

    async def set_label(
        self,
        user_id: str,
        phone: str,
        *,
        color_label: str | None,
        display_name: str,
    ) -> ContactRecord:
        """Set ``color_label`` on a contact, creating it if necessary.

        Patch-like: only touches ``color_label`` and ``updated_at``.
        Preserves ``is_starred``, ``display_name``, and ``tags``.

        Returns the final :class:`ContactRecord`.
        """
        raise NotImplementedError

    async def set_tags(
        self,
        user_id: str,
        phone: str,
        *,
        tags: list[str],
        display_name: str,
    ) -> ContactRecord:
        """Set ``tags`` on a contact, creating it if necessary.

        Patch-like: only touches ``tags`` and ``updated_at``.
        Preserves ``is_starred``, ``display_name``, and ``color_label``.

        Tags are normalized via :func:`normalize_tags` before storage.

        Returns the final :class:`ContactRecord`.
        """
        raise NotImplementedError

    async def list_starred_phones(self, user_id: str) -> set[str]:
        """Return the set of starred phone numbers for a user."""
        raise NotImplementedError

    async def list_tags(self, user_id: str) -> list[str]:
        """Return all distinct tags for a user, sorted alphabetically."""
        raise NotImplementedError

    async def get_contact_metadata(
        self, user_id: str, phones: set[str]
    ) -> dict[str, ContactRecord]:
        """Batch-fetch contact records for a set of phone numbers.

        Returns a dict mapping phone → :class:`ContactRecord`. Phones
        with no contact record are omitted from the result.
        """
        raise NotImplementedError


class InMemoryContactRepository(ContactRepository):
    """In-memory contact store for tests."""

    def __init__(self) -> None:
        self._contacts: dict[tuple[str, str], ContactRecord] = {}

    async def save(self, contact: ContactRecord) -> None:
        key = (contact.user_id, contact.phone_number)
        existing = self._contacts.get(key)
        # Preserve is_starred, color_label, tags from existing record;
        # use contact's values only for new contacts.
        if existing:
            self._contacts[key] = replace(
                contact,
                is_starred=existing.is_starred,
                color_label=existing.color_label,
                tags=existing.tags,
            )
        else:
            self._contacts[key] = contact

    async def find_by_name(self, user_id: str, name: str) -> ContactRecord | None:
        name_lower = name.lower().strip()
        for (uid, _phone), contact in self._contacts.items():
            if uid == user_id and contact.display_name.lower().strip().startswith(name_lower):
                return contact
        return None

    async def find_by_phone(self, user_id: str, phone: str) -> ContactRecord | None:
        return self._contacts.get((user_id, phone))

    async def set_starred(
        self,
        user_id: str,
        phone: str,
        *,
        is_starred: bool,
        display_name: str,
    ) -> ContactRecord:
        key = (user_id, phone)
        existing = self._contacts.get(key)
        if existing is None:
            record = ContactRecord(
                user_id=user_id,
                display_name=display_name,
                phone_number=phone,
                is_starred=is_starred,
            )
        else:
            record = replace(existing, is_starred=is_starred)
        self._contacts[key] = record
        return record

    async def set_label(
        self,
        user_id: str,
        phone: str,
        *,
        color_label: str | None,
        display_name: str,
    ) -> ContactRecord:
        key = (user_id, phone)
        existing = self._contacts.get(key)
        if existing is None:
            record = ContactRecord(
                user_id=user_id,
                display_name=display_name,
                phone_number=phone,
                color_label=color_label,
            )
        else:
            record = replace(existing, color_label=color_label)
        self._contacts[key] = record
        return record

    async def set_tags(
        self,
        user_id: str,
        phone: str,
        *,
        tags: list[str],
        display_name: str,
    ) -> ContactRecord:
        normalized = normalize_tags(tags)
        key = (user_id, phone)
        existing = self._contacts.get(key)
        if existing is None:
            record = ContactRecord(
                user_id=user_id,
                display_name=display_name,
                phone_number=phone,
                tags=normalized,
            )
        else:
            record = replace(existing, tags=normalized)
        self._contacts[key] = record
        return record

    async def list_starred_phones(self, user_id: str) -> set[str]:
        return {
            phone
            for (uid, phone), contact in self._contacts.items()
            if uid == user_id and contact.is_starred
        }

    async def list_tags(self, user_id: str) -> list[str]:
        seen: set[str] = set()
        for (uid, _phone), contact in self._contacts.items():
            if uid != user_id:
                continue
            for tag in contact.tags:
                key = tag.lower()
                if key not in seen:
                    seen.add(key)
        return sorted(seen)

    async def get_contact_metadata(
        self, user_id: str, phones: set[str]
    ) -> dict[str, ContactRecord]:
        return {
            phone: self._contacts[(user_id, phone)]
            for phone in phones
            if (user_id, phone) in self._contacts
        }


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
            return _row_to_record(row)

    async def find_by_phone(self, user_id: str, phone: str) -> ContactRecord | None:
        async with self._session_factory() as session:
            stmt = select(ContactRow).where(
                ContactRow.user_id == user_id,
                ContactRow.phone_number == phone,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            return _row_to_record(row)

    async def set_starred(
        self,
        user_id: str,
        phone: str,
        *,
        is_starred: bool,
        display_name: str,
    ) -> ContactRecord:
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            stmt = (
                pg_insert(ContactRow)
                .values(
                    user_id=user_id,
                    display_name=display_name,
                    phone_number=phone,
                    is_starred=is_starred,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    constraint="contacts_user_phone_key",
                    set_={
                        "is_starred": is_starred,
                        "updated_at": now,
                    },
                )
                .returning(ContactRow)
            )
            row = (await session.execute(stmt)).scalar_one()
            await session.commit()
            return _row_to_record(row)

    async def set_label(
        self,
        user_id: str,
        phone: str,
        *,
        color_label: str | None,
        display_name: str,
    ) -> ContactRecord:
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            stmt = (
                pg_insert(ContactRow)
                .values(
                    user_id=user_id,
                    display_name=display_name,
                    phone_number=phone,
                    color_label=color_label,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    constraint="contacts_user_phone_key",
                    set_={
                        "color_label": color_label,
                        "updated_at": now,
                    },
                )
                .returning(ContactRow)
            )
            row = (await session.execute(stmt)).scalar_one()
            await session.commit()
            return _row_to_record(row)

    async def set_tags(
        self,
        user_id: str,
        phone: str,
        *,
        tags: list[str],
        display_name: str,
    ) -> ContactRecord:
        normalized = normalize_tags(tags)
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            stmt = (
                pg_insert(ContactRow)
                .values(
                    user_id=user_id,
                    display_name=display_name,
                    phone_number=phone,
                    tags=normalized,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    constraint="contacts_user_phone_key",
                    set_={
                        "tags": normalized,
                        "updated_at": now,
                    },
                )
                .returning(ContactRow)
            )
            row = (await session.execute(stmt)).scalar_one()
            await session.commit()
            return _row_to_record(row)

    async def list_starred_phones(self, user_id: str) -> set[str]:
        async with self._session_factory() as session:
            stmt = select(ContactRow.phone_number).where(
                ContactRow.user_id == user_id,
                ContactRow.is_starred.is_(True),
            )
            result = await session.execute(stmt)
            return {row[0] for row in result.all()}

    async def list_tags(self, user_id: str) -> list[str]:
        async with self._session_factory() as session:
            stmt = (
                select(func.unnest(ContactRow.tags).distinct())
                .where(ContactRow.user_id == user_id)
                .order_by(func.unnest(ContactRow.tags))
            )
            result = await session.execute(stmt)
            return [row[0] for row in result.all()]

    async def get_contact_metadata(
        self, user_id: str, phones: set[str]
    ) -> dict[str, ContactRecord]:
        if not phones:
            return {}
        async with self._session_factory() as session:
            stmt = select(ContactRow).where(
                ContactRow.user_id == user_id,
                ContactRow.phone_number.in_(phones),
            )
            result = await session.execute(stmt)
            return {
                row.phone_number: _row_to_record(row)
                for row in result.scalars()
            }


def _row_to_record(row: ContactRow) -> ContactRecord:
    """Convert a ContactRow ORM object to a ContactRecord."""
    return ContactRecord(
        user_id=str(row.user_id),
        display_name=row.display_name,
        phone_number=row.phone_number,
        is_starred=row.is_starred,
        color_label=row.color_label,
        tags=list(row.tags) if row.tags else [],
    )
