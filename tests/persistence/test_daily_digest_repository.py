"""Tests for InMemoryDailyDigestRepository."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from echo_v2.domain.digest import DailyDigestStatus
from echo_v2.persistence.digest_repositories import (
    DailyDigestRepository,
    InMemoryDailyDigestRepository,
)

pytestmark = pytest.mark.asyncio

TODAY = date(2026, 9, 12)


async def test_claim_or_get_inserts_new_row():
    repo = InMemoryDailyDigestRepository()
    digest = await repo.claim_or_get(user_id="user-1", local_date=TODAY)
    assert digest is not None
    assert digest.status == DailyDigestStatus.PROCESSING
    assert digest.local_date == TODAY


async def test_claim_or_get_returns_none_if_already_claimed():
    repo = InMemoryDailyDigestRepository()
    first = await repo.claim_or_get(user_id="user-1", local_date=TODAY)
    assert first is not None
    second = await repo.claim_or_get(user_id="user-1", local_date=TODAY)
    assert second is None


async def test_update_status_to_sent():
    repo = InMemoryDailyDigestRepository()
    digest = await repo.claim_or_get(user_id="user-1", local_date=TODAY)
    assert digest is not None
    sent_at = datetime(2026, 9, 12, 8, 0, 0, tzinfo=timezone.utc)
    updated = await repo.update_status(
        digest_id=digest.id,
        status=DailyDigestStatus.SENT,
        sent_at=sent_at,
        provider_message_id="msg-123",
        item_count=3,
    )
    assert updated is True
    row = await repo.get(user_id="user-1", local_date=TODAY)
    assert row is not None
    assert row.status == DailyDigestStatus.SENT
    assert row.sent_at == sent_at
    assert row.provider_message_id == "msg-123"
    assert row.item_count == 3


async def test_update_status_to_empty():
    repo = InMemoryDailyDigestRepository()
    digest = await repo.claim_or_get(user_id="user-1", local_date=TODAY)
    assert digest is not None
    await repo.update_status(
        digest_id=digest.id,
        status=DailyDigestStatus.EMPTY,
        item_count=0,
    )
    row = await repo.get(user_id="user-1", local_date=TODAY)
    assert row is not None
    assert row.status == DailyDigestStatus.EMPTY


async def test_update_status_to_indeterminate():
    repo = InMemoryDailyDigestRepository()
    digest = await repo.claim_or_get(user_id="user-1", local_date=TODAY)
    assert digest is not None
    await repo.update_status(
        digest_id=digest.id,
        status=DailyDigestStatus.INDETERMINATE,
        item_count=2,
    )
    row = await repo.get(user_id="user-1", local_date=TODAY)
    assert row is not None
    assert row.status == DailyDigestStatus.INDETERMINATE
    assert row.sent_at is None


async def test_get_returns_none_if_not_exists():
    repo = InMemoryDailyDigestRepository()
    row = await repo.get(user_id="user-1", local_date=TODAY)
    assert row is None


async def test_different_users_same_date():
    repo = InMemoryDailyDigestRepository()
    d1 = await repo.claim_or_get(user_id="user-1", local_date=TODAY)
    d2 = await repo.claim_or_get(user_id="user-2", local_date=TODAY)
    assert d1 is not None
    assert d2 is not None
    assert d1.id != d2.id


async def test_same_user_different_dates():
    repo = InMemoryDailyDigestRepository()
    d1 = await repo.claim_or_get(user_id="user-1", local_date=TODAY)
    d2 = await repo.claim_or_get(user_id="user-1", local_date=date(2026, 9, 13))
    assert d1 is not None
    assert d2 is not None
    assert d1.id != d2.id


async def test_satisfies_protocol():
    repo = InMemoryDailyDigestRepository()
    assert isinstance(repo, DailyDigestRepository)
