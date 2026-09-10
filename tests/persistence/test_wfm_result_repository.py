"""Tests for WaitingForMeResultRepository (in-memory)."""

from __future__ import annotations

import pytest

from echo_v2.domain.waiting_for_me import WaitingForMeDecision, WaitingForMeResult
from echo_v2.persistence.chat_repositories import InMemoryWaitingForMeResultRepository

pytestmark = pytest.mark.asyncio


def _make_result(
    decision: WaitingForMeDecision = WaitingForMeDecision.WAITING_FOR_ME,
    target_version: int = 1,
) -> WaitingForMeResult:
    return WaitingForMeResult(
        decision=decision,
        confidence=0.9,
        reason="test reason",
        target_version=target_version,
    )


async def test_save_and_list_recent():
    repo = InMemoryWaitingForMeResultRepository()
    await repo.save(
        user_id="user-1",
        chat_id="chat-1@c.us",
        result=_make_result(WaitingForMeDecision.WAITING_FOR_ME, 1),
    )
    await repo.save(
        user_id="user-1",
        chat_id="chat-1@c.us",
        result=_make_result(WaitingForMeDecision.NOT_WAITING_FOR_ME, 2),
    )

    results = await repo.list_recent(user_id="user-1", chat_id="chat-1@c.us")
    assert len(results) == 2
    # Newest first
    assert results[0].target_version == 2
    assert results[1].target_version == 1


async def test_list_recent_filters_by_chat():
    repo = InMemoryWaitingForMeResultRepository()
    await repo.save(
        user_id="user-1",
        chat_id="chat-1@c.us",
        result=_make_result(WaitingForMeDecision.WAITING_FOR_ME, 1),
    )
    await repo.save(
        user_id="user-1",
        chat_id="chat-2@c.us",
        result=_make_result(WaitingForMeDecision.NOT_WAITING_FOR_ME, 1),
    )

    results = await repo.list_recent(user_id="user-1", chat_id="chat-1@c.us")
    assert len(results) == 1
    assert results[0].decision == WaitingForMeDecision.WAITING_FOR_ME


async def test_list_recent_filters_by_user():
    repo = InMemoryWaitingForMeResultRepository()
    await repo.save(
        user_id="user-1",
        chat_id="chat-1@c.us",
        result=_make_result(WaitingForMeDecision.WAITING_FOR_ME, 1),
    )
    await repo.save(
        user_id="user-2",
        chat_id="chat-1@c.us",
        result=_make_result(WaitingForMeDecision.NOT_WAITING_FOR_ME, 1),
    )

    results = await repo.list_recent(user_id="user-1", chat_id="chat-1@c.us")
    assert len(results) == 1
    assert results[0].decision == WaitingForMeDecision.WAITING_FOR_ME


async def test_list_recent_respects_limit():
    repo = InMemoryWaitingForMeResultRepository()
    for i in range(5):
        await repo.save(
            user_id="user-1",
            chat_id="chat-1@c.us",
            result=_make_result(WaitingForMeDecision.WAITING_FOR_ME, i + 1),
        )

    results = await repo.list_recent(
        user_id="user-1", chat_id="chat-1@c.us", limit=3,
    )
    assert len(results) == 3
    assert results[0].target_version == 5


async def test_list_recent_empty():
    repo = InMemoryWaitingForMeResultRepository()
    results = await repo.list_recent(user_id="user-1", chat_id="chat-1@c.us")
    assert results == []


async def test_satisfies_protocol():
    from echo_v2.persistence.chat_repositories import WaitingForMeResultRepository

    repo = InMemoryWaitingForMeResultRepository()
    assert isinstance(repo, WaitingForMeResultRepository)
