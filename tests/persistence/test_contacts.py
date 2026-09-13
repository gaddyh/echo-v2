"""Contact repository tests (in-memory + PostgreSQL).

The in-memory tests run without Docker. The Postgres tests require
Docker (testcontainers).
"""

from __future__ import annotations

import pytest

from echo_v2.persistence.contacts import (
    ContactRecord,
    ContactRepository,
    InMemoryContactRepository,
)

pytestmark = pytest.mark.asyncio


# --- In-memory tests --------------------------------------------------------


async def test_inmem_save_and_find_by_name():
    repo = InMemoryContactRepository()
    contact = ContactRecord(
        user_id="user-1",
        display_name="Alice",
        phone_number="+972501234567",
    )
    await repo.save(contact)
    found = await repo.find_by_name("user-1", "Alice")
    assert found is not None
    assert found.display_name == "Alice"
    assert found.phone_number == "+972501234567"


async def test_inmem_find_by_name_case_insensitive():
    repo = InMemoryContactRepository()
    await repo.save(
        ContactRecord(user_id="user-1", display_name="Alice", phone_number="+111")
    )
    found = await repo.find_by_name("user-1", "alice")
    assert found is not None
    assert found.display_name == "Alice"


async def test_inmem_find_by_name_partial_match():
    repo = InMemoryContactRepository()
    await repo.save(
        ContactRecord(user_id="user-1", display_name="זיפוש המהממת", phone_number="+222")
    )
    found = await repo.find_by_name("user-1", "זיפוש")
    assert found is not None
    assert found.display_name == "זיפוש המהממת"


async def test_inmem_find_by_name_not_found():
    repo = InMemoryContactRepository()
    assert await repo.find_by_name("user-1", "Nobody") is None


async def test_inmem_find_by_name_strips_whitespace():
    repo = InMemoryContactRepository()
    await repo.save(
        ContactRecord(user_id="user-1", display_name="Alice", phone_number="+111")
    )
    found = await repo.find_by_name("user-1", "  Alice  ")
    assert found is not None


async def test_inmem_find_by_phone():
    repo = InMemoryContactRepository()
    await repo.save(
        ContactRecord(user_id="user-1", display_name="Alice", phone_number="+972501234567")
    )
    found = await repo.find_by_phone("user-1", "+972501234567")
    assert found is not None
    assert found.display_name == "Alice"


async def test_inmem_find_by_phone_not_found():
    repo = InMemoryContactRepository()
    assert await repo.find_by_phone("user-1", "+000") is None


async def test_inmem_save_upserts_on_phone():
    repo = InMemoryContactRepository()
    await repo.save(
        ContactRecord(user_id="user-1", display_name="Alice", phone_number="+111")
    )
    await repo.save(
        ContactRecord(user_id="user-1", display_name="Alice Smith", phone_number="+111")
    )
    found = await repo.find_by_phone("user-1", "+111")
    assert found is not None
    assert found.display_name == "Alice Smith"


async def test_inmem_find_by_name_filters_by_user():
    repo = InMemoryContactRepository()
    await repo.save(
        ContactRecord(user_id="user-1", display_name="Alice", phone_number="+111")
    )
    await repo.save(
        ContactRecord(user_id="user-2", display_name="Alice", phone_number="+222")
    )
    found = await repo.find_by_name("user-1", "Alice")
    assert found is not None
    assert found.user_id == "user-1"
    assert found.phone_number == "+111"


# --- Base class NotImplementedError ------------------------------------------


async def test_base_save_raises_not_implemented():
    repo = ContactRepository()
    with pytest.raises(NotImplementedError):
        await repo.save(ContactRecord(user_id="u", display_name="n", phone_number="p"))


async def test_base_find_by_name_raises_not_implemented():
    repo = ContactRepository()
    with pytest.raises(NotImplementedError):
        await repo.find_by_name("u", "n")


async def test_base_find_by_phone_raises_not_implemented():
    repo = ContactRepository()
    with pytest.raises(NotImplementedError):
        await repo.find_by_phone("u", "p")


async def test_base_set_starred_raises_not_implemented():
    repo = ContactRepository()
    with pytest.raises(NotImplementedError):
        await repo.set_starred("u", "p", is_starred=True, display_name="n")


async def test_base_list_starred_phones_raises_not_implemented():
    repo = ContactRepository()
    with pytest.raises(NotImplementedError):
        await repo.list_starred_phones("u")


# --- Starred tests (in-memory) ----------------------------------------------


async def test_inmem_set_starred_on_existing_contact_preserves_name():
    repo = InMemoryContactRepository()
    await repo.save(
        ContactRecord(user_id="user-1", display_name="Alice", phone_number="+111")
    )
    record = await repo.set_starred(
        "user-1", "+111", is_starred=True, display_name="IGNORED"
    )
    assert record.is_starred is True
    assert record.display_name == "Alice"  # name preserved
    found = await repo.find_by_phone("user-1", "+111")
    assert found is not None
    assert found.is_starred is True
    assert found.display_name == "Alice"


async def test_inmem_set_starred_creates_contact_if_missing():
    repo = InMemoryContactRepository()
    record = await repo.set_starred(
        "user-1", "+999", is_starred=True, display_name="Bob"
    )
    assert record.is_starred is True
    assert record.display_name == "Bob"
    assert record.phone_number == "+999"
    found = await repo.find_by_phone("user-1", "+999")
    assert found is not None
    assert found.is_starred is True


async def test_inmem_set_starred_false_unstars():
    repo = InMemoryContactRepository()
    await repo.set_starred("user-1", "+111", is_starred=True, display_name="Alice")
    await repo.set_starred("user-1", "+111", is_starred=False, display_name="Alice")
    found = await repo.find_by_phone("user-1", "+111")
    assert found is not None
    assert found.is_starred is False


async def test_inmem_consecutive_true_then_false_leaves_false():
    """Two rapid requests true then false leave false (idempotent set, not toggle)."""
    repo = InMemoryContactRepository()
    await repo.set_starred("user-1", "+111", is_starred=True, display_name="Alice")
    await repo.set_starred("user-1", "+111", is_starred=False, display_name="Alice")
    found = await repo.find_by_phone("user-1", "+111")
    assert found is not None
    assert found.is_starred is False


async def test_inmem_save_preserves_is_starred():
    """save() must not reset is_starred on an existing contact."""
    repo = InMemoryContactRepository()
    await repo.save(
        ContactRecord(user_id="user-1", display_name="Alice", phone_number="+111")
    )
    await repo.set_starred("user-1", "+111", is_starred=True, display_name="Alice")
    # Now save again with a new name — is_starred must be preserved.
    await repo.save(
        ContactRecord(user_id="user-1", display_name="Alice Smith", phone_number="+111")
    )
    found = await repo.find_by_phone("user-1", "+111")
    assert found is not None
    assert found.display_name == "Alice Smith"
    assert found.is_starred is True


async def test_inmem_list_starred_phones():
    repo = InMemoryContactRepository()
    await repo.save(
        ContactRecord(user_id="user-1", display_name="Alice", phone_number="+111")
    )
    await repo.set_starred("user-1", "+222", is_starred=True, display_name="Bob")
    await repo.set_starred("user-1", "+333", is_starred=True, display_name="Carol")
    starred = await repo.list_starred_phones("user-1")
    assert starred == {"+222", "+333"}


async def test_inmem_list_starred_phones_filters_by_user():
    repo = InMemoryContactRepository()
    await repo.set_starred("user-1", "+111", is_starred=True, display_name="Alice")
    await repo.set_starred("user-2", "+222", is_starred=True, display_name="Bob")
    starred = await repo.list_starred_phones("user-1")
    assert starred == {"+111"}


async def test_inmem_list_starred_phones_empty():
    repo = InMemoryContactRepository()
    starred = await repo.list_starred_phones("user-1")
    assert starred == set()
