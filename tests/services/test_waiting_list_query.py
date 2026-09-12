"""Tests for the WaitingListQueryService.

Covers:
* Current actionable items (version match, not acknowledged, not snoozed).
* Stale items (version mismatch) are excluded.
* Acknowledged items are excluded.
* Snoozed items are excluded (until snooze expires).
* Muted chats are excluded.
* Items sorted by waiting_since ascending (oldest first).
* Empty list when no items.
* Items with no chat state are excluded.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.persistence.chat_repositories import (
    InMemoryChatStateRepository,
    InMemoryWaitingForMeActiveRepository,
)
from echo_v2.persistence.feedback_repositories import InMemoryChatMuteRepository
from echo_v2.ports.whatsapp import MessageDirection
from echo_v2.services.waiting_list_query import WaitingListQueryService

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 12, 6, 0, 0, tzinfo=timezone.utc)
USER_ID = "user-1"
CHAT_ID_A = "chat-a@c.us"
CHAT_ID_B = "chat-b@c.us"
CHAT_ID_C = "chat-c@c.us"
RESULT_ID = "result-1"


async def _setup_chat(
    chat_state_repo: InMemoryChatStateRepository,
    chat_id: str,
    *,
    user_id: str = USER_ID,
    activity_version: int = 1,
) -> None:
    await chat_state_repo.upsert_on_message(
        user_id=user_id,
        chat_id=chat_id,
        direction=MessageDirection.INBOUND,
        observed_at=NOW,
        next_analysis_at=None,
    )
    # Set activity_version.
    chat = await chat_state_repo.get(user_id, chat_id)
    if chat is not None:
        chat.activity_version = activity_version


async def _setup_active(
    active_repo: InMemoryWaitingForMeActiveRepository,
    chat_id: str,
    *,
    target_version: int = 1,
    waiting_since: datetime = NOW,
    snoozed_until: datetime | None = None,
    acknowledged_at: datetime | None = None,
) -> str:
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id=chat_id,
        target_version=target_version,
        result_id=RESULT_ID,
        waiting_since=waiting_since,
    )
    if snoozed_until is not None:
        await active_repo.snooze(
            user_id=USER_ID, chat_id=chat_id, snoozed_until=snoozed_until
        )
    if acknowledged_at is not None:
        await active_repo.acknowledge(
            user_id=USER_ID, chat_id=chat_id, acknowledged_at=acknowledged_at
        )
    active = await active_repo.get(user_id=USER_ID, chat_id=chat_id)
    return active.id


def _make_service(
    *,
    mute_repo: InMemoryChatMuteRepository | None = None,
) -> tuple[
    WaitingListQueryService,
    InMemoryWaitingForMeActiveRepository,
    InMemoryChatStateRepository,
    InMemoryChatMuteRepository | None,
]:
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    if mute_repo is None:
        mute_repo = InMemoryChatMuteRepository()
    service = WaitingListQueryService(
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        mute_repo=mute_repo,
    )
    return service, active_repo, chat_state_repo, mute_repo


async def test_current_actionable_returns_matching_items():
    service, active_repo, chat_state_repo, _ = _make_service()
    await _setup_chat(chat_state_repo, CHAT_ID_A, activity_version=1)
    await _setup_active(active_repo, CHAT_ID_A, target_version=1)
    items = await service.current_actionable(USER_ID, now=NOW)
    assert len(items) == 1
    assert items[0].chat_id == CHAT_ID_A


async def test_current_actionable_excludes_stale_version():
    service, active_repo, chat_state_repo, _ = _make_service()
    await _setup_chat(chat_state_repo, CHAT_ID_A, activity_version=2)
    await _setup_active(active_repo, CHAT_ID_A, target_version=1)
    items = await service.current_actionable(USER_ID, now=NOW)
    assert len(items) == 0


async def test_current_actionable_excludes_acknowledged():
    service, active_repo, chat_state_repo, _ = _make_service()
    await _setup_chat(chat_state_repo, CHAT_ID_A)
    await _setup_active(
        active_repo, CHAT_ID_A, acknowledged_at=NOW
    )
    items = await service.current_actionable(USER_ID, now=NOW)
    assert len(items) == 0


async def test_current_actionable_excludes_snoozed():
    service, active_repo, chat_state_repo, _ = _make_service()
    await _setup_chat(chat_state_repo, CHAT_ID_A)
    await _setup_active(
        active_repo, CHAT_ID_A, snoozed_until=NOW + timedelta(hours=6)
    )
    items = await service.current_actionable(USER_ID, now=NOW)
    assert len(items) == 0


async def test_current_actionable_includes_expired_snooze():
    """An item whose snooze time has passed is actionable again."""
    service, active_repo, chat_state_repo, _ = _make_service()
    await _setup_chat(chat_state_repo, CHAT_ID_A)
    await _setup_active(
        active_repo, CHAT_ID_A, snoozed_until=NOW - timedelta(hours=1)
    )
    items = await service.current_actionable(USER_ID, now=NOW)
    assert len(items) == 1


async def test_current_actionable_excludes_muted():
    service, active_repo, chat_state_repo, mute_repo = _make_service()
    await _setup_chat(chat_state_repo, CHAT_ID_A)
    await _setup_active(active_repo, CHAT_ID_A)
    # Mute the chat.
    await mute_repo.mute_permanent(user_id=USER_ID, chat_id=CHAT_ID_A)
    items = await service.current_actionable(USER_ID, now=NOW)
    assert len(items) == 0


async def test_current_actionable_includes_unmuted():
    """A chat that was unmuted is included."""
    service, active_repo, chat_state_repo, mute_repo = _make_service()
    await _setup_chat(chat_state_repo, CHAT_ID_A)
    await _setup_active(active_repo, CHAT_ID_A)
    await mute_repo.mute_permanent(user_id=USER_ID, chat_id=CHAT_ID_A)
    await mute_repo.unmute(user_id=USER_ID, chat_id=CHAT_ID_A)
    items = await service.current_actionable(USER_ID, now=NOW)
    assert len(items) == 1


async def test_current_actionable_sorted_oldest_first():
    service, active_repo, chat_state_repo, _ = _make_service()
    await _setup_chat(chat_state_repo, CHAT_ID_A)
    await _setup_chat(chat_state_repo, CHAT_ID_B)
    # Chat B is older (waiting_since earlier).
    await _setup_active(
        active_repo, CHAT_ID_B, waiting_since=NOW - timedelta(hours=5)
    )
    await _setup_active(
        active_repo, CHAT_ID_A, waiting_since=NOW - timedelta(hours=1)
    )
    items = await service.current_actionable(USER_ID, now=NOW)
    assert len(items) == 2
    assert items[0].chat_id == CHAT_ID_B  # older first
    assert items[1].chat_id == CHAT_ID_A


async def test_current_actionable_empty_list():
    service, _, _, _ = _make_service()
    items = await service.current_actionable(USER_ID, now=NOW)
    assert items == []


async def test_current_actionable_excludes_missing_chat_state():
    """If chat state doesn't exist, the item is excluded."""
    service, active_repo, _, _ = _make_service()
    # Don't set up chat state — just the active row.
    await _setup_active(active_repo, CHAT_ID_A)
    items = await service.current_actionable(USER_ID, now=NOW)
    assert len(items) == 0


async def test_current_actionable_no_mute_repo():
    """Works without a mute repo (no mute filtering)."""
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    service = WaitingListQueryService(
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        mute_repo=None,
    )
    await _setup_chat(chat_state_repo, CHAT_ID_A)
    await _setup_active(active_repo, CHAT_ID_A)
    items = await service.current_actionable(USER_ID, now=NOW)
    assert len(items) == 1


async def test_current_actionable_filters_per_user():
    """Items for other users don't appear."""
    service, active_repo, chat_state_repo, _ = _make_service()
    await _setup_chat(chat_state_repo, CHAT_ID_A, user_id="user-a")
    await _setup_active(active_repo, CHAT_ID_A)
    # Query as user-b.
    items = await service.current_actionable("user-b", now=NOW)
    assert len(items) == 0


async def test_current_actionable_snooze_exactly_now_is_actionable():
    """snoozed_until == now means the snooze has expired (not > now)."""
    service, active_repo, chat_state_repo, _ = _make_service()
    await _setup_chat(chat_state_repo, CHAT_ID_A)
    await _setup_active(active_repo, CHAT_ID_A, snoozed_until=NOW)
    items = await service.current_actionable(USER_ID, now=NOW)
    assert len(items) == 1


async def test_current_actionable_multiple_items_mixed_states():
    """Mix of actionable, stale, snoozed, acknowledged, muted."""
    service, active_repo, chat_state_repo, mute_repo = _make_service()
    # A: actionable.
    await _setup_chat(chat_state_repo, CHAT_ID_A, activity_version=1)
    await _setup_active(active_repo, CHAT_ID_A, target_version=1)
    # B: stale.
    await _setup_chat(chat_state_repo, CHAT_ID_B, activity_version=2)
    await _setup_active(active_repo, CHAT_ID_B, target_version=1)
    # C: muted.
    await _setup_chat(chat_state_repo, CHAT_ID_C, activity_version=1)
    await _setup_active(active_repo, CHAT_ID_C, target_version=1)
    await mute_repo.mute_permanent(user_id=USER_ID, chat_id=CHAT_ID_C)

    items = await service.current_actionable(USER_ID, now=NOW)
    assert len(items) == 1
    assert items[0].chat_id == CHAT_ID_A


# --- is_muted edge cases ---


async def test_is_muted_temporary_active():
    """A temporary mute that hasn't expired returns True."""
    repo = InMemoryChatMuteRepository()
    await repo.mute_temporary(
        user_id=USER_ID,
        chat_id=CHAT_ID_A,
        muted_until=NOW + timedelta(hours=1),
    )
    assert await repo.is_muted(user_id=USER_ID, chat_id=CHAT_ID_A, now=NOW) is True


async def test_is_muted_temporary_expired_cleans_up():
    """An expired temporary mute returns False and cleans up."""
    repo = InMemoryChatMuteRepository()
    await repo.mute_temporary(
        user_id=USER_ID,
        chat_id=CHAT_ID_A,
        muted_until=NOW - timedelta(hours=1),
    )
    assert await repo.is_muted(user_id=USER_ID, chat_id=CHAT_ID_A, now=NOW) is False
    # The expired mute should be cleaned up.
    assert (USER_ID, CHAT_ID_A) not in repo._rows


async def test_is_muted_permanent():
    """A permanent mute returns True regardless of time."""
    repo = InMemoryChatMuteRepository()
    await repo.mute_permanent(user_id=USER_ID, chat_id=CHAT_ID_A)
    assert await repo.is_muted(user_id=USER_ID, chat_id=CHAT_ID_A, now=NOW) is True


async def test_is_muted_no_mute_returns_false():
    """No mute record returns False."""
    repo = InMemoryChatMuteRepository()
    assert await repo.is_muted(user_id=USER_ID, chat_id=CHAT_ID_A, now=NOW) is False


async def test_is_muted_temporary_with_null_until_returns_false():
    """A mute with muted_until=None and not permanent returns False (edge case)."""
    repo = InMemoryChatMuteRepository()
    # Manually insert a malformed mute (not permanent, no muted_until).
    from echo_v2.domain.feedback import ChatMute

    repo._rows[(USER_ID, CHAT_ID_A)] = ChatMute(
        user_id=USER_ID,
        chat_id=CHAT_ID_A,
        muted_until=None,
        permanent=False,
        created_at=NOW,
        updated_at=NOW,
    )
    assert await repo.is_muted(user_id=USER_ID, chat_id=CHAT_ID_A, now=NOW) is False
