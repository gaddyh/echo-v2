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
