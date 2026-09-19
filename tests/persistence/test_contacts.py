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
    normalize_tags,
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


async def test_inmem_find_by_name_no_match_with_existing_contacts():
    """find_by_name iterates existing contacts but finds no match."""
    repo = InMemoryContactRepository()
    await repo.save(
        ContactRecord(user_id="user-1", display_name="Alice", phone_number="+111")
    )
    await repo.save(
        ContactRecord(user_id="user-1", display_name="Bob", phone_number="+222")
    )
    # Name that doesn't match any contact — loop iterates but condition is false.
    assert await repo.find_by_name("user-1", "Charlie") is None


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


# --- Label tests (in-memory) -----------------------------------------------


async def test_inmem_set_label_on_existing_contact_preserves_name_and_star():
    repo = InMemoryContactRepository()
    await repo.save(
        ContactRecord(user_id="user-1", display_name="Alice", phone_number="+111")
    )
    await repo.set_starred("user-1", "+111", is_starred=True, display_name="Alice")
    record = await repo.set_label(
        "user-1", "+111", color_label="red", display_name="IGNORED"
    )
    assert record.color_label == "red"
    assert record.display_name == "Alice"  # name preserved
    assert record.is_starred is True  # star preserved
    found = await repo.find_by_phone("user-1", "+111")
    assert found is not None
    assert found.color_label == "red"
    assert found.display_name == "Alice"
    assert found.is_starred is True


async def test_inmem_set_label_creates_contact_if_missing():
    repo = InMemoryContactRepository()
    record = await repo.set_label(
        "user-1", "+999", color_label="blue", display_name="Bob"
    )
    assert record.color_label == "blue"
    assert record.display_name == "Bob"
    assert record.phone_number == "+999"
    found = await repo.find_by_phone("user-1", "+999")
    assert found is not None
    assert found.color_label == "blue"


async def test_inmem_set_label_none_clears_label():
    repo = InMemoryContactRepository()
    await repo.set_label("user-1", "+111", color_label="red", display_name="Alice")
    await repo.set_label("user-1", "+111", color_label=None, display_name="Alice")
    found = await repo.find_by_phone("user-1", "+111")
    assert found is not None
    assert found.color_label is None


async def test_inmem_set_label_preserves_tags():
    repo = InMemoryContactRepository()
    await repo.set_tags("user-1", "+111", tags=["work"], display_name="Alice")
    await repo.set_label("user-1", "+111", color_label="green", display_name="Alice")
    found = await repo.find_by_phone("user-1", "+111")
    assert found is not None
    assert found.color_label == "green"
    assert found.tags == ["work"]


# --- Tags tests (in-memory) -------------------------------------------------


async def test_inmem_set_tags_on_existing_contact_preserves_name_and_star():
    repo = InMemoryContactRepository()
    await repo.save(
        ContactRecord(user_id="user-1", display_name="Alice", phone_number="+111")
    )
    await repo.set_starred("user-1", "+111", is_starred=True, display_name="Alice")
    record = await repo.set_tags(
        "user-1", "+111", tags=["work", "urgent"], display_name="IGNORED"
    )
    assert record.tags == ["work", "urgent"]
    assert record.display_name == "Alice"  # name preserved
    assert record.is_starred is True  # star preserved
    found = await repo.find_by_phone("user-1", "+111")
    assert found is not None
    assert found.tags == ["work", "urgent"]
    assert found.display_name == "Alice"
    assert found.is_starred is True


async def test_inmem_set_tags_creates_contact_if_missing():
    repo = InMemoryContactRepository()
    record = await repo.set_tags(
        "user-1", "+999", tags=["work"], display_name="Bob"
    )
    assert record.tags == ["work"]
    assert record.display_name == "Bob"
    found = await repo.find_by_phone("user-1", "+999")
    assert found is not None
    assert found.tags == ["work"]


async def test_inmem_set_tags_empty_clears_tags():
    repo = InMemoryContactRepository()
    await repo.set_tags("user-1", "+111", tags=["work"], display_name="Alice")
    await repo.set_tags("user-1", "+111", tags=[], display_name="Alice")
    found = await repo.find_by_phone("user-1", "+111")
    assert found is not None
    assert found.tags == []


async def test_inmem_set_tags_preserves_label():
    repo = InMemoryContactRepository()
    await repo.set_label("user-1", "+111", color_label="red", display_name="Alice")
    await repo.set_tags("user-1", "+111", tags=["work"], display_name="Alice")
    found = await repo.find_by_phone("user-1", "+111")
    assert found is not None
    assert found.color_label == "red"
    assert found.tags == ["work"]


async def test_inmem_set_tags_normalizes():
    repo = InMemoryContactRepository()
    record = await repo.set_tags(
        "user-1",
        "+111",
        tags=["  Work  ", "", "work", "URGENT", "urgent"],
        display_name="Alice",
    )
    # Stripped, deduped case-insensitive, first-seen casing preserved.
    assert record.tags == ["Work", "URGENT"]


async def test_inmem_list_tags():
    repo = InMemoryContactRepository()
    await repo.set_tags("user-1", "+111", tags=["work", "urgent"], display_name="Alice")
    await repo.set_tags("user-1", "+222", tags=["family", "work"], display_name="Bob")
    await repo.set_tags("user-2", "+333", tags=["other"], display_name="Carol")
    tags = await repo.list_tags("user-1")
    assert tags == ["family", "urgent", "work"]


async def test_inmem_list_tags_empty():
    repo = InMemoryContactRepository()
    tags = await repo.list_tags("user-1")
    assert tags == []


# --- get_contact_metadata tests (in-memory) --------------------------------


async def test_inmem_get_contact_metadata_batch():
    repo = InMemoryContactRepository()
    await repo.save(
        ContactRecord(user_id="user-1", display_name="Alice", phone_number="+111")
    )
    await repo.set_starred("user-1", "+222", is_starred=True, display_name="Bob")
    await repo.set_label("user-1", "+333", color_label="red", display_name="Carol")
    meta = await repo.get_contact_metadata("user-1", {"+111", "+222", "+333", "+999"})
    assert "+111" in meta
    assert meta["+111"].display_name == "Alice"
    assert "+222" in meta
    assert meta["+222"].is_starred is True
    assert "+333" in meta
    assert meta["+333"].color_label == "red"
    assert "+999" not in meta  # no contact record


async def test_inmem_get_contact_metadata_empty_phones():
    repo = InMemoryContactRepository()
    meta = await repo.get_contact_metadata("user-1", set())
    assert meta == {}


async def test_inmem_get_contact_metadata_filters_by_user():
    repo = InMemoryContactRepository()
    await repo.save(
        ContactRecord(user_id="user-1", display_name="Alice", phone_number="+111")
    )
    await repo.save(
        ContactRecord(user_id="user-2", display_name="Bob", phone_number="+111")
    )
    meta = await repo.get_contact_metadata("user-1", {"+111"})
    assert "+111" in meta
    assert meta["+111"].user_id == "user-1"


# --- Base class NotImplementedError for new methods --------------------------


async def test_base_set_label_raises_not_implemented():
    repo = ContactRepository()
    with pytest.raises(NotImplementedError):
        await repo.set_label("u", "p", color_label="red", display_name="n")


async def test_base_set_tags_raises_not_implemented():
    repo = ContactRepository()
    with pytest.raises(NotImplementedError):
        await repo.set_tags("u", "p", tags=[], display_name="n")


async def test_base_list_tags_raises_not_implemented():
    repo = ContactRepository()
    with pytest.raises(NotImplementedError):
        await repo.list_tags("u")


async def test_base_get_contact_metadata_raises_not_implemented():
    repo = ContactRepository()
    with pytest.raises(NotImplementedError):
        await repo.get_contact_metadata("u", set())


# --- normalize_tags unit tests ----------------------------------------------
# These are sync tests; the pytestmark above applies to async tests only
# in practice, but we suppress the warning by not using the mark here.
# Ruff/pytest still collects them fine.


def test_normalize_tags_strips_whitespace():
    assert normalize_tags(["  work  ", " urgent "]) == ["work", "urgent"]


def test_normalize_tags_rejects_empty():
    assert normalize_tags(["", "  ", "work"]) == ["work"]


def test_normalize_tags_dedupes_case_insensitive():
    assert normalize_tags(["Work", "WORK", "work"]) == ["Work"]


def test_normalize_tags_preserves_first_casing():
    assert normalize_tags(["work", "Work"]) == ["work"]
    assert normalize_tags(["Work", "work"]) == ["Work"]


def test_normalize_tags_truncates_long_tags():
    long_tag = "x" * 50
    result = normalize_tags([long_tag])
    assert len(result) == 1
    assert len(result[0]) == 40


def test_normalize_tags_limits_to_max():
    tags = [f"tag{i}" for i in range(15)]
    result = normalize_tags(tags)
    assert len(result) == 10


def test_normalize_tags_empty_input():
    assert normalize_tags([]) == []
