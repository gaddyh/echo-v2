"""Tests for the canonical WaitingForMeView and ContactNameResolver."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.domain.chat import Message
from echo_v2.domain.waiting_for_me import (
    WaitingForMeDecision,
    WaitingForMeResult,
)
from echo_v2.persistence.chat_repositories import (
    InMemoryChatStateRepository,
    InMemoryMessageRepository,
    InMemoryWaitingForMeActiveRepository,
    InMemoryWaitingForMeResultRepository,
)
from echo_v2.persistence.contacts import ContactRecord, InMemoryContactRepository
from echo_v2.persistence.feedback_repositories import InMemoryChatMuteRepository
from echo_v2.ports.whatsapp import MessageDirection
from echo_v2.services.waiting_for_me_view import (
    ContactNameResolver,
    WaitingForMeView,
    phone_from_chat_id,
    truncate_preview,
)
from echo_v2.services.waiting_list_query import WaitingListQueryService

NOW = datetime(2026, 9, 12, 6, 0, 0, tzinfo=timezone.utc)
USER_ID = "user-1"
CHAT_ID = "972508765432@c.us"
OTHER_CHAT_ID = "972509876543@c.us"
RESULT_ID = "result-1"


# --- phone_from_chat_id (sync) ----------------------------------------------


def test_phone_from_chat_id_extracts_phone():
    assert phone_from_chat_id("972501234567@c.us") == "972501234567"


def test_phone_from_chat_id_no_suffix():
    assert phone_from_chat_id("972501234567") == "972501234567"


def test_phone_from_chat_id_group():
    assert phone_from_chat_id("group-123@g.us") == "group-123"


# --- truncate_preview (sync) -------------------------------------------------


def test_truncate_preview_none():
    assert truncate_preview(None) is None


def test_truncate_preview_empty():
    assert truncate_preview("") is None


def test_truncate_preview_short():
    assert truncate_preview("hello") == "hello"


def test_truncate_preview_exact_120():
    text = "x" * 120
    assert truncate_preview(text) == text


def test_truncate_preview_long_adds_ellipsis():
    text = "x" * 300
    result = truncate_preview(text)
    assert result is not None
    assert len(result) == 121  # 120 + ellipsis
    assert result.endswith("…")


# --- helpers + ContactNameResolver.resolve_name -----------------------------
# asyncio_mode=auto in pyproject.toml handles the asyncio mark for async tests.


def _make_repos():
    return (
        InMemoryWaitingForMeActiveRepository(),
        InMemoryChatStateRepository(),
        InMemoryMessageRepository(),
        InMemoryContactRepository(),
        InMemoryChatMuteRepository(),
        InMemoryWaitingForMeResultRepository(),
    )


def _make_resolver(chat_state_repo, message_repo, contact_repo):
    return ContactNameResolver(
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
    )


def _make_query_service(
    active_repo, chat_state_repo, message_repo, contact_repo, mute_repo, result_repo
):
    return WaitingListQueryService(
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        mute_repo=mute_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        result_repo=result_repo,
    )


async def _setup_chat_and_active(
    active_repo, chat_state_repo, *, chat_id=CHAT_ID, target_version=1
):
    await chat_state_repo.upsert_on_message(
        user_id=USER_ID,
        chat_id=chat_id,
        direction=MessageDirection.INBOUND,
        observed_at=NOW,
        next_analysis_at=None,
    )
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id=chat_id,
        target_version=target_version,
        result_id=RESULT_ID,
        waiting_since=NOW,
    )


async def test_resolve_name_from_chat_state():
    _active_repo, chat_state_repo, message_repo, contact_repo, _, _ = _make_repos()
    resolver = _make_resolver(chat_state_repo, message_repo, contact_repo)
    await chat_state_repo.upsert_on_message(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        direction=MessageDirection.INBOUND,
        observed_at=NOW,
        next_analysis_at=None,
    )
    chat = await chat_state_repo.get(USER_ID, CHAT_ID)
    chat_with_name = replace(chat, chat_name="יוסי")
    chat_state_repo._chats[(USER_ID, CHAT_ID)] = chat_with_name

    name = await resolver.resolve_name(USER_ID, CHAT_ID)
    assert name == "יוסי"


async def test_resolve_name_falls_back_to_contact():
    _active_repo, chat_state_repo, message_repo, contact_repo, _, _ = _make_repos()
    resolver = _make_resolver(chat_state_repo, message_repo, contact_repo)
    await chat_state_repo.upsert_on_message(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        direction=MessageDirection.INBOUND,
        observed_at=NOW,
        next_analysis_at=None,
    )
    await contact_repo.save(
        ContactRecord(
            user_id=USER_ID,
            phone_number=phone_from_chat_id(CHAT_ID),
            display_name="דנה",
        )
    )

    name = await resolver.resolve_name(USER_ID, CHAT_ID)
    assert name == "דנה"


async def test_resolve_name_falls_back_to_message_chat_name():
    _active_repo, chat_state_repo, message_repo, contact_repo, _, _ = _make_repos()
    resolver = _make_resolver(chat_state_repo, message_repo, contact_repo)
    await chat_state_repo.upsert_on_message(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        direction=MessageDirection.INBOUND,
        observed_at=NOW,
        next_analysis_at=None,
    )
    await message_repo.save(
        Message(
            id="msg-1",
            user_id=USER_ID,
            connection_id="conn-1",
            chat_id=CHAT_ID,
            provider_message_id="pm-1",
            direction=MessageDirection.INBOUND,
            sender_id=None,
            text="היי",
            chat_name="שרה",
            sender_name="שרה",
            timestamp=NOW,
        )
    )

    name = await resolver.resolve_name(USER_ID, CHAT_ID)
    assert name == "שרה"


async def test_resolve_name_falls_back_to_phone_when_nothing_else():
    _active_repo, chat_state_repo, message_repo, contact_repo, _, _ = _make_repos()
    resolver = _make_resolver(chat_state_repo, message_repo, contact_repo)
    await chat_state_repo.upsert_on_message(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        direction=MessageDirection.INBOUND,
        observed_at=NOW,
        next_analysis_at=None,
    )

    # No chat_name, no contact, no message → resolver returns None.
    # Caller falls back to phone_from_chat_id.
    name = await resolver.resolve_name(USER_ID, CHAT_ID)
    assert name is None
    assert phone_from_chat_id(CHAT_ID) == "972508765432"


async def test_resolve_name_uses_pre_fetched_contact():
    _active_repo, chat_state_repo, message_repo, contact_repo, _, _ = _make_repos()
    resolver = _make_resolver(chat_state_repo, message_repo, contact_repo)
    await chat_state_repo.upsert_on_message(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        direction=MessageDirection.INBOUND,
        observed_at=NOW,
        next_analysis_at=None,
    )
    contact = ContactRecord(
        user_id=USER_ID,
        phone_number=phone_from_chat_id(CHAT_ID),
        display_name="מראש",
    )

    name = await resolver.resolve_name(USER_ID, CHAT_ID, contact=contact)
    assert name == "מראש"


# --- WaitingListQueryService.current_views ----------------------------------


async def test_current_views_returns_empty_when_no_active():
    repos = _make_repos()
    active_repo, chat_state_repo, message_repo, contact_repo, mute_repo, result_repo = repos
    query = _make_query_service(
        active_repo, chat_state_repo, message_repo, contact_repo, mute_repo, result_repo
    )
    views = await query.current_views(USER_ID, now=NOW)
    assert views == []


async def test_current_views_returns_one_view():
    repos = _make_repos()
    active_repo, chat_state_repo, message_repo, contact_repo, mute_repo, result_repo = repos
    query = _make_query_service(
        active_repo, chat_state_repo, message_repo, contact_repo, mute_repo, result_repo
    )
    await _setup_chat_and_active(active_repo, chat_state_repo)

    views = await query.current_views(USER_ID, now=NOW)
    assert len(views) == 1
    view = views[0]
    assert isinstance(view, WaitingForMeView)
    assert view.owner == USER_ID
    assert view.version == 1
    assert view.waiting_since == NOW
    assert view.is_starred is False
    assert view.color_label is None
    assert view.tags == []


async def test_current_views_includes_contact_name():
    repos = _make_repos()
    active_repo, chat_state_repo, message_repo, contact_repo, mute_repo, result_repo = repos
    query = _make_query_service(
        active_repo, chat_state_repo, message_repo, contact_repo, mute_repo, result_repo
    )
    await _setup_chat_and_active(active_repo, chat_state_repo)
    await contact_repo.save(
        ContactRecord(
            user_id=USER_ID,
            phone_number=phone_from_chat_id(CHAT_ID),
            display_name="דנה",
        )
    )

    views = await query.current_views(USER_ID, now=NOW)
    assert views[0].contact_name == "דנה"


async def test_current_views_includes_summary():
    repos = _make_repos()
    active_repo, chat_state_repo, message_repo, contact_repo, mute_repo, result_repo = repos
    query = _make_query_service(
        active_repo, chat_state_repo, message_repo, contact_repo, mute_repo, result_repo
    )
    result_id = await result_repo.save(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        result=WaitingForMeResult(
            decision=WaitingForMeDecision.WAITING_FOR_ME,
            confidence=0.9,
            reason="Direct question.",
            summary="רוצה לתאם פגישה.",
            target_version=1,
        ),
    )
    await _setup_chat_and_active(active_repo, chat_state_repo)
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        target_version=1,
        result_id=result_id,
        waiting_since=NOW,
    )

    views = await query.current_views(USER_ID, now=NOW)
    assert views[0].summary == "רוצה לתאם פגישה."


async def test_current_views_includes_last_message_preview():
    repos = _make_repos()
    active_repo, chat_state_repo, message_repo, contact_repo, mute_repo, result_repo = repos
    query = _make_query_service(
        active_repo, chat_state_repo, message_repo, contact_repo, mute_repo, result_repo
    )
    await _setup_chat_and_active(active_repo, chat_state_repo)
    await message_repo.save(
        Message(
            id="msg-1",
            user_id=USER_ID,
            connection_id="conn-1",
            chat_id=CHAT_ID,
            provider_message_id="pm-1",
            direction=MessageDirection.INBOUND,
            sender_id=None,
            text="היי מה קורה",
            timestamp=NOW,
        )
    )

    views = await query.current_views(USER_ID, now=NOW)
    assert views[0].last_message == "היי מה קורה"


async def test_current_views_truncates_long_preview():
    repos = _make_repos()
    active_repo, chat_state_repo, message_repo, contact_repo, mute_repo, result_repo = repos
    query = _make_query_service(
        active_repo, chat_state_repo, message_repo, contact_repo, mute_repo, result_repo
    )
    await _setup_chat_and_active(active_repo, chat_state_repo)
    long_text = "x" * 300
    await message_repo.save(
        Message(
            id="msg-1",
            user_id=USER_ID,
            connection_id="conn-1",
            chat_id=CHAT_ID,
            provider_message_id="pm-1",
            direction=MessageDirection.INBOUND,
            sender_id=None,
            text=long_text,
            timestamp=NOW,
        )
    )

    views = await query.current_views(USER_ID, now=NOW)
    preview = views[0].last_message
    assert preview is not None
    assert len(preview) == 121
    assert preview.endswith("…")


async def test_current_views_excludes_snoozed():
    repos = _make_repos()
    active_repo, chat_state_repo, message_repo, contact_repo, mute_repo, result_repo = repos
    query = _make_query_service(
        active_repo, chat_state_repo, message_repo, contact_repo, mute_repo, result_repo
    )
    await _setup_chat_and_active(active_repo, chat_state_repo)
    future = NOW + timedelta(hours=10)
    await active_repo.snooze(
        user_id=USER_ID, chat_id=CHAT_ID, snoozed_until=future
    )

    views = await query.current_views(USER_ID, now=NOW)
    assert views == []


async def test_current_views_excludes_muted():
    repos = _make_repos()
    active_repo, chat_state_repo, message_repo, contact_repo, mute_repo, result_repo = repos
    query = _make_query_service(
        active_repo, chat_state_repo, message_repo, contact_repo, mute_repo, result_repo
    )
    await _setup_chat_and_active(active_repo, chat_state_repo)
    await mute_repo.mute_permanent(user_id=USER_ID, chat_id=CHAT_ID)

    views = await query.current_views(USER_ID, now=NOW)
    assert views == []


async def test_current_views_excludes_stale():
    repos = _make_repos()
    active_repo, chat_state_repo, message_repo, contact_repo, mute_repo, result_repo = repos
    query = _make_query_service(
        active_repo, chat_state_repo, message_repo, contact_repo, mute_repo, result_repo
    )
    # Chat at version 2, active at version 1 → stale.
    await _setup_chat_and_active(active_repo, chat_state_repo, target_version=1)
    # Bump chat to version 2.
    await chat_state_repo.upsert_on_message(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        direction=MessageDirection.INBOUND,
        observed_at=NOW,
        next_analysis_at=None,
    )

    views = await query.current_views(USER_ID, now=NOW)
    assert views == []


async def test_current_views_starred_first():
    repos = _make_repos()
    active_repo, chat_state_repo, message_repo, contact_repo, mute_repo, result_repo = repos
    query = _make_query_service(
        active_repo, chat_state_repo, message_repo, contact_repo, mute_repo, result_repo
    )
    # Two chats: starred (newer) and non-starred (older).
    await _setup_chat_and_active(active_repo, chat_state_repo, chat_id=OTHER_CHAT_ID)
    await _setup_chat_and_active(active_repo, chat_state_repo, chat_id=CHAT_ID)
    # Star CHAT_ID.
    await contact_repo.set_starred(
        user_id=USER_ID,
        phone=phone_from_chat_id(CHAT_ID),
        is_starred=True,
        display_name="starred",
    )

    views = await query.current_views(USER_ID, now=NOW)
    assert len(views) == 2
    assert views[0].is_starred is True
    assert views[1].is_starred is False


async def test_current_views_oldest_first_within_starred():
    repos = _make_repos()
    active_repo, chat_state_repo, message_repo, contact_repo, mute_repo, result_repo = repos
    query = _make_query_service(
        active_repo, chat_state_repo, message_repo, contact_repo, mute_repo, result_repo
    )
    old_chat = "972500000001@c.us"
    new_chat = "972500000002@c.us"
    await chat_state_repo.upsert_on_message(
        user_id=USER_ID,
        chat_id=old_chat,
        direction=MessageDirection.INBOUND,
        observed_at=NOW,
        next_analysis_at=None,
    )
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id=old_chat,
        target_version=1,
        result_id=RESULT_ID,
        waiting_since=NOW,
    )
    await chat_state_repo.upsert_on_message(
        user_id=USER_ID,
        chat_id=new_chat,
        direction=MessageDirection.INBOUND,
        observed_at=NOW,
        next_analysis_at=None,
    )
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id=new_chat,
        target_version=1,
        result_id=RESULT_ID,
        waiting_since=NOW + timedelta(hours=1),
    )
    # Star both.
    await contact_repo.set_starred(
        user_id=USER_ID,
        phone=phone_from_chat_id(old_chat),
        is_starred=True,
        display_name="old",
    )
    await contact_repo.set_starred(
        user_id=USER_ID,
        phone=phone_from_chat_id(new_chat),
        is_starred=True,
        display_name="new",
    )

    views = await query.current_views(USER_ID, now=NOW)
    assert len(views) == 2
    assert views[0].is_starred is True
    assert views[1].is_starred is True
    # Older first.
    assert views[0].waiting_since == NOW
    assert views[1].waiting_since == NOW + timedelta(hours=1)


async def test_current_views_includes_color_label_and_tags():
    repos = _make_repos()
    active_repo, chat_state_repo, message_repo, contact_repo, mute_repo, result_repo = repos
    query = _make_query_service(
        active_repo, chat_state_repo, message_repo, contact_repo, mute_repo, result_repo
    )
    await _setup_chat_and_active(active_repo, chat_state_repo)
    await contact_repo.set_label(
        user_id=USER_ID,
        phone=phone_from_chat_id(CHAT_ID),
        color_label="blue",
        display_name="test",
    )
    await contact_repo.set_tags(
        user_id=USER_ID,
        phone=phone_from_chat_id(CHAT_ID),
        tags=["work", "family"],
        display_name="test",
    )

    views = await query.current_views(USER_ID, now=NOW)
    view = views[0]
    assert view.color_label == "blue"
    assert view.tags == ["work", "family"]


async def test_current_views_raises_without_message_repo():
    repos = _make_repos()
    active_repo, chat_state_repo, _message_repo, _contact_repo, mute_repo, _result_repo = repos
    query = WaitingListQueryService(
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        mute_repo=mute_repo,
    )
    with pytest.raises(RuntimeError, match="current_views requires"):
        await query.current_views(USER_ID, now=NOW)
