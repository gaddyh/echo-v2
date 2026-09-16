"""Postgres-backed contact repository tests (requires Docker)."""

from __future__ import annotations

import pytest
import pytest_asyncio

from echo_v2.persistence.contacts import (
    ContactRecord,
    PostgresContactRepository,
)
from tests.persistence.conftest import insert_user

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def contacts_repo(session_factory, clean_db):
    return PostgresContactRepository(session_factory)


async def test_postgres_save_and_find_by_phone(contacts_repo, session_factory):
    user_id = await insert_user(session_factory)
    await contacts_repo.save(
        ContactRecord(user_id=user_id, display_name="Alice", phone_number="+972501234567")
    )
    found = await contacts_repo.find_by_phone(user_id, "+972501234567")
    assert found is not None
    assert found.display_name == "Alice"
    assert found.phone_number == "+972501234567"


async def test_postgres_find_by_phone_not_found(contacts_repo, session_factory):
    user_id = await insert_user(session_factory)
    assert await contacts_repo.find_by_phone(user_id, "+000") is None


async def test_postgres_find_by_name_exact(contacts_repo, session_factory):
    user_id = await insert_user(session_factory)
    await contacts_repo.save(
        ContactRecord(user_id=user_id, display_name="Alice", phone_number="+111")
    )
    found = await contacts_repo.find_by_name(user_id, "Alice")
    assert found is not None
    assert found.display_name == "Alice"


async def test_postgres_find_by_name_partial(contacts_repo, session_factory):
    user_id = await insert_user(session_factory)
    await contacts_repo.save(
        ContactRecord(user_id=user_id, display_name="זיפוש המהממת", phone_number="+222")
    )
    found = await contacts_repo.find_by_name(user_id, "זיפוש")
    assert found is not None
    assert found.display_name == "זיפוש המהממת"


async def test_postgres_find_by_name_case_insensitive(contacts_repo, session_factory):
    user_id = await insert_user(session_factory)
    await contacts_repo.save(
        ContactRecord(user_id=user_id, display_name="Alice", phone_number="+111")
    )
    found = await contacts_repo.find_by_name(user_id, "alice")
    assert found is not None


async def test_postgres_find_by_name_not_found(contacts_repo, session_factory):
    user_id = await insert_user(session_factory)
    assert await contacts_repo.find_by_name(user_id, "Nobody") is None


async def test_postgres_save_upserts_on_phone(contacts_repo, session_factory):
    user_id = await insert_user(session_factory)
    await contacts_repo.save(
        ContactRecord(user_id=user_id, display_name="Alice", phone_number="+111")
    )
    await contacts_repo.save(
        ContactRecord(user_id=user_id, display_name="Alice Smith", phone_number="+111")
    )
    found = await contacts_repo.find_by_phone(user_id, "+111")
    assert found is not None
    assert found.display_name == "Alice Smith"


# --- set_starred -----------------------------------------------------------


async def test_set_starred_creates_new_contact(contacts_repo, session_factory):
    """set_starred on non-existent contact creates it with is_starred=True."""
    user_id = await insert_user(session_factory)

    record = await contacts_repo.set_starred(
        user_id, "+111", is_starred=True, display_name="Alice"
    )
    assert record.is_starred is True
    assert record.display_name == "Alice"
    assert record.phone_number == "+111"

    found = await contacts_repo.find_by_phone(user_id, "+111")
    assert found is not None
    assert found.is_starred is True


async def test_set_starred_preserves_display_name_on_update(
    contacts_repo, session_factory
):
    """Update existing contact: only is_starred changes, display_name preserved."""
    user_id = await insert_user(session_factory)
    await contacts_repo.save(
        ContactRecord(user_id=user_id, display_name="Alice", phone_number="+111")
    )

    record = await contacts_repo.set_starred(
        user_id, "+111", is_starred=True, display_name="ignored-name"
    )
    assert record.is_starred is True
    # on_conflict_do_update only sets is_starred + updated_at; display_name
    # from the INSERT values is ignored on conflict, so the original is kept.
    assert record.display_name == "Alice"

    found = await contacts_repo.find_by_phone(user_id, "+111")
    assert found is not None
    assert found.is_starred is True
    assert found.display_name == "Alice"


async def test_set_starred_unstar(contacts_repo, session_factory):
    """Unstar: set_starred with is_starred=False."""
    user_id = await insert_user(session_factory)
    await contacts_repo.set_starred(
        user_id, "+111", is_starred=True, display_name="Alice"
    )

    record = await contacts_repo.set_starred(
        user_id, "+111", is_starred=False, display_name="Alice"
    )
    assert record.is_starred is False

    found = await contacts_repo.find_by_phone(user_id, "+111")
    assert found is not None
    assert found.is_starred is False


async def test_set_starred_nonexistent_uses_phone_as_display_name(
    contacts_repo, session_factory
):
    """set_starred on non-existent contact creates contact with phone as display_name."""
    user_id = await insert_user(session_factory)

    record = await contacts_repo.set_starred(
        user_id, "+972501234567", is_starred=True, display_name="+972501234567"
    )
    assert record.is_starred is True
    assert record.display_name == "+972501234567"


# --- set_label -------------------------------------------------------------


async def test_set_label_creates_new_contact(contacts_repo, session_factory):
    """Create new contact with label."""
    user_id = await insert_user(session_factory)

    record = await contacts_repo.set_label(
        user_id, "+111", color_label="red", display_name="Alice"
    )
    assert record.color_label == "red"
    assert record.display_name == "Alice"

    found = await contacts_repo.find_by_phone(user_id, "+111")
    assert found is not None
    assert found.color_label == "red"


async def test_set_label_updates_existing(contacts_repo, session_factory):
    """Update existing contact label."""
    user_id = await insert_user(session_factory)
    await contacts_repo.set_label(
        user_id, "+111", color_label="red", display_name="Alice"
    )

    record = await contacts_repo.set_label(
        user_id, "+111", color_label="blue", display_name="Alice"
    )
    assert record.color_label == "blue"

    found = await contacts_repo.find_by_phone(user_id, "+111")
    assert found is not None
    assert found.color_label == "blue"


async def test_set_label_clears_with_none(contacts_repo, session_factory):
    """Clear label with None."""
    user_id = await insert_user(session_factory)
    await contacts_repo.set_label(
        user_id, "+111", color_label="red", display_name="Alice"
    )

    record = await contacts_repo.set_label(
        user_id, "+111", color_label=None, display_name="Alice"
    )
    assert record.color_label is None

    found = await contacts_repo.find_by_phone(user_id, "+111")
    assert found is not None
    assert found.color_label is None


# --- set_tags --------------------------------------------------------------


async def test_set_tags_creates_new_contact(contacts_repo, session_factory):
    """Create new contact with tags."""
    user_id = await insert_user(session_factory)

    record = await contacts_repo.set_tags(
        user_id, "+111", tags=["work", "vip"], display_name="Alice"
    )
    assert record.tags == ["work", "vip"]

    found = await contacts_repo.find_by_phone(user_id, "+111")
    assert found is not None
    assert found.tags == ["work", "vip"]


async def test_set_tags_updates_existing(contacts_repo, session_factory):
    """Update existing contact tags."""
    user_id = await insert_user(session_factory)
    await contacts_repo.set_tags(
        user_id, "+111", tags=["work"], display_name="Alice"
    )

    record = await contacts_repo.set_tags(
        user_id, "+111", tags=["personal", "friend"], display_name="Alice"
    )
    assert record.tags == ["personal", "friend"]

    found = await contacts_repo.find_by_phone(user_id, "+111")
    assert found is not None
    assert found.tags == ["personal", "friend"]


async def test_set_tags_normalizes(contacts_repo, session_factory):
    """Tags are normalized (strip, dedupe, max length)."""
    user_id = await insert_user(session_factory)

    record = await contacts_repo.set_tags(
        user_id,
        "+111",
        tags=["  Work  ", "work", "URGENT"],
        display_name="Alice",
    )
    # "  Work  " -> "Work", "work" is a case-insensitive dup of "Work",
    # "URGENT" is kept.
    assert record.tags == ["Work", "URGENT"]


async def test_set_tags_clears_with_empty_list(contacts_repo, session_factory):
    """Clear tags with empty list."""
    user_id = await insert_user(session_factory)
    await contacts_repo.set_tags(
        user_id, "+111", tags=["work"], display_name="Alice"
    )

    record = await contacts_repo.set_tags(
        user_id, "+111", tags=[], display_name="Alice"
    )
    assert record.tags == []

    found = await contacts_repo.find_by_phone(user_id, "+111")
    assert found is not None
    assert found.tags == []


# --- list_tags -------------------------------------------------------------


async def test_list_tags_returns_distinct_sorted(contacts_repo, session_factory):
    """Returns distinct tags sorted alphabetically."""
    user_id = await insert_user(session_factory)
    await contacts_repo.set_tags(
        user_id, "+111", tags=["zebra", "apple"], display_name="Alice"
    )
    await contacts_repo.set_tags(
        user_id, "+222", tags=["mango", "apple"], display_name="Bob"
    )

    tags = await contacts_repo.list_tags(user_id)
    # Distinct + sorted.
    assert tags == ["apple", "mango", "zebra"]


async def test_list_tags_empty_when_no_tags(contacts_repo, session_factory):
    """Returns empty list when no contacts have tags."""
    user_id = await insert_user(session_factory)
    await contacts_repo.save(
        ContactRecord(user_id=user_id, display_name="Alice", phone_number="+111")
    )

    tags = await contacts_repo.list_tags(user_id)
    assert tags == []


async def test_list_tags_overlapping_tags_distinct(contacts_repo, session_factory):
    """Multiple contacts with overlapping tags -> distinct only."""
    user_id = await insert_user(session_factory)
    await contacts_repo.set_tags(
        user_id, "+111", tags=["work", "vip"], display_name="Alice"
    )
    await contacts_repo.set_tags(
        user_id, "+222", tags=["vip", "family"], display_name="Bob"
    )

    tags = await contacts_repo.list_tags(user_id)
    # "vip" appears in both contacts but should be returned once.
    assert tags == ["family", "vip", "work"]


# --- get_contact_metadata --------------------------------------------------


async def test_get_contact_metadata_returns_record(contacts_repo, session_factory):
    """Returns ContactRecord for existing contact."""
    user_id = await insert_user(session_factory)
    await contacts_repo.set_starred(
        user_id, "+111", is_starred=True, display_name="Alice"
    )
    await contacts_repo.set_label(
        user_id, "+111", color_label="red", display_name="Alice"
    )
    await contacts_repo.set_tags(
        user_id, "+111", tags=["work"], display_name="Alice"
    )

    metadata = await contacts_repo.get_contact_metadata(user_id, {"+111"})
    assert "+111" in metadata
    record = metadata["+111"]
    assert record.is_starred is True
    assert record.color_label == "red"
    assert record.tags == ["work"]
    assert record.display_name == "Alice"


async def test_get_contact_metadata_returns_none_for_missing(
    contacts_repo, session_factory
):
    """Returns None for non-existent phone (not in dict)."""
    user_id = await insert_user(session_factory)

    metadata = await contacts_repo.get_contact_metadata(user_id, {"+000"})
    assert metadata == {}


async def test_get_contact_metadata_multiple_phones(
    contacts_repo, session_factory
):
    """Returns dict with only existing contacts."""
    user_id = await insert_user(session_factory)
    await contacts_repo.save(
        ContactRecord(user_id=user_id, display_name="Alice", phone_number="+111")
    )
    await contacts_repo.save(
        ContactRecord(user_id=user_id, display_name="Bob", phone_number="+222")
    )

    metadata = await contacts_repo.get_contact_metadata(
        user_id, {"+111", "+222", "+999"}
    )
    assert set(metadata.keys()) == {"+111", "+222"}
    assert metadata["+111"].display_name == "Alice"
    assert metadata["+222"].display_name == "Bob"


async def test_get_contact_metadata_empty_phones(contacts_repo, session_factory):
    """Empty phone set returns empty dict."""
    user_id = await insert_user(session_factory)

    metadata = await contacts_repo.get_contact_metadata(user_id, set())
    assert metadata == {}
