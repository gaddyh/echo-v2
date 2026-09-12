"""Tests for PostgresUserRepository — onboarding write + read operations.

Covers create_user, get_by_phone, update_onboarding_status,
update_first_name, resolve, and get_phone_by_id against a testcontainers
Postgres instance.
"""

from __future__ import annotations

import pytest

from echo_v2.persistence.user_repository import PostgresUserRepository

pytestmark = pytest.mark.asyncio

PHONE = "0546610653"
PHONE_E164 = "+972546610653"


@pytest.fixture
async def user_repo(session_factory, clean_db) -> PostgresUserRepository:
    """Fresh repository backed by the testcontainer session factory."""
    return PostgresUserRepository(session_factory)


# ---------------------------------------------------------------------------
# create_user
# ---------------------------------------------------------------------------


async def test_create_user_returns_user_id(user_repo):
    """create_user inserts a row and returns the user_id string."""
    user_id = await user_repo.create_user(PHONE)
    assert user_id is not None
    assert isinstance(user_id, str)


async def test_create_user_normalizes_phone(user_repo):
    """create_user stores the E.164-normalized phone number."""
    user_id = await user_repo.create_user(PHONE)
    phone = await user_repo.get_phone_by_id(user_id)
    assert phone == PHONE_E164


async def test_create_user_with_optional_fields(user_repo):
    """create_user accepts timezone, first_name, and onboarding_status."""
    user_id = await user_repo.create_user(
        PHONE,
        timezone="America/New_York",
        first_name="Alice",
        onboarding_status="active",
    )
    result = await user_repo.get_by_phone(PHONE)
    assert result is not None
    uid, status, first_name = result
    assert uid == user_id
    assert status == "active"
    assert first_name == "Alice"


async def test_create_user_raises_on_duplicate_phone(user_repo):
    """create_user raises ValueError when the phone already exists."""
    await user_repo.create_user(PHONE)
    with pytest.raises(ValueError, match="already exists"):
        await user_repo.create_user(PHONE)


async def test_create_user_duplicate_after_normalization(user_repo):
    """create_user raises even if the duplicate phone uses a different format."""
    await user_repo.create_user(PHONE)
    with pytest.raises(ValueError, match="already exists"):
        await user_repo.create_user(PHONE_E164)


# ---------------------------------------------------------------------------
# get_by_phone
# ---------------------------------------------------------------------------


async def test_get_by_phone_returns_tuple_for_existing_user(user_repo):
    """get_by_phone returns (user_id, onboarding_status, first_name)."""
    user_id = await user_repo.create_user(PHONE, first_name="Bob")
    result = await user_repo.get_by_phone(PHONE)
    assert result is not None
    assert result[0] == user_id
    assert result[1] == "pending"
    assert result[2] == "Bob"


async def test_get_by_phone_returns_none_for_nonexistent(user_repo):
    """get_by_phone returns None when no user matches."""
    result = await user_repo.get_by_phone("0549999999")
    assert result is None


async def test_get_by_phone_returns_none_for_invalid_phone(user_repo):
    """get_by_phone returns None for an unparseable phone string."""
    result = await user_repo.get_by_phone("not-a-phone")
    assert result is None


async def test_get_by_phone_normalizes_phone(user_repo):
    """get_by_phone normalizes the phone before lookup."""
    user_id = await user_repo.create_user(PHONE)
    result = await user_repo.get_by_phone(PHONE_E164)
    assert result is not None
    assert result[0] == user_id


# ---------------------------------------------------------------------------
# update_onboarding_status
# ---------------------------------------------------------------------------


async def test_update_onboarding_status_updates_status(user_repo):
    """update_onboarding_status changes the onboarding_status column."""
    user_id = await user_repo.create_user(PHONE)
    await user_repo.update_onboarding_status(user_id, "active")
    result = await user_repo.get_by_phone(PHONE)
    assert result is not None
    assert result[1] == "active"


# ---------------------------------------------------------------------------
# update_first_name
# ---------------------------------------------------------------------------


async def test_update_first_name_updates_name_and_status(user_repo):
    """update_first_name sets first_name and onboarding_status='active'."""
    user_id = await user_repo.create_user(PHONE)
    await user_repo.update_first_name(user_id, "Charlie")
    result = await user_repo.get_by_phone(PHONE)
    assert result is not None
    assert result[2] == "Charlie"
    assert result[1] == "active"


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


async def test_resolve_returns_user_id_and_timezone(user_repo):
    """resolve returns (user_id, timezone) for an existing user."""
    user_id = await user_repo.create_user(PHONE, timezone="Europe/London")
    result = await user_repo.resolve(PHONE)
    assert result is not None
    assert result[0] == user_id
    assert result[1] == "Europe/London"


async def test_resolve_returns_none_for_nonexistent(user_repo):
    """resolve returns None when no user matches."""
    result = await user_repo.resolve("0549999999")
    assert result is None


# ---------------------------------------------------------------------------
# get_phone_by_id
# ---------------------------------------------------------------------------


async def test_get_phone_by_id_returns_phone_for_existing(user_repo):
    """get_phone_by_id returns the normalized phone for an existing user."""
    user_id = await user_repo.create_user(PHONE)
    phone = await user_repo.get_phone_by_id(user_id)
    assert phone == PHONE_E164


async def test_get_phone_by_id_returns_none_for_nonexistent(user_repo):
    """get_phone_by_id returns None when no user matches the id."""
    phone = await user_repo.get_phone_by_id("00000000-0000-0000-0000-000000000000")
    assert phone is None
