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
    scheduling_service=None,
    user_phone_lookup=None,
    chat_name_lookup=None,
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
        scheduling_service=scheduling_service,
        user_phone_lookup=user_phone_lookup,
        chat_name_lookup=chat_name_lookup,
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


async def test_snooze_default_uses_one_hour():
    """Default snooze (no preset, no snooze_until) is 1 hour from now."""
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
    assert active.snoozed_until == NOW + timedelta(hours=1)


async def test_snooze_preset_10m():
    """Snooze preset '10m' is 10 minutes from now."""
    service, active_repo, _, _ = _make_service()
    active_id = await _setup_active(active_repo)
    outcome = await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        tz_name="Asia/Jerusalem",
        snooze_preset="10m",
        now_utc=NOW,
    )
    assert outcome == HandlingOutcome.APPLIED
    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active is not None
    assert active.snoozed_until == NOW + timedelta(minutes=10)


async def test_snooze_preset_1h():
    """Snooze preset '1h' is 1 hour from now."""
    service, active_repo, _, _ = _make_service()
    active_id = await _setup_active(active_repo)
    outcome = await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        tz_name="Asia/Jerusalem",
        snooze_preset="1h",
        now_utc=NOW,
    )
    assert outcome == HandlingOutcome.APPLIED
    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active is not None
    assert active.snoozed_until == NOW + timedelta(hours=1)


async def test_snooze_preset_3h():
    """Snooze preset '3h' is 3 hours from now."""
    service, active_repo, _, _ = _make_service()
    active_id = await _setup_active(active_repo)
    outcome = await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        tz_name="Asia/Jerusalem",
        snooze_preset="3h",
        now_utc=NOW,
    )
    assert outcome == HandlingOutcome.APPLIED
    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active is not None
    assert active.snoozed_until == NOW + timedelta(hours=3)


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


# --- Edge cases ---


async def test_snooze_exactly_now_is_invalid():
    """snooze_until == now is not strictly future → INVALID."""
    service, active_repo, _, _ = _make_service()
    active_id = await _setup_active(active_repo)
    outcome = await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        snooze_until=NOW,
        now_utc=NOW,
    )
    assert outcome == HandlingOutcome.INVALID


async def test_snooze_exactly_7_days_is_valid():
    """snooze_until exactly 7 days ahead is the boundary — valid."""
    service, active_repo, _, _ = _make_service()
    active_id = await _setup_active(active_repo)
    snooze_until = NOW + timedelta(days=7)
    outcome = await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        snooze_until=snooze_until,
        now_utc=NOW,
    )
    assert outcome == HandlingOutcome.APPLIED


async def test_snooze_7_days_plus_1_second_is_invalid():
    """snooze_until 7 days + 1 second exceeds the max → INVALID."""
    service, active_repo, _, _ = _make_service()
    active_id = await _setup_active(active_repo)
    snooze_until = NOW + timedelta(days=7, seconds=1)
    outcome = await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        snooze_until=snooze_until,
        now_utc=NOW,
    )
    assert outcome == HandlingOutcome.INVALID


async def test_snooze_preserves_session_id_in_payload():
    """Snooze with session_id records it in the action payload."""
    service, active_repo, action_repo, _ = _make_service()
    active_id = await _setup_active(active_repo)
    await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        snooze_preset="tomorrow",
        now_utc=NOW,
        session_id="my-session",
    )
    assert len(action_repo._rows) == 1
    payload = action_repo._rows[0].action_payload
    assert payload["waiting_list_session_id"] == "my-session"
    assert payload["source"] == "waiting_list_web"
    assert payload["snooze_preset"] == "tomorrow"


async def test_snooze_without_session_id_omits_web_metadata():
    """Snooze without session_id (WhatsApp path) does not add web metadata."""
    service, active_repo, action_repo, _ = _make_service()
    active_id = await _setup_active(active_repo)
    await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="wa:action-1",
        snooze_preset="tomorrow",
        now_utc=NOW,
    )
    assert len(action_repo._rows) == 1
    payload = action_repo._rows[0].action_payload
    assert "waiting_list_session_id" not in payload
    assert "source" not in payload


async def test_snooze_preset_afternoon():
    service, active_repo, _, _ = _make_service()
    active_id = await _setup_active(active_repo)
    outcome = await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        tz_name="Asia/Jerusalem",
        snooze_preset="afternoon",
        now_utc=NOW,
    )
    assert outcome == HandlingOutcome.APPLIED
    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active is not None
    assert active.snoozed_until is not None


async def test_snooze_preset_evening():
    service, active_repo, _, _ = _make_service()
    active_id = await _setup_active(active_repo)
    outcome = await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        tz_name="Asia/Jerusalem",
        snooze_preset="evening",
        now_utc=NOW,
    )
    assert outcome == HandlingOutcome.APPLIED
    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active is not None
    assert active.snoozed_until is not None


async def test_done_then_snooze_same_action_id_returns_duplicate():
    """After done, a snooze with the same action_id returns DUPLICATE."""
    service, active_repo, _, _ = _make_service()
    active_id = await _setup_active(active_repo)

    outcome1 = await service.done(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:same-id",
        session_id=SESSION_ID,
    )
    assert outcome1 == HandlingOutcome.APPLIED

    outcome2 = await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:same-id",
        now_utc=NOW,
    )
    assert outcome2 == HandlingOutcome.DUPLICATE


async def test_dismiss_unknown_reason_no_feedback():
    """An unknown reason still resolves the item but records no feedback."""
    service, active_repo, _, feedback_repo = _make_service()
    active_id = await _setup_active(active_repo)
    outcome = await service.dismiss_with_reason(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        reason="some_other_reason",
        session_id=SESSION_ID,
    )
    assert outcome == HandlingOutcome.APPLIED
    assert len(feedback_repo._rows) == 0


async def test_dismiss_stale_returns_stale():
    service, active_repo, _, _ = _make_service()
    active_id = await _setup_active(active_repo, target_version=2)
    outcome = await service.dismiss_with_reason(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        reason="already_handled",
        session_id=SESSION_ID,
    )
    assert outcome == HandlingOutcome.STALE


async def test_dismiss_not_found_returns_not_found():
    service, _, _, _ = _make_service()
    outcome = await service.dismiss_with_reason(
        user_id=USER_ID,
        active_id="nonexistent",
        target_version=1,
        provider_message_id="web:action-1",
        reason="already_handled",
        session_id=SESSION_ID,
    )
    assert outcome == HandlingOutcome.NOT_FOUND


async def test_snooze_stale_returns_stale():
    service, active_repo, _, _ = _make_service()
    active_id = await _setup_active(active_repo, target_version=2)
    outcome = await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        now_utc=NOW,
    )
    assert outcome == HandlingOutcome.STALE


async def test_snooze_not_found_returns_stale():
    """Snooze on non-existent item returns STALE (apply_if_version can't
    distinguish not-found from stale — both return False)."""
    service, _, _, _ = _make_service()
    outcome = await service.snooze(
        user_id=USER_ID,
        active_id="nonexistent",
        target_version=1,
        provider_message_id="web:action-1",
        now_utc=NOW,
    )
    # The snooze method records the action first, then apply_if_version
    # returns False. The code returns STALE for both cases.
    assert outcome in (HandlingOutcome.STALE, HandlingOutcome.NOT_FOUND)


async def test_snooze_duplicate_returns_duplicate():
    service, active_repo, _, _ = _make_service()
    active_id = await _setup_active(active_repo)
    await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        snooze_preset="tomorrow",
        now_utc=NOW,
    )
    outcome = await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        snooze_preset="tomorrow",
        now_utc=NOW,
    )
    assert outcome == HandlingOutcome.DUPLICATE


async def test_done_cross_user_returns_stale():
    """done with wrong user_id returns STALE (not NOT_FOUND).

    The delete_if_version checks user_id → 0 rows deleted. Then
    get_by_id finds the row (it belongs to user-a), so the code
    returns STALE. This is acceptable — _build_item uses the
    session's user_id to query chat_state, so no data leaks.
    """
    service, active_repo, _, _ = _make_service()
    active_id = await _setup_active(active_repo, user_id="user-a")
    outcome = await service.done(
        user_id="user-b",
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        session_id=SESSION_ID,
    )
    assert outcome == HandlingOutcome.STALE
    # Item still exists for user-a.
    active = await active_repo.get(user_id="user-a", chat_id=CHAT_ID)
    assert active is not None


async def test_snooze_on_already_snoozed_item():
    """Snoozing an already-snoozed item updates snoozed_until."""
    service, active_repo, _, _ = _make_service()
    active_id = await _setup_active(active_repo)
    # First snooze.
    await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        snooze_preset="morning",
        now_utc=NOW,
    )
    # Second snooze with different action_id.
    outcome = await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-2",
        snooze_preset="evening",
        now_utc=NOW,
    )
    assert outcome == HandlingOutcome.APPLIED
    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active is not None
    assert active.snoozed_until is not None


# --- snooze reminder scheduling -------------------------------------------


class FakeSchedulingService:
    """Records create() calls; used to verify snooze schedules a reminder."""

    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create(
        self,
        *,
        user_id: str,
        type,
        execute_at_utc: datetime,
        timezone_name: str,
        payload: dict,
    ):
        self.created.append(
            {
                "user_id": user_id,
                "type": type,
                "execute_at_utc": execute_at_utc,
                "timezone_name": timezone_name,
                "payload": payload,
            }
        )
        from echo_v2.domain.scheduling import ScheduledAction, ScheduledActionStatus
        return ScheduledAction(
            id="sched-1",
            user_id=user_id,
            type=type,
            execute_at_utc=execute_at_utc,
            timezone=timezone_name,
            status=ScheduledActionStatus.PENDING,
            payload=payload,
        )


async def _phone_lookup(_user_id: str) -> str | None:
    return "972500000001"


async def _chat_name_lookup(_user_id: str, _chat_id: str) -> str | None:
    return "דנה לוי"


async def test_snooze_schedules_reminder():
    """Snoozing with a scheduling_service creates a SEND_BOT_MESSAGE."""
    sched = FakeSchedulingService()
    service, active_repo, _, _ = _make_service(
        scheduling_service=sched,
        user_phone_lookup=_phone_lookup,
        chat_name_lookup=_chat_name_lookup,
    )
    active_id = await _setup_active(active_repo)
    outcome = await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        snooze_preset="1h",
        now_utc=NOW,
    )
    assert outcome == HandlingOutcome.APPLIED
    assert len(sched.created) == 1
    created = sched.created[0]
    from echo_v2.domain.scheduling import ScheduledActionType
    assert created["type"] is ScheduledActionType.SEND_BOT_MESSAGE
    assert created["execute_at_utc"] == NOW + timedelta(hours=1)
    assert created["payload"]["chat_id"] == "972500000001"
    assert "תזכורת" in created["payload"]["message"]
    assert "דנה לוי" in created["payload"]["message"]
    assert len(created["payload"]["buttons"]) == 3
    titles = [b["title"] for b in created["payload"]["buttons"]]
    assert "טופל" in titles
    assert "נודניק עוד שעה" in titles
    assert "לא להיום" in titles


async def test_snooze_without_scheduling_service_does_not_schedule():
    """Without a scheduling_service, snooze still works but no reminder."""
    service, active_repo, _, _ = _make_service()
    active_id = await _setup_active(active_repo)
    outcome = await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        snooze_preset="1h",
        now_utc=NOW,
    )
    assert outcome == HandlingOutcome.APPLIED


async def test_snooze_scheduling_failure_does_not_fail_snooze():
    """If scheduling fails, the snooze action still succeeds."""
    class FailingSchedulingService(FakeSchedulingService):
        async def create(self, **kwargs):
            raise RuntimeError("scheduler down")
    sched = FailingSchedulingService()
    service, active_repo, _, _ = _make_service(
        scheduling_service=sched,
        user_phone_lookup=_phone_lookup,
        chat_name_lookup=_chat_name_lookup,
    )
    active_id = await _setup_active(active_repo)
    outcome = await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        snooze_preset="1h",
        now_utc=NOW,
    )
    assert outcome == HandlingOutcome.APPLIED
    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active is not None
    assert active.snoozed_until == NOW + timedelta(hours=1)


async def test_snooze_scheduling_no_phone_skips_reminder():
    """If user_phone_lookup returns None, no reminder is scheduled."""
    async def no_phone(_user_id: str) -> str | None:
        return None
    sched = FakeSchedulingService()
    service, active_repo, _, _ = _make_service(
        scheduling_service=sched,
        user_phone_lookup=no_phone,
        chat_name_lookup=_chat_name_lookup,
    )
    active_id = await _setup_active(active_repo)
    outcome = await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        snooze_preset="1h",
        now_utc=NOW,
    )
    assert outcome == HandlingOutcome.APPLIED
    assert len(sched.created) == 0


async def test_snooze_scheduling_no_name_lookup_uses_fallback():
    """If chat_name_lookup is None, the reminder uses 'לקוח' fallback."""
    sched = FakeSchedulingService()
    service, active_repo, _, _ = _make_service(
        scheduling_service=sched,
        user_phone_lookup=_phone_lookup,
        chat_name_lookup=None,
    )
    active_id = await _setup_active(active_repo)
    outcome = await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        snooze_preset="1h",
        now_utc=NOW,
    )
    assert outcome == HandlingOutcome.APPLIED
    assert len(sched.created) == 1
    assert "לקוח" in sched.created[0]["payload"]["message"]


async def test_snooze_scheduling_no_name_uses_fallback():
    """If chat_name_lookup returns None, the reminder uses 'לקוח' fallback."""
    async def no_name(_user_id: str, _chat_id: str) -> str | None:
        return None
    sched = FakeSchedulingService()
    service, active_repo, _, _ = _make_service(
        scheduling_service=sched,
        user_phone_lookup=_phone_lookup,
        chat_name_lookup=no_name,
    )
    active_id = await _setup_active(active_repo)
    outcome = await service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="web:action-1",
        snooze_preset="1h",
        now_utc=NOW,
    )
    assert outcome == HandlingOutcome.APPLIED
    assert len(sched.created) == 1
    assert "לקוח" in sched.created[0]["payload"]["message"]
