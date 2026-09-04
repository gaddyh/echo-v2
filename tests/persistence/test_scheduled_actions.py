"""Tests for InMemoryScheduledActionRepository."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from echo_v2.domain.scheduling import (
    ScheduledAction,
    ScheduledActionStatus,
    ScheduledActionType,
)
from echo_v2.persistence.scheduled_actions import (
    InMemoryScheduledActionRepository,
    ScheduledActionRepository,
)


def _make_action(
    *,
    id: str = "act-1",
    user_id: str = "user-1",
    execute_at: datetime | None = None,
    status: ScheduledActionStatus = ScheduledActionStatus.PENDING,
    payload: dict | None = None,
    claimed_at: datetime | None = None,
) -> ScheduledAction:
    return ScheduledAction(
        id=id,
        user_id=user_id,
        type=ScheduledActionType.SEND_WHATSAPP_MESSAGE,
        execute_at_utc=execute_at or datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc),
        timezone="Asia/Jerusalem",
        status=status,
        payload=payload or {"chat_id": "972@c.us", "message": "hello"},
        claimed_at=claimed_at,
    )


def test_in_memory_repo_is_scheduled_action_repository():
    repo = InMemoryScheduledActionRepository()
    assert isinstance(repo, ScheduledActionRepository)


async def test_save_and_get():
    repo = InMemoryScheduledActionRepository()
    action = _make_action()
    await repo.save(action)
    fetched = await repo.get("act-1")
    assert fetched is not None
    assert fetched.id == "act-1"
    assert fetched.status is ScheduledActionStatus.PENDING


async def test_get_returns_none_for_missing():
    repo = InMemoryScheduledActionRepository()
    assert await repo.get("nope") is None


async def test_list_pending_returns_only_pending_sorted_by_execute_at():
    repo = InMemoryScheduledActionRepository()
    later = _make_action(id="a2", execute_at=datetime(2026, 9, 5, 10, tzinfo=timezone.utc))
    earlier = _make_action(id="a1", execute_at=datetime(2026, 9, 5, 8, tzinfo=timezone.utc))
    done = _make_action(id="a3", status=ScheduledActionStatus.SUCCEEDED)
    other_user = _make_action(id="a4", user_id="user-2")
    await repo.save(later)
    await repo.save(earlier)
    await repo.save(done)
    await repo.save(other_user)

    pending = await repo.list_pending("user-1")
    assert [a.id for a in pending] == ["a1", "a2"]


async def test_claim_due_returns_oldest_due_action():
    repo = InMemoryScheduledActionRepository()
    now = datetime(2026, 9, 5, 9, tzinfo=timezone.utc)
    due = _make_action(id="a1", execute_at=datetime(2026, 9, 5, 8, tzinfo=timezone.utc))
    not_yet = _make_action(id="a2", execute_at=datetime(2026, 9, 5, 10, tzinfo=timezone.utc))
    await repo.save(due)
    await repo.save(not_yet)

    claimed = await repo.claim_due(now)
    assert claimed is not None
    assert claimed.id == "a1"
    assert claimed.status is ScheduledActionStatus.IN_PROGRESS
    assert claimed.claimed_at == now


async def test_claim_due_returns_none_when_nothing_due():
    repo = InMemoryScheduledActionRepository()
    now = datetime(2026, 9, 5, 8, tzinfo=timezone.utc)
    future = _make_action(execute_at=datetime(2026, 9, 5, 10, tzinfo=timezone.utc))
    await repo.save(future)
    assert await repo.claim_due(now) is None


async def test_claim_due_skips_non_pending():
    repo = InMemoryScheduledActionRepository()
    now = datetime(2026, 9, 5, 9, tzinfo=timezone.utc)
    in_progress = _make_action(status=ScheduledActionStatus.IN_PROGRESS)
    await repo.save(in_progress)
    assert await repo.claim_due(now) is None


async def test_mark_succeeded_sets_status_and_result():
    repo = InMemoryScheduledActionRepository()
    await repo.save(_make_action())
    await repo.mark_succeeded("act-1", {"provider_message_id": "MSG_1"})
    action = await repo.get("act-1")
    assert action.status is ScheduledActionStatus.SUCCEEDED
    assert action.result == {"provider_message_id": "MSG_1"}
    assert action.executed_at is not None


async def test_mark_failed_sets_status_and_error():
    repo = InMemoryScheduledActionRepository()
    await repo.save(_make_action())
    await repo.mark_failed("act-1", "boom")
    action = await repo.get("act-1")
    assert action.status is ScheduledActionStatus.FAILED
    assert action.error == "boom"


async def test_mark_indeterminate_sets_status_and_error():
    repo = InMemoryScheduledActionRepository()
    await repo.save(_make_action())
    await repo.mark_indeterminate("act-1", "timeout")
    action = await repo.get("act-1")
    assert action.status is ScheduledActionStatus.INDETERMINATE
    assert action.error == "timeout"


async def test_cancel_pending_action():
    repo = InMemoryScheduledActionRepository()
    await repo.save(_make_action())
    assert await repo.cancel("act-1", "user-1") is True
    action = await repo.get("act-1")
    assert action.status is ScheduledActionStatus.CANCELLED


async def test_cancel_returns_false_for_wrong_user():
    repo = InMemoryScheduledActionRepository()
    await repo.save(_make_action())
    assert await repo.cancel("act-1", "user-2") is False
    action = await repo.get("act-1")
    assert action.status is ScheduledActionStatus.PENDING


async def test_cancel_returns_false_for_non_pending():
    repo = InMemoryScheduledActionRepository()
    await repo.save(_make_action(status=ScheduledActionStatus.IN_PROGRESS))
    assert await repo.cancel("act-1", "user-1") is False


async def test_cancel_returns_false_for_missing():
    repo = InMemoryScheduledActionRepository()
    assert await repo.cancel("nope", "user-1") is False


async def test_recover_stale_resets_old_in_progress_to_pending():
    repo = InMemoryScheduledActionRepository()
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    old_claim = now - timedelta(seconds=600)
    stale = _make_action(
        status=ScheduledActionStatus.IN_PROGRESS,
        claimed_at=old_claim,
    )
    fresh = _make_action(
        id="act-2",
        status=ScheduledActionStatus.IN_PROGRESS,
        claimed_at=now - timedelta(seconds=10),
    )
    pending = _make_action(id="act-3")
    await repo.save(stale)
    await repo.save(fresh)
    await repo.save(pending)

    recovered = await repo.recover_stale(now, lease_seconds=300)
    assert recovered == 1
    assert (await repo.get("act-1")).status is ScheduledActionStatus.PENDING
    assert (await repo.get("act-1")).claimed_at is None
    assert (await repo.get("act-2")).status is ScheduledActionStatus.IN_PROGRESS


async def test_recover_stale_returns_zero_when_none_stale():
    repo = InMemoryScheduledActionRepository()
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    fresh = _make_action(
        status=ScheduledActionStatus.IN_PROGRESS,
        claimed_at=now - timedelta(seconds=10),
    )
    await repo.save(fresh)
    assert await repo.recover_stale(now, lease_seconds=300) == 0
