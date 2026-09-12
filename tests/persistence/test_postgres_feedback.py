"""Postgres integration tests for feedback, action, and mute repositories.

Uses testcontainers — skips locally if Docker is unavailable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.domain.feedback import (
    FeedbackVerdict,
    WaitingForMeActionType,
)

from .conftest import insert_result, insert_user

pytestmark = pytest.mark.asyncio

CHAT_ID = "972508765432@c.us"
NOW = datetime(2026, 9, 12, 6, 0, 0, tzinfo=timezone.utc)


# --- Feedback repo ----------------------------------------------------------


async def test_pg_feedback_record_and_idempotent(feedback_repo, session_factory):
    """Record feedback, then duplicate returns None."""
    user_id = await insert_user(session_factory)

    result1 = await feedback_repo.record(
        user_id=user_id,
        chat_id=CHAT_ID,
        verdict=FeedbackVerdict.FALSE_POSITIVE,
        target_version=1,
        provider_message_id="evt-pg-fb-1",
    )
    assert result1 is not None
    assert result1.verdict == FeedbackVerdict.FALSE_POSITIVE

    # Duplicate — same provider_message_id.
    result2 = await feedback_repo.record(
        user_id=user_id,
        chat_id=CHAT_ID,
        verdict=FeedbackVerdict.FALSE_POSITIVE,
        target_version=1,
        provider_message_id="evt-pg-fb-1",
    )
    assert result2 is None


async def test_pg_feedback_semantic_dedup(feedback_repo, session_factory):
    """First feedback for a result_id wins (semantic dedup)."""
    user_id = await insert_user(session_factory)
    result_id = await insert_result(session_factory, user_id)

    r1 = await feedback_repo.record(
        user_id=user_id,
        chat_id=CHAT_ID,
        verdict=FeedbackVerdict.CORRECT,
        result_id=result_id,
        target_version=1,
        provider_message_id="evt-pg-fb-a",
    )
    assert r1 is not None

    # Different message_id, same result_id → duplicate.
    r2 = await feedback_repo.record(
        user_id=user_id,
        chat_id=CHAT_ID,
        verdict=FeedbackVerdict.FALSE_POSITIVE,
        result_id=result_id,
        target_version=1,
        provider_message_id="evt-pg-fb-b",
    )
    assert r2 is None


async def test_pg_feedback_delete_expired(feedback_repo, session_factory):
    """Delete expired feedback rows."""
    user_id = await insert_user(session_factory)

    # Expired feedback.
    past = datetime.now(timezone.utc) - timedelta(days=1)
    await feedback_repo.record(
        user_id=user_id,
        chat_id=CHAT_ID,
        verdict=FeedbackVerdict.CORRECT,
        provider_message_id="evt-pg-expired",
        expires_at=past,
    )

    # Active feedback (no expiry).
    await feedback_repo.record(
        user_id=user_id,
        chat_id=CHAT_ID,
        verdict=FeedbackVerdict.CORRECT,
        provider_message_id="evt-pg-active",
    )

    now = datetime.now(timezone.utc)
    deleted = await feedback_repo.delete_expired(now=now)
    assert deleted == 1


# --- Action repo -------------------------------------------------------------


async def test_pg_action_record_and_idempotent(action_repo, session_factory):
    """Record an action, then duplicate returns None."""
    user_id = await insert_user(session_factory)

    result1 = await action_repo.record(
        user_id=user_id,
        chat_id=CHAT_ID,
        action_type=WaitingForMeActionType.ACKNOWLEDGE,
        active_id="active-1",
        target_version=1,
        provider_message_id="evt-pg-act-1",
    )
    assert result1 is not None
    assert result1.action_type == WaitingForMeActionType.ACKNOWLEDGE

    # Duplicate.
    result2 = await action_repo.record(
        user_id=user_id,
        chat_id=CHAT_ID,
        action_type=WaitingForMeActionType.ACKNOWLEDGE,
        active_id="active-1",
        target_version=1,
        provider_message_id="evt-pg-act-1",
    )
    assert result2 is None


async def test_pg_action_record_snooze_with_payload(action_repo, session_factory):
    """Record a snooze action with payload."""
    user_id = await insert_user(session_factory)

    snoozed_until = datetime.now(timezone.utc) + timedelta(hours=18)
    result = await action_repo.record(
        user_id=user_id,
        chat_id=CHAT_ID,
        action_type=WaitingForMeActionType.SNOOZE,
        active_id="active-1",
        target_version=1,
        action_payload={"snoozed_until": snoozed_until.isoformat()},
        provider_message_id="evt-pg-snooze-1",
    )
    assert result is not None
    assert result.action_payload is not None
    assert "snoozed_until" in result.action_payload


async def test_pg_action_list_by_session(action_repo, session_factory):
    """list_by_session returns actions matching waiting_list_session_id."""
    user_id = await insert_user(session_factory)
    session_id = "test-session-1"

    # Record two actions for this session.
    await action_repo.record(
        user_id=user_id,
        chat_id=CHAT_ID,
        action_type=WaitingForMeActionType.RESOLVE,
        active_id="active-1",
        target_version=1,
        action_payload={
            "source": "waiting_list_web",
            "waiting_list_session_id": session_id,
        },
        provider_message_id="evt-pg-session-1",
    )
    await action_repo.record(
        user_id=user_id,
        chat_id=CHAT_ID,
        action_type=WaitingForMeActionType.SNOOZE,
        active_id="active-2",
        target_version=1,
        action_payload={
            "source": "waiting_list_web",
            "waiting_list_session_id": session_id,
        },
        provider_message_id="evt-pg-session-2",
    )
    # Record an action for a different session.
    await action_repo.record(
        user_id=user_id,
        chat_id=CHAT_ID,
        action_type=WaitingForMeActionType.RESOLVE,
        active_id="active-3",
        target_version=1,
        action_payload={
            "source": "waiting_list_web",
            "waiting_list_session_id": "other-session",
        },
        provider_message_id="evt-pg-session-3",
    )

    actions = await action_repo.list_by_session(
        user_id=user_id, session_id=session_id
    )
    assert len(actions) == 2
    for a in actions:
        assert a.action_payload["waiting_list_session_id"] == session_id


# --- Mute repo ---------------------------------------------------------------


async def test_pg_mute_temporary(mute_repo, session_factory):
    """Temporary mute is active until expiry."""
    user_id = await insert_user(session_factory)

    future = datetime.now(timezone.utc) + timedelta(hours=24)
    await mute_repo.mute_temporary(
        user_id=user_id, chat_id=CHAT_ID, muted_until=future
    )

    assert await mute_repo.is_muted(
        user_id=user_id, chat_id=CHAT_ID, now=datetime.now(timezone.utc)
    ) is True

    after = future + timedelta(hours=1)
    assert await mute_repo.is_muted(user_id=user_id, chat_id=CHAT_ID, now=after) is False


async def test_pg_mute_permanent(mute_repo, session_factory):
    """Permanent mute never expires."""
    user_id = await insert_user(session_factory)

    await mute_repo.mute_permanent(user_id=user_id, chat_id=CHAT_ID)

    far_future = datetime.now(timezone.utc) + timedelta(days=365 * 10)
    assert await mute_repo.is_muted(
        user_id=user_id, chat_id=CHAT_ID, now=datetime.now(timezone.utc)
    ) is True
    assert await mute_repo.is_muted(user_id=user_id, chat_id=CHAT_ID, now=far_future) is True


async def test_pg_mute_get(mute_repo, session_factory):
    """get returns the mute record."""
    user_id = await insert_user(session_factory)

    assert await mute_repo.get(user_id=user_id, chat_id=CHAT_ID) is None

    await mute_repo.mute_permanent(user_id=user_id, chat_id=CHAT_ID)
    mute = await mute_repo.get(user_id=user_id, chat_id=CHAT_ID)
    assert mute is not None
    assert mute.permanent is True


async def test_pg_mute_unmute(mute_repo, session_factory):
    """unmute removes the mute."""
    user_id = await insert_user(session_factory)

    await mute_repo.mute_permanent(user_id=user_id, chat_id=CHAT_ID)
    assert await mute_repo.unmute(user_id=user_id, chat_id=CHAT_ID) is True
    assert await mute_repo.get(user_id=user_id, chat_id=CHAT_ID) is None
    assert await mute_repo.unmute(user_id=user_id, chat_id=CHAT_ID) is False


async def test_pg_mute_temporary_upsert(mute_repo, session_factory):
    """mute_temporary updates an existing mute."""
    user_id = await insert_user(session_factory)

    soon = datetime.now(timezone.utc) + timedelta(hours=1)
    later = datetime.now(timezone.utc) + timedelta(hours=48)

    await mute_repo.mute_temporary(
        user_id=user_id, chat_id=CHAT_ID, muted_until=soon
    )
    await mute_repo.mute_temporary(
        user_id=user_id, chat_id=CHAT_ID, muted_until=later
    )

    mute = await mute_repo.get(user_id=user_id, chat_id=CHAT_ID)
    assert mute is not None
    assert mute.muted_until == later


async def test_pg_mute_permanent_upsert_over_temporary(mute_repo, session_factory):
    """mute_permanent overwrites a temporary mute."""
    user_id = await insert_user(session_factory)

    soon = datetime.now(timezone.utc) + timedelta(hours=1)
    await mute_repo.mute_temporary(
        user_id=user_id, chat_id=CHAT_ID, muted_until=soon
    )
    await mute_repo.mute_permanent(user_id=user_id, chat_id=CHAT_ID)

    mute = await mute_repo.get(user_id=user_id, chat_id=CHAT_ID)
    assert mute is not None
    assert mute.permanent is True
    assert mute.muted_until is None


async def test_pg_mute_is_muted_no_row(mute_repo, session_factory):
    """is_muted returns False when no mute row exists."""
    user_id = await insert_user(session_factory)
    assert await mute_repo.is_muted(
        user_id=user_id, chat_id=CHAT_ID, now=datetime.now(timezone.utc)
    ) is False


async def test_pg_mute_is_muted_cleans_expired(mute_repo, session_factory):
    """is_muted returns False and deletes an expired temporary mute."""
    user_id = await insert_user(session_factory)

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    await mute_repo.mute_temporary(
        user_id=user_id, chat_id=CHAT_ID, muted_until=past
    )

    assert await mute_repo.is_muted(
        user_id=user_id, chat_id=CHAT_ID, now=datetime.now(timezone.utc)
    ) is False

    # Row should be cleaned up.
    assert await mute_repo.get(user_id=user_id, chat_id=CHAT_ID) is None
