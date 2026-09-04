"""Tests for PostgresScheduledActionRepository against a testcontainer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.domain.scheduling import (
    ScheduledAction,
    ScheduledActionStatus,
    ScheduledActionType,
)
from echo_v2.persistence.postgres_scheduled_actions import (
    PostgresScheduledActionRepository,
)
from echo_v2.persistence.scheduled_actions import ScheduledActionRepository

from tests.persistence.conftest import insert_user


pytestmark = pytest.mark.asyncio


def _make_action(
    user_id: str,
    *,
    id: str = "act-1",
    execute_at: datetime | None = None,
    status: ScheduledActionStatus = ScheduledActionStatus.PENDING,
    claimed_at: datetime | None = None,
) -> ScheduledAction:
    return ScheduledAction(
        id=id,
        user_id=user_id,
        type=ScheduledActionType.SEND_WHATSAPP_MESSAGE,
        execute_at_utc=execute_at or datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc),
        timezone="Asia/Jerusalem",
        status=status,
        payload={"chat_id": "972@c.us", "message": "hello"},
        claimed_at=claimed_at,
    )


async def test_postgres_repo_is_scheduled_action_repository(scheduled_actions_repo):
    assert isinstance(scheduled_actions_repo, ScheduledActionRepository)


async def test_save_and_get(scheduled_actions_repo, session_factory):
    user_id = await insert_user(session_factory)
    action = _make_action(user_id)
    await scheduled_actions_repo.save(action)
    fetched = await scheduled_actions_repo.get("act-1")
    assert fetched is not None
    assert fetched.id == "act-1"
    assert fetched.user_id == user_id
    assert fetched.status is ScheduledActionStatus.PENDING
    assert fetched.payload == {"chat_id": "972@c.us", "message": "hello"}
    assert fetched.timezone == "Asia/Jerusalem"


async def test_get_returns_none_for_missing(scheduled_actions_repo):
    assert await scheduled_actions_repo.get("nope") is None


async def test_list_pending_sorted_by_execute_at(scheduled_actions_repo, session_factory):
    user_id = await insert_user(session_factory)
    await scheduled_actions_repo.save(
        _make_action(user_id, id="a2", execute_at=datetime(2026, 9, 5, 10, tzinfo=timezone.utc))
    )
    await scheduled_actions_repo.save(
        _make_action(user_id, id="a1", execute_at=datetime(2026, 9, 5, 8, tzinfo=timezone.utc))
    )
    await scheduled_actions_repo.save(
        _make_action(user_id, id="a3", status=ScheduledActionStatus.SUCCEEDED)
    )
    pending = await scheduled_actions_repo.list_pending(user_id)
    assert [a.id for a in pending] == ["a1", "a2"]


async def test_claim_due_returns_oldest_due(scheduled_actions_repo, session_factory):
    user_id = await insert_user(session_factory)
    now = datetime(2026, 9, 5, 9, tzinfo=timezone.utc)
    await scheduled_actions_repo.save(
        _make_action(user_id, id="a1", execute_at=datetime(2026, 9, 5, 8, tzinfo=timezone.utc))
    )
    await scheduled_actions_repo.save(
        _make_action(user_id, id="a2", execute_at=datetime(2026, 9, 5, 10, tzinfo=timezone.utc))
    )
    claimed = await scheduled_actions_repo.claim_due(now)
    assert claimed is not None
    assert claimed.id == "a1"
    assert claimed.status is ScheduledActionStatus.IN_PROGRESS
    assert claimed.claimed_at is not None


async def test_claim_due_returns_none_when_nothing_due(scheduled_actions_repo, session_factory):
    user_id = await insert_user(session_factory)
    now = datetime(2026, 9, 5, 8, tzinfo=timezone.utc)
    await scheduled_actions_repo.save(
        _make_action(user_id, execute_at=datetime(2026, 9, 5, 10, tzinfo=timezone.utc))
    )
    assert await scheduled_actions_repo.claim_due(now) is None


async def test_claim_due_skips_non_pending(scheduled_actions_repo, session_factory):
    user_id = await insert_user(session_factory)
    now = datetime(2026, 9, 5, 9, tzinfo=timezone.utc)
    await scheduled_actions_repo.save(
        _make_action(user_id, status=ScheduledActionStatus.IN_PROGRESS)
    )
    assert await scheduled_actions_repo.claim_due(now) is None


async def test_mark_succeeded(scheduled_actions_repo, session_factory):
    user_id = await insert_user(session_factory)
    await scheduled_actions_repo.save(_make_action(user_id))
    await scheduled_actions_repo.mark_succeeded("act-1", {"provider_message_id": "MSG_1"})
    action = await scheduled_actions_repo.get("act-1")
    assert action.status is ScheduledActionStatus.SUCCEEDED
    assert action.result == {"provider_message_id": "MSG_1"}
    assert action.executed_at is not None


async def test_mark_failed(scheduled_actions_repo, session_factory):
    user_id = await insert_user(session_factory)
    await scheduled_actions_repo.save(_make_action(user_id))
    await scheduled_actions_repo.mark_failed("act-1", "boom")
    action = await scheduled_actions_repo.get("act-1")
    assert action.status is ScheduledActionStatus.FAILED
    assert action.error == "boom"


async def test_mark_indeterminate(scheduled_actions_repo, session_factory):
    user_id = await insert_user(session_factory)
    await scheduled_actions_repo.save(_make_action(user_id))
    await scheduled_actions_repo.mark_indeterminate("act-1", "timeout")
    action = await scheduled_actions_repo.get("act-1")
    assert action.status is ScheduledActionStatus.INDETERMINATE
    assert action.error == "timeout"


async def test_cancel_pending(scheduled_actions_repo, session_factory):
    user_id = await insert_user(session_factory)
    await scheduled_actions_repo.save(_make_action(user_id))
    assert await scheduled_actions_repo.cancel("act-1", user_id) is True
    action = await scheduled_actions_repo.get("act-1")
    assert action.status is ScheduledActionStatus.CANCELLED


async def test_cancel_wrong_user_returns_false(scheduled_actions_repo, session_factory):
    user_id = await insert_user(session_factory)
    await scheduled_actions_repo.save(_make_action(user_id))
    assert await scheduled_actions_repo.cancel("act-1", "other-user") is False


async def test_cancel_non_pending_returns_false(scheduled_actions_repo, session_factory):
    user_id = await insert_user(session_factory)
    await scheduled_actions_repo.save(
        _make_action(user_id, status=ScheduledActionStatus.IN_PROGRESS)
    )
    assert await scheduled_actions_repo.cancel("act-1", user_id) is False


async def test_recover_stale_resets_old_in_progress(scheduled_actions_repo, session_factory):
    user_id = await insert_user(session_factory)
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    old_claim = now - timedelta(seconds=600)
    await scheduled_actions_repo.save(
        _make_action(user_id, status=ScheduledActionStatus.IN_PROGRESS, claimed_at=old_claim)
    )
    await scheduled_actions_repo.save(
        _make_action(
            user_id,
            id="act-2",
            status=ScheduledActionStatus.IN_PROGRESS,
            claimed_at=now - timedelta(seconds=10),
        )
    )
    recovered = await scheduled_actions_repo.recover_stale(now, lease_seconds=300)
    assert recovered == 1
    assert (await scheduled_actions_repo.get("act-1")).status is ScheduledActionStatus.PENDING
    assert (await scheduled_actions_repo.get("act-1")).claimed_at is None
    assert (await scheduled_actions_repo.get("act-2")).status is ScheduledActionStatus.IN_PROGRESS


async def test_save_upserts_on_conflict(scheduled_actions_repo, session_factory):
    user_id = await insert_user(session_factory)
    action = _make_action(user_id)
    await scheduled_actions_repo.save(action)
    # Save again with updated status.
    from dataclasses import replace
    await scheduled_actions_repo.save(replace(action, status=ScheduledActionStatus.SUCCEEDED))
    fetched = await scheduled_actions_repo.get("act-1")
    assert fetched.status is ScheduledActionStatus.SUCCEEDED
