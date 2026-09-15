"""Postgres waitlist repository tests (requires Docker)."""

from __future__ import annotations

import pytest
import pytest_asyncio

from echo_v2.persistence.waitlist import PostgresWaitlistRepository

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def waitlist_repo(session_factory, clean_db):
    return PostgresWaitlistRepository(session_factory)


async def test_add_first_returns_true(waitlist_repo):
    inserted = await waitlist_repo.add(name="דנה לוי", phone_number="+972501234567")
    assert inserted is True

    signups = await waitlist_repo.list_all()
    assert len(signups) == 1
    assert signups[0].name == "דנה לוי"
    assert signups[0].phone_number == "+972501234567"
    assert signups[0].created_at is not None


async def test_add_duplicate_phone_returns_false(waitlist_repo):
    first = await waitlist_repo.add(name="דנה", phone_number="+972501234567")
    second = await waitlist_repo.add(name="שם אחר", phone_number="+972501234567")
    assert first is True
    assert second is False

    # Original signup preserved, not overwritten.
    signups = await waitlist_repo.list_all()
    assert len(signups) == 1
    assert signups[0].name == "דנה"


async def test_list_all_ordered_oldest_first(waitlist_repo):
    await waitlist_repo.add(name="ראשונה", phone_number="+972501111111")
    await waitlist_repo.add(name="שני", phone_number="+972502222222")
    await waitlist_repo.add(name="שלישית", phone_number="+972503333333")

    signups = await waitlist_repo.list_all()
    assert [s.name for s in signups] == ["ראשונה", "שני", "שלישית"]


async def test_add_with_willingness_to_pay(waitlist_repo):
    inserted = await waitlist_repo.add(
        name="דנה",
        phone_number="+972501234567",
        willingness_to_pay="20_50",
    )
    assert inserted is True
    signups = await waitlist_repo.list_all()
    assert len(signups) == 1
    assert signups[0].willingness_to_pay == "20_50"


async def test_add_without_willingness_to_pay_defaults_null(waitlist_repo):
    await waitlist_repo.add(name="דנה", phone_number="+972501234567")
    signups = await waitlist_repo.list_all()
    assert signups[0].willingness_to_pay is None


async def test_count_returns_total_signups(waitlist_repo):
    assert await waitlist_repo.count() == 0
    await waitlist_repo.add(name="ראשונה", phone_number="+972501111111")
    await waitlist_repo.add(name="שני", phone_number="+972502222222")
    # Duplicate — should not increment count.
    await waitlist_repo.add(name="שכפול", phone_number="+972501111111")
    assert await waitlist_repo.count() == 2
