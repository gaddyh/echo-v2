"""Tests for the web app action methods on WaitingForMeActionService.

Covers:
* done — operational resolve, no automatic CORRECT feedback.
* dismiss_with_reason — reason → feedback split.
* snooze presets (morning/afternoon/evening/tomorrow) + custom.
* Idempotency checked before item existence (retry after delete → duplicate).
* Atomic version-checked delete (stale → STALE, not_found → NOT_FOUND).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.domain.feedback import FeedbackVerdict, HandlingOutcome
from echo_v2.persistence.chat_repositories import (
    InMemoryWaitingForMeActiveRepository,
    InMemoryWaitingForMeResultRepository,
)
from echo_v2.persistence.feedback_repositories import (
    InMemoryChatMuteRepository,
    InMemoryWaitingForMeActionRepository,
    InMemoryWaitingForMeFeedbackRepository,
)
from echo_v2.services.feedback_service import WaitingForMeActionService

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 12, 6, 0, 0, tzinfo=timezone.utc)
USER_ID = "user-1"
CHAT_ID = "972508765432@c.us"
ACTIVE_ID = "active-1"
RESULT_ID = "result-1"
SESSION_ID = "session-1"


def _make_service(
    *,
    active_repo: InMemoryWaitingForMeActiveRepository | None = None,
    action_repo: InMemoryWaitingForMeActionRepository | None = None,
    feedback_repo: InMemoryWaitingForMeFeedbackRepository | None = None,
) -> tuple[
    WaitingForMeActionService,
    InMemoryWaitingForMeActiveRepository,
    InMemoryWaitingForMeActionRepository,
    InMemoryWaitingForMeFeedbackRepository,
]:
    active_repo = active_repo or InMemoryWaitingForMeActiveRepository()
    action_repo = action_repo or InMemoryWaitingForMeActionRepository()
    feedback_repo = feedback_repo or InMemoryWaitingForMeFeedbackRepository()
    service = WaitingForMeActionService(
        active_repo=active_repo,
        action_repo=action_repo,
        mute_repo=InMemoryChatMuteRepository(),
        feedback_repo=feedback_repo,
        result_repo=InMemoryWaitingForMeResultRepository(),
    )
    return service, active_repo, action_repo, feedback_repo


async def _setup_active(
    active_repo: InMemoryWaitingForMeActiveRepository,
    *,
    active_id: str = ACTIVE_ID,
    target_version: int = 1,
    user_id: str = USER_ID,
    chat_id: str = CHAT_ID,
):
    await active_repo.upsert(
        user_id=user_id,
        chat_id=chat_id,
        target_version=target_version,
        result_id=RESULT_ID,
        waiting_since=NOW,
    )
    # The upsert generates a UUID; we need to use the generated id.
    # For tests, we'll look it up by chat_id.
    active = await active_repo.get(user_id=user_id, chat_id=chat_id)
    return active.id


# --- done ---


async def test_done_resolves_and_no_feedback():
    service, active_repo, action_repo, feedback_repo = _make_service()
    active_id = await _setup_active(active_repo)

    outcome = await service.done(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        session_id=SESSION_ID,
    )
    assert outcome == HandlingOutcome.APPLIED
    # Active row deleted.
    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active is None
    # No feedback recorded.
    assert len(feedback_repo._rows) == 0
    # Action recorded with session_id.
    assert len(action_repo._rows) == 1
    assert action_repo._rows[0].action_payload["waiting_list_session_id"] == SESSION_ID
    assert action_repo._rows[0].action_payload["source"] == "waiting_list_web"


async def test_done_duplicate_after_delete_returns_duplicate():
    """Idempotency checked before item existence — retry after delete → duplicate."""
    service, active_repo, _action_repo, _feedback_repo = _make_service()
    active_id = await _setup_active(active_repo)

    # First call succeeds.
    outcome1 = await service.done(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        session_id=SESSION_ID,
    )
    assert outcome1 == HandlingOutcome.APPLIED

    # Retry with same action_id — should return DUPLICATE, not NOT_FOUND.
    outcome2 = await service.done(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        session_id=SESSION_ID,
    )
    assert outcome2 == HandlingOutcome.DUPLICATE


async def test_done_stale_version_returns_stale():
    service, active_repo, _, _ = _make_service()
    active_id = await _setup_active(active_repo, target_version=2)

    outcome = await service.done(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,  # stale
        provider_message_id="web:action-1",
        session_id=SESSION_ID,
    )
    assert outcome == HandlingOutcome.STALE
    # Active row NOT deleted.
    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active is not None


async def test_done_not_found_returns_not_found():
    service, _, _, _ = _make_service()
    outcome = await service.done(
        user_id=USER_ID,
        active_id="nonexistent",
        target_version=1,
        provider_message_id="web:action-1",
        session_id=SESSION_ID,
    )
    assert outcome == HandlingOutcome.NOT_FOUND


# --- dismiss_with_reason ---


async def test_dismiss_detected_incorrectly_records_false_positive():
    service, active_repo, _, feedback_repo = _make_service()
    active_id = await _setup_active(active_repo)

    outcome = await service.dismiss_with_reason(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        reason="detected_incorrectly",
        session_id=SESSION_ID,
    )
    assert outcome == HandlingOutcome.APPLIED
    # FALSE_POSITIVE feedback recorded.
    assert len(feedback_repo._rows) == 1
    assert feedback_repo._rows[0].verdict == FeedbackVerdict.FALSE_POSITIVE


async def test_dismiss_already_handled_no_feedback():
    service, active_repo, _, feedback_repo = _make_service()
    active_id = await _setup_active(active_repo)

    outcome = await service.dismiss_with_reason(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        reason="already_handled",
        session_id=SESSION_ID,
    )
    assert outcome == HandlingOutcome.APPLIED
    # No feedback recorded.
    assert len(feedback_repo._rows) == 0


async def test_dismiss_no_response_required_no_feedback():
    service, active_repo, _, feedback_repo = _make_service()
    active_id = await _setup_active(active_repo)

    outcome = await service.dismiss_with_reason(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        reason="no_response_required",
        session_id=SESSION_ID,
    )
    assert outcome == HandlingOutcome.APPLIED
    assert len(feedback_repo._rows) == 0


async def test_dismiss_duplicate_returns_duplicate():
    service, active_repo, _, _ = _make_service()
    active_id = await _setup_active(active_repo)

    await service.dismiss_with_reason(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        reason="already_handled",
        session_id=SESSION_ID,
    )
    outcome = await service.dismiss_with_reason(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        reason="already_handled",
        session_id=SESSION_ID,
    )
    assert outcome == HandlingOutcome.DUPLICATE


# --- snooze presets ---


async def test_snooze_preset_morning():
    service, active_repo, _, _ = _make_service()
    active_id = await _setup_active(active_repo)
    # NOW is 2026-09-12 06:00 UTC = 09:00 Asia/Jerusalem (UTC+3).
    # "morning" = 08:00 local. Since 08:00 already passed, → tomorrow 08:00.
    outcome = await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        tz_name="Asia/Jerusalem",
        snooze_preset="morning",
        now_utc=NOW,
    )
    assert outcome == HandlingOutcome.APPLIED
    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active is not None
    assert active.snoozed_until is not None
    # Should be 2026-09-13 05:00 UTC = 08:00 local.
    expected = datetime(2026, 9, 13, 5, 0, 0, tzinfo=timezone.utc)
    assert active.snoozed_until == expected


async def test_snooze_preset_tomorrow():
    service, active_repo, _, _ = _make_service()
    active_id = await _setup_active(active_repo)
    outcome = await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        tz_name="Asia/Jerusalem",
        snooze_preset="tomorrow",
        now_utc=NOW,
    )
    assert outcome == HandlingOutcome.APPLIED
    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active is not None
    expected = datetime(2026, 9, 13, 5, 0, 0, tzinfo=timezone.utc)
    assert active.snoozed_until == expected


async def test_snooze_custom_valid():
    service, active_repo, _, _ = _make_service()
    active_id = await _setup_active(active_repo)
    snooze_until = NOW + timedelta(hours=6)
    outcome = await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        snooze_until=snooze_until,
        now_utc=NOW,
    )
    assert outcome == HandlingOutcome.APPLIED
    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active is not None
    assert active.snoozed_until == snooze_until


async def test_snooze_custom_in_past_returns_invalid():
    service, active_repo, _, _ = _make_service()
    active_id = await _setup_active(active_repo)
    snooze_until = NOW - timedelta(hours=1)
    outcome = await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        snooze_until=snooze_until,
        now_utc=NOW,
    )
    assert outcome == HandlingOutcome.INVALID


async def test_snooze_custom_too_far_returns_invalid():
    service, active_repo, _, _ = _make_service()
    active_id = await _setup_active(active_repo)
    snooze_until = NOW + timedelta(days=10)
    outcome = await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        snooze_until=snooze_until,
        now_utc=NOW,
    )
    assert outcome == HandlingOutcome.INVALID


async def test_snooze_invalid_preset_returns_invalid():
    service, active_repo, _, _ = _make_service()
    active_id = await _setup_active(active_repo)
    outcome = await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        snooze_preset="invalid_preset",
        now_utc=NOW,
    )
    assert outcome == HandlingOutcome.INVALID


async def test_snooze_default_uses_next_digest_hour():
    service, active_repo, _, _ = _make_service()
    active_id = await _setup_active(active_repo)
    outcome = await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        tz_name="Asia/Jerusalem",
        now_utc=NOW,
    )
    assert outcome == HandlingOutcome.APPLIED
    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active is not None
    assert active.snoozed_until is not None


async def test_snooze_dst_boundary():
    """Snooze to 'tomorrow' across a DST boundary.

    Asia/Jerusalem DST ends on 2026-10-25 at 02:00 → 01:00 (clocks back).
    Before: UTC+3. After: UTC+2.
    """
    service, active_repo, _, _ = _make_service()
    # Set up active at 2026-10-24 06:00 UTC = 09:00 local (DST, UTC+3).
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        target_version=1,
        result_id=RESULT_ID,
        waiting_since=datetime(2026, 10, 24, 6, 0, 0, tzinfo=timezone.utc),
    )
    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    # Snooze to "tomorrow" — 2026-10-25 08:00 local. After DST end, 08:00
    # local is 06:00 UTC (UTC+2).
    outcome = await service.snooze(
        user_id=USER_ID,
        active_id=active.id,
        target_version=1,
        provider_message_id="web:action-1",
        tz_name="Asia/Jerusalem",
        snooze_preset="tomorrow",
        now_utc=datetime(2026, 10, 24, 6, 0, 0, tzinfo=timezone.utc),
    )
    assert outcome == HandlingOutcome.APPLIED
    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active is not None
    # 2026-10-25 08:00 local (UTC+2) = 06:00 UTC.
    expected = datetime(2026, 10, 25, 6, 0, 0, tzinfo=timezone.utc)
    assert active.snoozed_until == expected
