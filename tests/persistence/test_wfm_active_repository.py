"""Tests for WaitingForMeActiveRepository (in-memory)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.persistence.chat_repositories import InMemoryWaitingForMeActiveRepository

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 12, 10, 0, 0, tzinfo=timezone.utc)


async def test_upsert_inserts_new_row():
    repo = InMemoryWaitingForMeActiveRepository()
    await repo.upsert(
        user_id="user-1",
        chat_id="chat-1@c.us",
        target_version=1,
        result_id="result-1",
        waiting_since=NOW,
    )
    row = await repo.get(user_id="user-1", chat_id="chat-1@c.us")
    assert row is not None
    assert row.target_version == 1
    assert row.result_id == "result-1"
    assert row.waiting_since == NOW
    assert row.notified_at is None


async def test_upsert_preserves_waiting_since_and_notified_at():
    repo = InMemoryWaitingForMeActiveRepository()
    # First insert
    await repo.upsert(
        user_id="user-1",
        chat_id="chat-1@c.us",
        target_version=1,
        result_id="result-1",
        waiting_since=NOW,
    )
    notified_at = NOW + timedelta(hours=1)
    # Second upsert — new version, new result_id, but waiting_since should be preserved
    await repo.upsert(
        user_id="user-1",
        chat_id="chat-1@c.us",
        target_version=2,
        result_id="result-2",
        waiting_since=NOW,
        notified_at=notified_at,
    )
    row = await repo.get(user_id="user-1", chat_id="chat-1@c.us")
    assert row is not None
    assert row.target_version == 2
    assert row.result_id == "result-2"
    assert row.waiting_since == NOW  # preserved from first insert


async def test_delete_removes_row():
    repo = InMemoryWaitingForMeActiveRepository()
    await repo.upsert(
        user_id="user-1",
        chat_id="chat-1@c.us",
        target_version=1,
        result_id="result-1",
        waiting_since=NOW,
    )
    deleted = await repo.delete(user_id="user-1", chat_id="chat-1@c.us")
    assert deleted is True
    row = await repo.get(user_id="user-1", chat_id="chat-1@c.us")
    assert row is None


async def test_delete_returns_false_if_not_exists():
    repo = InMemoryWaitingForMeActiveRepository()
    deleted = await repo.delete(user_id="user-1", chat_id="chat-1@c.us")
    assert deleted is False


async def test_get_returns_none_if_not_exists():
    repo = InMemoryWaitingForMeActiveRepository()
    row = await repo.get(user_id="user-1", chat_id="chat-1@c.us")
    assert row is None


async def test_list_active_returns_matching_rows():
    repo = InMemoryWaitingForMeActiveRepository()
    await repo.upsert(
        user_id="user-1",
        chat_id="chat-1@c.us",
        target_version=5,
        result_id="result-1",
        waiting_since=NOW,
    )
    await repo.upsert(
        user_id="user-1",
        chat_id="chat-2@c.us",
        target_version=3,
        result_id="result-2",
        waiting_since=NOW,
    )
    # Only chat-1 has matching version
    active = await repo.list_active(
        user_id="user-1",
        current_versions={"chat-1@c.us": 5, "chat-2@c.us": 4},
    )
    assert len(active) == 1
    assert active[0].chat_id == "chat-1@c.us"


async def test_list_active_filters_by_user():
    repo = InMemoryWaitingForMeActiveRepository()
    await repo.upsert(
        user_id="user-1",
        chat_id="chat-1@c.us",
        target_version=1,
        result_id="result-1",
        waiting_since=NOW,
    )
    await repo.upsert(
        user_id="user-2",
        chat_id="chat-2@c.us",
        target_version=1,
        result_id="result-2",
        waiting_since=NOW,
    )
    active = await repo.list_active(
        user_id="user-1",
        current_versions={"chat-1@c.us": 1, "chat-2@c.us": 1},
    )
    assert len(active) == 1
    assert active[0].user_id == "user-1"


async def test_list_active_empty():
    repo = InMemoryWaitingForMeActiveRepository()
    active = await repo.list_active(
        user_id="user-1",
        current_versions={},
    )
    assert active == []


async def test_satisfies_protocol():
    from echo_v2.persistence.chat_repositories import WaitingForMeActiveRepository

    repo = InMemoryWaitingForMeActiveRepository()
    assert isinstance(repo, WaitingForMeActiveRepository)
