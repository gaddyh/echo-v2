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
