"""Tests for user_resolver — phone → (user_id, timezone) resolution."""

from __future__ import annotations

import pytest

from echo_v2.persistence.user_resolver import InMemoryUserResolver

pytestmark = pytest.mark.asyncio

# Valid Israeli mobile number for tests.
PHONE = "0546610653"
PHONE_E164 = "+972546610653"


async def test_inmemory_resolve_known_user():
    """Known user resolves to (user_id, timezone)."""
    resolver = InMemoryUserResolver()
    resolver.add_user(PHONE, "user-1", "Asia/Jerusalem")
    result = await resolver.resolve(PHONE)
    assert result is not None
    assert result == ("user-1", "Asia/Jerusalem")


async def test_inmemory_resolve_unknown_user():
    """Unknown user resolves to None."""
    resolver = InMemoryUserResolver()
    resolver.add_user(PHONE, "user-1")
    result = await resolver.resolve("0549999999")
    assert result is None


async def test_inmemory_resolve_invalid_phone():
    """Invalid phone number resolves to None."""
    resolver = InMemoryUserResolver()
    resolver.add_user(PHONE, "user-1")
    result = await resolver.resolve("not-a-phone")
    assert result is None


async def test_inmemory_resolve_normalizes_phone():
    """Resolver normalizes phone before lookup."""
    resolver = InMemoryUserResolver()
    resolver.add_user(PHONE, "user-1")
    # Lookup with E.164 format should match local format registration.
    result = await resolver.resolve(PHONE_E164)
    assert result is not None
    assert result[0] == "user-1"


async def test_inmemory_resolve_empty_resolver():
    """Empty resolver returns None for any phone."""
    resolver = InMemoryUserResolver()
    result = await resolver.resolve(PHONE)
    assert result is None


async def test_inmemory_resolve_default_timezone():
    """Default timezone is Asia/Jerusalem when not specified."""
    resolver = InMemoryUserResolver()
    resolver.add_user(PHONE, "user-1")
    result = await resolver.resolve(PHONE)
    assert result is not None
    assert result[1] == "Asia/Jerusalem"
