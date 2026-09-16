"""Postgres integration tests for feedback, action, and mute repositories.

Uses testcontainers — skips locally if Docker is unavailable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from echo_v2.domain.feedback import (
    ActionCommandResult,
    FeedbackVerdict,
    HandlingOutcome,
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


# --- Atomic action methods --------------------------------------------


async def _insert_active(
    session_factory,
    *,
    user_id: str,
    chat_id: str = CHAT_ID,
    target_version: int = 1,
    result_id: str | None = None,
) -> str:
    """Insert a waiting_for_me_active row and return its id (UUID string)."""
    if result_id is None:
        result_id = await insert_result(session_factory, user_id, chat_id, target_version)
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO waiting_for_me_active "
                "(id, user_id, chat_id, target_version, result_id, waiting_since) "
                "VALUES (gen_random_uuid(), :uid, :cid, :tv, :rid, :now)"
            ),
            {"uid": user_id, "cid": chat_id, "tv": target_version, "rid": result_id, "now": NOW},
        )
        await session.commit()
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT id FROM waiting_for_me_active "
                "WHERE user_id = :uid AND chat_id = :cid AND target_version = :tv"
            ),
            {"uid": user_id, "cid": chat_id, "tv": target_version},
        )
        return str(result.scalar_one())


async def _active_exists(session_factory, active_id: str) -> bool:
    async with session_factory() as session:
        result = await session.execute(
            text("SELECT 1 FROM waiting_for_me_active WHERE id = :aid"),
            {"aid": active_id},
        )
        return result.first() is not None


async def _get_snoozed_until(session_factory, active_id: str):
    async with session_factory() as session:
        result = await session.execute(
            text("SELECT snoozed_until FROM waiting_for_me_active WHERE id = :aid"),
            {"aid": active_id},
        )
        return result.scalar_one_or_none()


async def _count_active_for_chat(session_factory, *, user_id: str, chat_id: str) -> int:
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT count(*) FROM waiting_for_me_active "
                "WHERE user_id = :uid AND chat_id = :cid"
            ),
            {"uid": user_id, "cid": chat_id},
        )
        return int(result.scalar_one())


# --- resolve_and_delete_by_version ------------------------------------


async def test_pg_resolve_by_version_applied(action_repo, session_factory):
    """APPLIED: active exists with matching version → deleted."""
    user_id = await insert_user(session_factory)
    result_id = await insert_result(session_factory, user_id)
    active_id = await _insert_active(
        session_factory, user_id=user_id, result_id=result_id, target_version=1
    )

    res = await action_repo.resolve_and_delete_by_version(
        user_id=user_id,
        active_id=active_id,
        target_version=1,
        provider_message_id="evt-pg-resolve-1",
    )
    assert res.outcome == HandlingOutcome.APPLIED
    assert res.chat_id == CHAT_ID
    assert res.result_id == result_id

    # Active row actually deleted.
    assert await _active_exists(session_factory, active_id) is False


async def test_pg_resolve_by_version_not_found(action_repo, session_factory):
    """NOT_FOUND: active doesn't exist."""
    user_id = await insert_user(session_factory)

    res = await action_repo.resolve_and_delete_by_version(
        user_id=user_id,
        active_id="00000000-0000-0000-0000-000000000000",
        target_version=1,
        provider_message_id="evt-pg-resolve-nf",
    )
    assert res.outcome == HandlingOutcome.NOT_FOUND
    assert res.chat_id is None
    assert res.result_id is None


async def test_pg_resolve_by_version_stale(action_repo, session_factory):
    """STALE: active exists but with different target_version."""
    user_id = await insert_user(session_factory)
    active_id = await _insert_active(
        session_factory, user_id=user_id, target_version=2
    )

    res = await action_repo.resolve_and_delete_by_version(
        user_id=user_id,
        active_id=active_id,
        target_version=1,
        provider_message_id="evt-pg-resolve-stale",
    )
    assert res.outcome == HandlingOutcome.STALE
    # Active row NOT deleted.
    assert await _active_exists(session_factory, active_id) is True


async def test_pg_resolve_by_version_duplicate(action_repo, session_factory):
    """DUPLICATE: same provider_message_id twice → second returns DUPLICATE."""
    user_id = await insert_user(session_factory)
    result_id = await insert_result(session_factory, user_id)
    active_id = await _insert_active(
        session_factory, user_id=user_id, result_id=result_id, target_version=1
    )

    r1 = await action_repo.resolve_and_delete_by_version(
        user_id=user_id,
        active_id=active_id,
        target_version=1,
        provider_message_id="evt-pg-resolve-dup",
    )
    assert r1.outcome == HandlingOutcome.APPLIED

    r2 = await action_repo.resolve_and_delete_by_version(
        user_id=user_id,
        active_id=active_id,
        target_version=1,
        provider_message_id="evt-pg-resolve-dup",
    )
    assert r2.outcome == HandlingOutcome.DUPLICATE


# --- resolve_and_delete_by_chat ---------------------------------------


async def test_pg_resolve_by_chat_applied(action_repo, session_factory):
    """APPLIED: active exists with matching version → deleted."""
    user_id = await insert_user(session_factory)
    result_id = await insert_result(session_factory, user_id)
    active_id = await _insert_active(
        session_factory, user_id=user_id, result_id=result_id, target_version=1
    )

    res = await action_repo.resolve_and_delete_by_chat(
        user_id=user_id,
        active_id=active_id,
        target_version=1,
        provider_message_id="evt-pg-reschat-1",
    )
    assert res.outcome == HandlingOutcome.APPLIED
    assert res.chat_id == CHAT_ID
    assert res.result_id == result_id

    assert await _count_active_for_chat(
        session_factory, user_id=user_id, chat_id=CHAT_ID
    ) == 0


async def test_pg_resolve_by_chat_not_found(action_repo, session_factory):
    """NOT_FOUND: active doesn't exist."""
    user_id = await insert_user(session_factory)

    res = await action_repo.resolve_and_delete_by_chat(
        user_id=user_id,
        active_id="00000000-0000-0000-0000-000000000000",
        target_version=1,
        provider_message_id="evt-pg-reschat-nf",
    )
    assert res.outcome == HandlingOutcome.NOT_FOUND


async def test_pg_resolve_by_chat_stale_version(action_repo, session_factory):
    """STALE: active exists but with different target_version."""
    user_id = await insert_user(session_factory)
    active_id = await _insert_active(
        session_factory, user_id=user_id, target_version=2
    )

    res = await action_repo.resolve_and_delete_by_chat(
        user_id=user_id,
        active_id=active_id,
        target_version=1,
        provider_message_id="evt-pg-reschat-stale",
    )
    assert res.outcome == HandlingOutcome.STALE
    assert await _active_exists(session_factory, active_id) is True


async def test_pg_resolve_by_chat_stale_user(action_repo, session_factory):
    """STALE: active exists but belongs to a different user_id."""
    user_id = await insert_user(session_factory)
    other_user_id = await insert_user(session_factory, phone="+972546610654")
    active_id = await _insert_active(
        session_factory, user_id=other_user_id, target_version=1
    )

    res = await action_repo.resolve_and_delete_by_chat(
        user_id=user_id,
        active_id=active_id,
        target_version=1,
        provider_message_id="evt-pg-reschat-stale-user",
    )
    assert res.outcome == HandlingOutcome.STALE
    assert await _active_exists(session_factory, active_id) is True


async def test_pg_resolve_by_chat_duplicate(action_repo, session_factory):
    """DUPLICATE: same provider_message_id twice."""
    user_id = await insert_user(session_factory)
    result_id = await insert_result(session_factory, user_id)
    active_id = await _insert_active(
        session_factory, user_id=user_id, result_id=result_id, target_version=1
    )

    r1 = await action_repo.resolve_and_delete_by_chat(
        user_id=user_id,
        active_id=active_id,
        target_version=1,
        provider_message_id="evt-pg-reschat-dup",
    )
    assert r1.outcome == HandlingOutcome.APPLIED

    r2 = await action_repo.resolve_and_delete_by_chat(
        user_id=user_id,
        active_id=active_id,
        target_version=1,
        provider_message_id="evt-pg-reschat-dup",
    )
    assert r2.outcome == HandlingOutcome.DUPLICATE


async def test_pg_resolve_by_chat_deletes_by_chat_scope(action_repo, session_factory):
    """APPLIED deletes the active row for the chat; other chats are untouched.

    The schema enforces one active row per (user_id, chat_id), so we verify
    the delete is scoped by chat_id rather than affecting unrelated chats.
    """
    user_id = await insert_user(session_factory)
    other_chat = "972508765433@c.us"

    rid1 = await insert_result(session_factory, user_id, CHAT_ID, 1)
    rid2 = await insert_result(session_factory, user_id, other_chat, 1)
    active_id1 = await _insert_active(
        session_factory, user_id=user_id, chat_id=CHAT_ID, result_id=rid1, target_version=1
    )
    active_id2 = await _insert_active(
        session_factory,
        user_id=user_id,
        chat_id=other_chat,
        result_id=rid2,
        target_version=1,
    )

    res = await action_repo.resolve_and_delete_by_chat(
        user_id=user_id,
        active_id=active_id1,
        target_version=1,
        provider_message_id="evt-pg-reschat-scope",
    )
    assert res.outcome == HandlingOutcome.APPLIED
    assert res.chat_id == CHAT_ID

    # The resolved chat's active row is gone; the other chat is untouched.
    assert await _active_exists(session_factory, active_id1) is False
    assert await _active_exists(session_factory, active_id2) is True


# --- snooze_active ----------------------------------------------------


async def test_pg_snooze_active_applied(action_repo, session_factory):
    """APPLIED: active exists with matching version → snoozed_until updated."""
    user_id = await insert_user(session_factory)
    active_id = await _insert_active(
        session_factory, user_id=user_id, target_version=1
    )

    snoozed_until = NOW + timedelta(hours=18)
    res = await action_repo.snooze_active(
        user_id=user_id,
        active_id=active_id,
        target_version=1,
        snoozed_until=snoozed_until,
        provider_message_id="evt-pg-snooze-1",
    )
    assert res.outcome == HandlingOutcome.APPLIED
    assert res.chat_id == CHAT_ID
    assert res.result_id is None

    # Verify snoozed_until is actually set in the DB.
    db_val = await _get_snoozed_until(session_factory, active_id)
    assert db_val is not None
    # Compare ignoring microseconds (DB timestamp precision).
    assert db_val.replace(microsecond=0) == snoozed_until.replace(microsecond=0)


async def test_pg_snooze_active_not_found(action_repo, session_factory):
    """NOT_FOUND: active doesn't exist."""
    user_id = await insert_user(session_factory)

    res = await action_repo.snooze_active(
        user_id=user_id,
        active_id="00000000-0000-0000-0000-000000000000",
        target_version=1,
        snoozed_until=NOW + timedelta(hours=1),
        provider_message_id="evt-pg-snooze-nf",
    )
    assert res.outcome == HandlingOutcome.NOT_FOUND


async def test_pg_snooze_active_stale(action_repo, session_factory):
    """STALE: active exists but with different version."""
    user_id = await insert_user(session_factory)
    active_id = await _insert_active(
        session_factory, user_id=user_id, target_version=2
    )

    res = await action_repo.snooze_active(
        user_id=user_id,
        active_id=active_id,
        target_version=1,
        snoozed_until=NOW + timedelta(hours=1),
        provider_message_id="evt-pg-snooze-stale",
    )
    assert res.outcome == HandlingOutcome.STALE
    # snoozed_until NOT updated.
    assert await _get_snoozed_until(session_factory, active_id) is None


async def test_pg_snooze_active_duplicate(action_repo, session_factory):
    """DUPLICATE: same provider_message_id twice."""
    user_id = await insert_user(session_factory)
    active_id = await _insert_active(
        session_factory, user_id=user_id, target_version=1
    )

    r1 = await action_repo.snooze_active(
        user_id=user_id,
        active_id=active_id,
        target_version=1,
        snoozed_until=NOW + timedelta(hours=1),
        provider_message_id="evt-pg-snooze-dup",
    )
    assert r1.outcome == HandlingOutcome.APPLIED

    r2 = await action_repo.snooze_active(
        user_id=user_id,
        active_id=active_id,
        target_version=1,
        snoozed_until=NOW + timedelta(hours=2),
        provider_message_id="evt-pg-snooze-dup",
    )
    assert r2.outcome == HandlingOutcome.DUPLICATE


# --- mute_chat_atomic -------------------------------------------------


async def test_pg_mute_chat_atomic_permanent(action_repo, mute_repo, session_factory):
    """APPLIED permanent: creates permanent mute."""
    user_id = await insert_user(session_factory)

    res = await action_repo.mute_chat_atomic(
        user_id=user_id,
        chat_id=CHAT_ID,
        permanent=True,
        muted_until=None,
        provider_message_id="evt-pg-mute-perm",
    )
    assert res.outcome == HandlingOutcome.APPLIED

    mute = await mute_repo.get(user_id=user_id, chat_id=CHAT_ID)
    assert mute is not None
    assert mute.permanent is True
    assert mute.muted_until is None


async def test_pg_mute_chat_atomic_temporary(action_repo, mute_repo, session_factory):
    """APPLIED temporary: creates temporary mute with muted_until."""
    user_id = await insert_user(session_factory)
    muted_until = NOW + timedelta(hours=24)

    res = await action_repo.mute_chat_atomic(
        user_id=user_id,
        chat_id=CHAT_ID,
        permanent=False,
        muted_until=muted_until,
        provider_message_id="evt-pg-mute-temp",
    )
    assert res.outcome == HandlingOutcome.APPLIED

    mute = await mute_repo.get(user_id=user_id, chat_id=CHAT_ID)
    assert mute is not None
    assert mute.permanent is False
    assert mute.muted_until is not None
    assert mute.muted_until.replace(microsecond=0) == muted_until.replace(microsecond=0)


async def test_pg_mute_chat_atomic_duplicate(action_repo, mute_repo, session_factory):
    """DUPLICATE: same provider_message_id twice."""
    user_id = await insert_user(session_factory)

    r1 = await action_repo.mute_chat_atomic(
        user_id=user_id,
        chat_id=CHAT_ID,
        permanent=True,
        muted_until=None,
        provider_message_id="evt-pg-mute-dup",
    )
    assert r1.outcome == HandlingOutcome.APPLIED

    r2 = await action_repo.mute_chat_atomic(
        user_id=user_id,
        chat_id=CHAT_ID,
        permanent=False,
        muted_until=NOW + timedelta(hours=1),
        provider_message_id="evt-pg-mute-dup",
    )
    assert r2.outcome == HandlingOutcome.DUPLICATE
    # First mute (permanent) preserved.
    mute = await mute_repo.get(user_id=user_id, chat_id=CHAT_ID)
    assert mute is not None
    assert mute.permanent is True


async def test_pg_mute_chat_atomic_upsert_perm_then_temp(
    action_repo, mute_repo, session_factory
):
    """Upsert: permanent then temporary → mute should be temporary."""
    user_id = await insert_user(session_factory)

    await action_repo.mute_chat_atomic(
        user_id=user_id,
        chat_id=CHAT_ID,
        permanent=True,
        muted_until=None,
        provider_message_id="evt-pg-mute-upsert-1",
    )
    muted_until = NOW + timedelta(hours=12)
    await action_repo.mute_chat_atomic(
        user_id=user_id,
        chat_id=CHAT_ID,
        permanent=False,
        muted_until=muted_until,
        provider_message_id="evt-pg-mute-upsert-2",
    )

    mute = await mute_repo.get(user_id=user_id, chat_id=CHAT_ID)
    assert mute is not None
    assert mute.permanent is False
    assert mute.muted_until is not None
    assert mute.muted_until.replace(microsecond=0) == muted_until.replace(microsecond=0)


# --- unmute_chat_atomic -----------------------------------------------


async def test_pg_unmute_chat_atomic_applied(action_repo, mute_repo, session_factory):
    """APPLIED: mute exists → deleted."""
    user_id = await insert_user(session_factory)
    await mute_repo.mute_permanent(user_id=user_id, chat_id=CHAT_ID)
    assert await mute_repo.get(user_id=user_id, chat_id=CHAT_ID) is not None

    res = await action_repo.unmute_chat_atomic(
        user_id=user_id,
        chat_id=CHAT_ID,
        provider_message_id="evt-pg-unmute-1",
    )
    assert res.outcome == HandlingOutcome.APPLIED
    assert await mute_repo.get(user_id=user_id, chat_id=CHAT_ID) is None


async def test_pg_unmute_chat_atomic_applied_no_mute(
    action_repo, mute_repo, session_factory
):
    """APPLIED: mute doesn't exist → still APPLIED (idempotent delete)."""
    user_id = await insert_user(session_factory)
    assert await mute_repo.get(user_id=user_id, chat_id=CHAT_ID) is None

    res = await action_repo.unmute_chat_atomic(
        user_id=user_id,
        chat_id=CHAT_ID,
        provider_message_id="evt-pg-unmute-nomute",
    )
    assert res.outcome == HandlingOutcome.APPLIED
    assert await mute_repo.get(user_id=user_id, chat_id=CHAT_ID) is None


async def test_pg_unmute_chat_atomic_duplicate(action_repo, mute_repo, session_factory):
    """DUPLICATE: same provider_message_id twice."""
    user_id = await insert_user(session_factory)
    await mute_repo.mute_permanent(user_id=user_id, chat_id=CHAT_ID)

    r1 = await action_repo.unmute_chat_atomic(
        user_id=user_id,
        chat_id=CHAT_ID,
        provider_message_id="evt-pg-unmute-dup",
    )
    assert r1.outcome == HandlingOutcome.APPLIED

    r2 = await action_repo.unmute_chat_atomic(
        user_id=user_id,
        chat_id=CHAT_ID,
        provider_message_id="evt-pg-unmute-dup",
    )
    assert r2.outcome == HandlingOutcome.DUPLICATE


async def test_pg_atomic_methods_return_action_command_result(action_repo, session_factory):
    """All atomic methods return ActionCommandResult instances."""
    user_id = await insert_user(session_factory)
    res = await action_repo.mute_chat_atomic(
        user_id=user_id,
        chat_id=CHAT_ID,
        permanent=True,
        muted_until=None,
        provider_message_id="evt-pg-typecheck",
    )
    assert isinstance(res, ActionCommandResult)
