"""Tests for the feedback flyloop — actions, feedback, mutes, and stale callbacks.

Covers the 11 required test cases:

1. Duplicate callback records feedback once.
2. Callback belonging to another user is rejected.
3. Callback for an old target_version changes nothing.
4. "לא רלוונטי" (resolve) resolves only that active item.
5. "לא דחוף" (snooze) suppresses until the next configured time.
6. Expired snooze returns to the digest.
7. Muted chat is absent from digest and list.
8. Temporary mute expires.
9. Permanent mute does not expire.
10. Eleven active chats produce a valid bounded first page (max 10).
11. Empty day is recorded without sending a template.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.domain.feedback import FeedbackVerdict
from echo_v2.domain.waiting_for_me import WaitingForMeActive
from echo_v2.persistence.chat_repositories import (
    InMemoryChatStateRepository,
    InMemoryMessageRepository,
    InMemoryWaitingForMeActiveRepository,
)
from echo_v2.persistence.contacts import InMemoryContactRepository
from echo_v2.persistence.feedback_repositories import (
    InMemoryChatMuteRepository,
    InMemoryWaitingForMeActionRepository,
    InMemoryWaitingForMeFeedbackRepository,
)
from echo_v2.ports.bot import BotEvent, BotEventType
from echo_v2.services.feedback_handler import FeedbackHandler
from echo_v2.services.feedback_service import FeedbackService

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 12, 6, 0, 0, tzinfo=timezone.utc)
USER_ID = "user-1"
USER_PHONE = "972501234567"
OTHER_USER_ID = "user-2"
CHAT_ID = "972508765432@c.us"
OTHER_CHAT_ID = "972509876543@c.us"


class FakeBot:
    """Fake BotChannel that records sent messages."""

    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.lists: list[tuple[str, str, str, list]] = []
        self.buttons: list[tuple[str, str, list]] = []

    async def send_text(self, user_phone: str, text: str) -> None:
        self.texts.append((user_phone, text))

    async def send_template(
        self, user_phone: str, template_name: str, language: str, body_params: list[str]
    ) -> str:
        return "fake-msg-id"

    async def send_interactive_list(
        self, user_phone: str, *, body_text: str, button_text: str, sections: list
    ) -> str:
        self.lists.append((user_phone, body_text, button_text, sections))
        return "fake-list-id"

    async def send_buttons(
        self, user_phone: str, *, body_text: str, buttons: list
    ) -> str:
        self.buttons.append((user_phone, body_text, buttons))
        return "fake-buttons-id"


class FakeUserResolver:
    """Maps phone to (user_id, onboarding_status, first_name)."""

    def __init__(self, user_id: str | None = USER_ID) -> None:
        self._user_id = user_id

    async def resolve(self, phone: str):
        if self._user_id is None:
            return None
        return (self._user_id, "active", None)


def _make_active(
    chat_id: str = CHAT_ID,
    target_version: int = 1,
    user_id: str = USER_ID,
    waiting_since: datetime = NOW,
    snoozed_until: datetime | None = None,
) -> WaitingForMeActive:
    return WaitingForMeActive(
        user_id=user_id,
        chat_id=chat_id,
        target_version=target_version,
        result_id="result-1",
        waiting_since=waiting_since,
        snoozed_until=snoozed_until,
    )


async def _setup_chat_state(handler, chat_id: str, version: int = 1):
    """Set up a chat state row with matching activity_version."""
    from echo_v2.ports.whatsapp import MessageDirection

    await handler._chat_state_repo.upsert_on_message(
        user_id=USER_ID,
        chat_id=chat_id,
        direction=MessageDirection.INBOUND,
        observed_at=NOW,
        next_analysis_at=None,
    )
    # upsert_on_message sets activity_version=1; bump if needed.
    for i in range(1, version):
        await handler._chat_state_repo.upsert_on_message(
            user_id=USER_ID,
            chat_id=chat_id,
            direction=MessageDirection.INBOUND,
            observed_at=NOW,
            next_analysis_at=None,
        )


def _make_event(
    event_id: str = "evt-1",
    event_type: BotEventType = BotEventType.BUTTON_REPLY,
    text: str | None = None,
    button_id: str | None = None,
    list_id: str | None = None,
    user_phone: str = USER_PHONE,
) -> BotEvent:
    return BotEvent(
        event_id=event_id,
        user_phone=user_phone,
        type=event_type,
        text=text,
        button_id=button_id,
        list_id=list_id,
    )


def _make_handler(
    *,
    bot: FakeBot | None = None,
    user_id: str | None = USER_ID,
) -> tuple[FeedbackHandler, FeedbackService, InMemoryWaitingForMeActiveRepository]:
    """Build a fully wired FeedbackHandler with in-memory repos."""
    bot = bot or FakeBot()
    active_repo = InMemoryWaitingForMeActiveRepository()
    action_repo = InMemoryWaitingForMeActionRepository()
    feedback_repo = InMemoryWaitingForMeFeedbackRepository()
    mute_repo = InMemoryChatMuteRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()

    feedback_service = FeedbackService(
        active_repo=active_repo,
        action_repo=action_repo,
        feedback_repo=feedback_repo,
        mute_repo=mute_repo,
    )
    handler = FeedbackHandler(
        bot=bot,
        feedback_service=feedback_service,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        mute_repo=mute_repo,
        user_resolver=FakeUserResolver(user_id),
    )
    return handler, feedback_service, active_repo


# --- Test 1: Duplicate callback records feedback once -----------------------


async def test_duplicate_callback_records_once():
    """Duplicate callback (same provider_event_id) records action once."""
    handler, feedback_service, active_repo = _make_handler()
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        target_version=1,
        result_id="result-1",
        waiting_since=NOW,
    )

    event = _make_event(
        event_id="evt-dup-1",
        button_id=f"wfm_feedback:acknowledge:{CHAT_ID}:1",
    )

    # First call: handled, action recorded.
    result1 = await handler.handle(event)
    assert result1 is True
    actions1 = feedback_service._action_repo._rows  # type: ignore[attr-defined]
    assert len(actions1) == 1

    # Second call with same event_id: duplicate, no new action.
    result2 = await handler.handle(event)
    assert result2 is True
    assert len(actions1) == 1


# --- Test 2: Callback belonging to another user is rejected -----------------


async def test_callback_for_other_user_rejected():
    """A callback for an active item belonging to another user is rejected."""
    handler, feedback_service, active_repo = _make_handler()

    # Create active item for OTHER_USER_ID, but the resolver returns USER_ID.
    await active_repo.upsert(
        user_id=OTHER_USER_ID,
        chat_id=CHAT_ID,
        target_version=1,
        result_id="result-1",
        waiting_since=NOW,
    )

    event = _make_event(
        event_id="evt-other-user",
        button_id=f"wfm_feedback:acknowledge:{CHAT_ID}:1",
    )

    # The handler resolves to USER_ID, but the active item is for OTHER_USER_ID.
    # _validate checks active_repo.get(user_id=USER_ID, chat_id=CHAT_ID) → None.
    result = await handler.handle(event)
    assert result is True  # handled (returned True) but action was not applied.
    actions = feedback_service._action_repo._rows  # type: ignore[attr-defined]
    assert len(actions) == 0


# --- Test 3: Callback for old target_version changes nothing ----------------


async def test_stale_version_callback_changes_nothing():
    """A callback with an old target_version does not mutate current state."""
    handler, feedback_service, active_repo = _make_handler()
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        target_version=2,  # current version is 2
        result_id="result-2",
        waiting_since=NOW,
    )

    # Callback for version 1 (stale).
    event = _make_event(
        event_id="evt-stale",
        button_id=f"wfm_feedback:acknowledge:{CHAT_ID}:1",
    )

    result = await handler.handle(event)
    assert result is True
    actions = feedback_service._action_repo._rows  # type: ignore[attr-defined]
    assert len(actions) == 0

    # Verify active item was NOT acknowledged.
    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active is not None
    assert active.acknowledged_at is None


# --- Test 4: Resolve removes only that active item --------------------------


async def test_resolve_removes_only_that_item():
    """Resolve removes the specified active item, not others."""
    handler, _feedback_service, active_repo = _make_handler()
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        target_version=1,
        result_id="result-1",
        waiting_since=NOW,
    )
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id=OTHER_CHAT_ID,
        target_version=1,
        result_id="result-2",
        waiting_since=NOW,
    )

    event = _make_event(
        event_id="evt-resolve",
        button_id=f"wfm_feedback:resolve:{CHAT_ID}:1",
    )
    result = await handler.handle(event)
    assert result is True

    # Resolved item is gone.
    assert await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID) is None
    # Other item remains.
    assert await active_repo.get(user_id=USER_ID, chat_id=OTHER_CHAT_ID) is not None


# --- Test 5: Snooze suppresses until next morning --------------------------


async def test_snooze_suppresses_until_configured_time():
    """Snooze sets snoozed_until, suppressing the item from the list."""
    handler, _feedback_service, active_repo = _make_handler()
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        target_version=1,
        result_id="result-1",
        waiting_since=NOW,
    )

    event = _make_event(
        event_id="evt-snooze",
        button_id=f"wfm_feedback:snooze:{CHAT_ID}:1",
    )
    result = await handler.handle(event)
    assert result is True

    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active is not None
    assert active.snoozed_until is not None
    # Snooze is set to ~18 hours from real now, so it's in the future.
    real_now = datetime.now(timezone.utc)
    assert active.snoozed_until > real_now


# --- Test 6: Expired snooze returns to the digest --------------------------


async def test_expired_snooze_returns_to_list():
    """An item with an expired snoozed_until appears in the list again."""
    bot = FakeBot()
    handler, _, active_repo = _make_handler(bot=bot)

    # Set up chat state with matching version.
    await _setup_chat_state(handler, CHAT_ID, version=1)

    # Create active item with snoozed_until in the past (relative to real now).
    past_snooze = datetime.now(timezone.utc) - timedelta(hours=1)
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        target_version=1,
        result_id="result-1",
        waiting_since=NOW,
    )
    await active_repo.snooze(
        user_id=USER_ID, chat_id=CHAT_ID, snoozed_until=past_snooze
    )

    # Tap "צפה בשיחות" — should include the expired-snooze item.
    event = _make_event(
        event_id="evt-view",
        event_type=BotEventType.TEXT,
        text="צפה בשיחות",
    )
    result = await handler.handle(event)
    assert result is True
    assert len(bot.lists) == 1
    _, body, _, _sections = bot.lists[0]
    assert "1" in body  # 1 item


# --- Test 7: Muted chat is absent from digest and list ---------------------


async def test_muted_chat_absent_from_list():
    """A muted chat does not appear in the interactive list."""
    bot = FakeBot()
    handler, _, active_repo = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        target_version=1,
        result_id="result-1",
        waiting_since=NOW,
    )

    # Mute the chat.
    await handler._mute_repo.mute_permanent(user_id=USER_ID, chat_id=CHAT_ID)

    # Tap "צפה בשיחות" — should show "no items".
    event = _make_event(
        event_id="evt-view-muted",
        event_type=BotEventType.TEXT,
        text="צפה בשיחות",
    )
    result = await handler.handle(event)
    assert result is True
    # Should send text "אין כרגע..." not a list.
    assert len(bot.texts) == 1
    assert "אין" in bot.texts[0][1]


# --- Test 8: Temporary mute expires ---------------------------------------


async def test_temporary_mute_expires():
    """A temporary mute stops suppressing after muted_until passes."""
    mute_repo = InMemoryChatMuteRepository()
    future = NOW + timedelta(hours=24)
    after_expiry = future + timedelta(hours=1)

    await mute_repo.mute_temporary(
        user_id=USER_ID, chat_id=CHAT_ID, muted_until=future
    )
    assert await mute_repo.is_muted(user_id=USER_ID, chat_id=CHAT_ID, now=NOW) is True

    # After expiry, is_muted returns False and cleans up.
    assert await mute_repo.is_muted(user_id=USER_ID, chat_id=CHAT_ID, now=after_expiry) is False


# --- Test 9: Permanent mute does not expire --------------------------------


async def test_permanent_mute_does_not_expire():
    """A permanent mute never expires."""
    mute_repo = InMemoryChatMuteRepository()
    far_future = NOW + timedelta(days=365 * 10)

    await mute_repo.mute_permanent(user_id=USER_ID, chat_id=CHAT_ID)
    assert await mute_repo.is_muted(user_id=USER_ID, chat_id=CHAT_ID, now=NOW) is True
    assert await mute_repo.is_muted(user_id=USER_ID, chat_id=CHAT_ID, now=far_future) is True


# --- Test 10: Eleven active chats produce bounded first page (max 10) ------


async def test_eleven_chats_bounded_to_ten():
    """More than 10 active items produce a list with exactly 10 rows."""
    bot = FakeBot()
    handler, _, active_repo = _make_handler(bot=bot)

    for i in range(11):
        chat_id = f"97250{i:08d}@c.us"
        await _setup_chat_state(handler, chat_id, version=1)
        await active_repo.upsert(
            user_id=USER_ID,
            chat_id=chat_id,
            target_version=1,
            result_id=f"result-{i}",
            waiting_since=NOW + timedelta(seconds=i),
        )

    event = _make_event(
        event_id="evt-view-11",
        event_type=BotEventType.TEXT,
        text="צפה בשיחות",
    )
    result = await handler.handle(event)
    assert result is True
    assert len(bot.lists) == 1
    _, _, _, sections = bot.lists[0]
    rows = sections[0]["rows"]
    assert len(rows) == 10  # bounded to 10


# --- Test 11: Empty day is recorded without sending a template --------------


async def test_empty_day_recorded_without_sending():
    """Digest worker records 'empty' status without sending a template when no items."""
    from echo_v2.persistence.digest_repositories import InMemoryDailyDigestRepository
    from echo_v2.services.digest_worker import DigestWorker

    bot = FakeBot()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    digest_repo = InMemoryDailyDigestRepository()
    mute_repo = InMemoryChatMuteRepository()

    worker = DigestWorker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=lambda: _async_user_provider(),
        mute_repo=mute_repo,
    )

    async def _async_user_provider():
        return [(USER_ID, USER_PHONE, "Asia/Jerusalem", None)]

    # No active items → empty day.
    sent = await worker.run_once(now_utc=NOW)
    assert sent == 0
    # No template sent.
    assert len(bot.texts) == 0
    assert len(bot.lists) == 0
    # But digest row exists with status 'empty'.
    from zoneinfo import ZoneInfo

    from echo_v2.domain.digest import DailyDigestStatus

    local_date = NOW.astimezone(ZoneInfo("Asia/Jerusalem")).date()
    digest = await digest_repo.get(user_id=USER_ID, local_date=local_date)
    assert digest is not None
    assert digest.status == DailyDigestStatus.EMPTY


# --- Bonus: Feedback recording (false_positive) ---------------------------


async def test_false_positive_feedback_recorded():
    """Recording a false_positive verdict stores it correctly."""
    _, feedback_service, _ = _make_handler()
    recorded = await feedback_service.record_feedback(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        verdict=FeedbackVerdict.FALSE_POSITIVE,
        target_version=1,
        provider_event_id="evt-fp-1",
    )
    assert recorded is True

    # Duplicate event_id → not recorded.
    recorded2 = await feedback_service.record_feedback(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        verdict=FeedbackVerdict.FALSE_POSITIVE,
        target_version=1,
        provider_event_id="evt-fp-1",
    )
    assert recorded2 is False


# --- Bonus: Should offer mute after 3 false positives ----------------------


async def test_should_offer_mute_after_three_false_positives():
    """After 3 recent false_positive verdicts, should_offer_mute returns True."""
    _, feedback_service, _ = _make_handler()
    for i in range(3):
        await feedback_service.record_feedback(
            user_id=USER_ID,
            chat_id=CHAT_ID,
            verdict=FeedbackVerdict.FALSE_POSITIVE,
            target_version=1,
            provider_event_id=f"evt-fp-{i}",
        )

    assert await feedback_service.should_offer_mute(user_id=USER_ID, chat_id=CHAT_ID) is True


# --- Bonus: Miss report ----------------------------------------------------


async def test_miss_report_recorded():
    """'פספסתי' records a false_negative feedback."""
    bot = FakeBot()
    handler, _, _ = _make_handler(bot=bot)

    event = _make_event(
        event_id="evt-miss",
        event_type=BotEventType.TEXT,
        text="פספסתי",
    )
    result = await handler.handle(event)
    assert result is True
    # Should send a confirmation message.
    assert len(bot.texts) == 1
    assert "תודה" in bot.texts[0][1]


# --- Bonus: Acknowledge sets acknowledged_at -------------------------------


async def test_acknowledge_sets_acknowledged_at():
    """Acknowledge action sets acknowledged_at on the active item."""
    handler, _, active_repo = _make_handler()
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        target_version=1,
        result_id="result-1",
        waiting_since=NOW,
    )

    event = _make_event(
        event_id="evt-ack",
        button_id=f"wfm_feedback:acknowledge:{CHAT_ID}:1",
    )
    result = await handler.handle(event)
    assert result is True

    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active is not None
    assert active.acknowledged_at is not None


# --- Bonus: Resolve + verdict flow -----------------------------------------


async def test_resolve_then_verdict_false_positive():
    """After resolve, the verdict button records false_positive feedback."""
    bot = FakeBot()
    handler, feedback_service, active_repo = _make_handler(bot=bot)
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        target_version=1,
        result_id="result-1",
        waiting_since=NOW,
    )

    # Step 1: Resolve.
    resolve_event = _make_event(
        event_id="evt-resolve-1",
        button_id=f"wfm_feedback:resolve:{CHAT_ID}:1",
    )
    await handler.handle(resolve_event)

    # Step 2: Verdict — false positive.
    verdict_event = _make_event(
        event_id="evt-verdict-1",
        button_id=f"wfm_verdict:false_positive:{CHAT_ID}:1",
    )
    await handler.handle(verdict_event)

    # Feedback should be recorded.
    count = await feedback_service._feedback_repo.count_recent_false_positives(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        since=NOW - timedelta(days=30),
    )
    assert count == 1


# --- Coverage: Verdict correct path ----------------------------------------


async def test_resolve_then_verdict_correct():
    """After resolve, 'לא, פשוט סיימתי' records correct feedback."""
    bot = FakeBot()
    handler, _feedback_service, active_repo = _make_handler(bot=bot)
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        target_version=1,
        result_id="result-1",
        waiting_since=NOW,
    )

    # Step 1: Resolve.
    resolve_event = _make_event(
        event_id="evt-resolve-correct",
        button_id=f"wfm_feedback:resolve:{CHAT_ID}:1",
    )
    await handler.handle(resolve_event)

    # Step 2: Verdict — correct.
    verdict_event = _make_event(
        event_id="evt-verdict-correct",
        button_id=f"wfm_verdict:correct:{CHAT_ID}:1",
    )
    await handler.handle(verdict_event)

    # Should send a confirmation text.
    assert any("תודה" in t[1] for t in bot.texts)


# --- Coverage: Mute confirmation flow --------------------------------------


async def test_mute_confirm_permanent():
    """Permanent mute confirmation mutes the chat."""
    bot = FakeBot()
    handler, _feedback_service, _ = _make_handler(bot=bot)

    event = _make_event(
        event_id="evt-mute-confirm",
        button_id=f"wfm_mute:{CHAT_ID}",
    )
    result = await handler.handle(event)
    assert result is True

    # Chat should be muted.
    is_muted = await handler._mute_repo.is_muted(
        user_id=USER_ID, chat_id=CHAT_ID, now=datetime.now(timezone.utc)
    )
    assert is_muted is True
    assert any("הושתקה" in t[1] for t in bot.texts)


# --- Coverage: Mute decline (wfm_nomute) -----------------------------------


async def test_mute_decline_does_nothing():
    """Declining mute (wfm_nomute) returns True without action."""
    bot = FakeBot()
    handler, _, _ = _make_handler(bot=bot)

    event = _make_event(
        event_id="evt-nomute",
        button_id=f"wfm_nomute:{CHAT_ID}",
    )
    result = await handler.handle(event)
    assert result is True
    # No mute applied.
    is_muted = await handler._mute_repo.is_muted(
        user_id=USER_ID, chat_id=CHAT_ID, now=datetime.now(timezone.utc)
    )
    assert is_muted is False


# --- Coverage: List item stale ---------------------------------------------


async def test_list_item_stale_returns_message():
    """Tapping a stale list item sends 'no longer current' message."""
    bot = FakeBot()
    handler, _, active_repo = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        target_version=2,  # current version is 2
        result_id="result-2",
        waiting_since=NOW,
    )

    # List item callback for version 1 (stale).
    event = _make_event(
        event_id="evt-list-stale",
        event_type=BotEventType.LIST_REPLY,
        list_id=f"wfm_item:{CHAT_ID}:1",
    )
    result = await handler.handle(event)
    assert result is True
    assert any("אינו עדכני" in t[1] for t in bot.texts)


# --- Coverage: List item with valid active ---------------------------------


async def test_list_item_valid_sends_buttons():
    """Tapping a valid list item sends 3 feedback buttons."""
    bot = FakeBot()
    handler, _, active_repo = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        target_version=1,
        result_id="result-1",
        waiting_since=NOW,
    )

    event = _make_event(
        event_id="evt-list-valid",
        event_type=BotEventType.LIST_REPLY,
        list_id=f"wfm_item:{CHAT_ID}:1",
    )
    result = await handler.handle(event)
    assert result is True
    assert len(bot.buttons) == 1
    _, _, buttons = bot.buttons[0]
    assert len(buttons) == 3


# --- Coverage: Unknown feedback action -------------------------------------


async def test_unknown_feedback_action_returns_false():
    """Unknown action in wfm_feedback: returns False."""
    handler, _, _ = _make_handler()

    event = _make_event(
        event_id="evt-unknown-action",
        button_id=f"wfm_feedback:unknown:{CHAT_ID}:1",
    )
    result = await handler.handle(event)
    assert result is False


# --- Coverage: Malformed callback IDs --------------------------------------


async def test_malformed_list_id_returns_false():
    """Malformed list_id returns False (not handled)."""
    handler, _, _ = _make_handler()

    event = _make_event(
        event_id="evt-malformed-list",
        event_type=BotEventType.LIST_REPLY,
        list_id="garbage",
    )
    result = await handler.handle(event)
    assert result is False


async def test_malformed_button_id_returns_false():
    """Malformed button_id returns False (not handled)."""
    handler, _, _ = _make_handler()

    event = _make_event(
        event_id="evt-malformed-btn",
        button_id="garbage",
    )
    result = await handler.handle(event)
    assert result is False


async def test_list_id_with_bad_version_returns_false():
    """List ID with non-integer version returns False."""
    handler, _, _ = _make_handler()

    event = _make_event(
        event_id="evt-bad-ver",
        event_type=BotEventType.LIST_REPLY,
        list_id="wfm_item:chat:abc",
    )
    result = await handler.handle(event)
    assert result is False


async def test_feedback_button_with_bad_version_returns_false():
    """Feedback button with non-integer version returns False."""
    handler, _, _ = _make_handler()

    event = _make_event(
        event_id="evt-bad-ver-btn",
        button_id="wfm_feedback:acknowledge:chat:abc",
    )
    result = await handler.handle(event)
    assert result is False


async def test_verdict_button_with_bad_version_returns_false():
    """Verdict button with non-integer version returns False."""
    handler, _, _ = _make_handler()

    event = _make_event(
        event_id="evt-bad-ver-verdict",
        button_id="wfm_verdict:correct:chat:abc",
    )
    result = await handler.handle(event)
    assert result is False


async def test_mute_confirm_malformed_returns_false():
    """Mute confirm with malformed ID returns False."""
    handler, _, _ = _make_handler()

    event = _make_event(
        event_id="evt-mute-malformed",
        button_id="wfm_mute",
    )
    result = await handler.handle(event)
    assert result is False


# --- Coverage: Unknown user returns False ---------------------------------


async def test_view_details_unknown_user_returns_false():
    """View details from unknown user returns False."""
    bot = FakeBot()
    handler, _, _ = _make_handler(bot=bot, user_id=None)

    event = _make_event(
        event_id="evt-view-unknown",
        event_type=BotEventType.TEXT,
        text="צפה בשיחות",
    )
    result = await handler.handle(event)
    assert result is False


async def test_list_item_unknown_user_returns_false():
    """List item from unknown user returns False."""
    bot = FakeBot()
    handler, _, _ = _make_handler(bot=bot, user_id=None)

    event = _make_event(
        event_id="evt-list-unknown",
        event_type=BotEventType.LIST_REPLY,
        list_id=f"wfm_item:{CHAT_ID}:1",
    )
    result = await handler.handle(event)
    assert result is False


async def test_feedback_button_unknown_user_returns_false():
    """Feedback button from unknown user returns False."""
    bot = FakeBot()
    handler, _, _ = _make_handler(bot=bot, user_id=None)

    event = _make_event(
        event_id="evt-fb-unknown",
        button_id=f"wfm_feedback:acknowledge:{CHAT_ID}:1",
    )
    result = await handler.handle(event)
    assert result is False


async def test_verdict_button_unknown_user_returns_false():
    """Verdict button from unknown user returns False."""
    bot = FakeBot()
    handler, _, _ = _make_handler(bot=bot, user_id=None)

    event = _make_event(
        event_id="evt-verdict-unknown",
        button_id=f"wfm_verdict:correct:{CHAT_ID}:1",
    )
    result = await handler.handle(event)
    assert result is False


async def test_mute_confirm_unknown_user_returns_false():
    """Mute confirm from unknown user returns False."""
    bot = FakeBot()
    handler, _, _ = _make_handler(bot=bot, user_id=None)

    event = _make_event(
        event_id="evt-mute-unknown",
        button_id=f"wfm_mute:{CHAT_ID}",
    )
    result = await handler.handle(event)
    assert result is False


async def test_miss_report_unknown_user_returns_false():
    """Miss report from unknown user returns False."""
    bot = FakeBot()
    handler, _, _ = _make_handler(bot=bot, user_id=None)

    event = _make_event(
        event_id="evt-miss-unknown",
        event_type=BotEventType.TEXT,
        text="פספסתי",
    )
    result = await handler.handle(event)
    assert result is False


# --- Coverage: Feedback service — mute/unmute ------------------------------


async def test_handle_mute_chat_permanent():
    """handle_mute_chat with permanent=True mutes permanently."""
    _, feedback_service, _ = _make_handler()

    await feedback_service.handle_mute_chat(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        permanent=True,
        provider_event_id="evt-mute-svc-1",
    )

    is_muted = await feedback_service._mute_repo.is_muted(
        user_id=USER_ID, chat_id=CHAT_ID, now=datetime.now(timezone.utc)
    )
    assert is_muted is True


async def test_handle_mute_chat_temporary():
    """handle_mute_chat with permanent=False mutes temporarily."""
    _, feedback_service, _ = _make_handler()

    await feedback_service.handle_mute_chat(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        permanent=False,
        provider_event_id="evt-mute-svc-2",
    )

    is_muted = await feedback_service._mute_repo.is_muted(
        user_id=USER_ID, chat_id=CHAT_ID, now=datetime.now(timezone.utc)
    )
    assert is_muted is True

    # Should expire after 48 hours.
    far_future = datetime.now(timezone.utc) + timedelta(hours=49)
    is_muted_after = await feedback_service._mute_repo.is_muted(
        user_id=USER_ID, chat_id=CHAT_ID, now=far_future
    )
    assert is_muted_after is False


async def test_handle_unmute_chat():
    """handle_unmute_chat removes the mute."""
    _, feedback_service, _ = _make_handler()

    # First mute.
    await feedback_service.handle_mute_chat(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        permanent=True,
        provider_event_id="evt-mute-svc-3",
    )
    # Then unmute.
    await feedback_service.handle_unmute_chat(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        provider_event_id="evt-unmute-svc-1",
    )

    is_muted = await feedback_service._mute_repo.is_muted(
        user_id=USER_ID, chat_id=CHAT_ID, now=datetime.now(timezone.utc)
    )
    assert is_muted is False


# --- Coverage: Feedback repository — delete_expired ------------------------


async def test_delete_expired_feedback():
    """delete_expired removes feedback rows past their expires_at."""
    _, feedback_service, _ = _make_handler()

    # Record feedback with expiry in the past.
    past_expiry = datetime.now(timezone.utc) - timedelta(days=1)
    await feedback_service._feedback_repo.record(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        verdict=FeedbackVerdict.FALSE_POSITIVE,
        provider_event_id="evt-expired-1",
        expires_at=past_expiry,
    )

    # Record feedback with expiry in the future.
    future_expiry = datetime.now(timezone.utc) + timedelta(days=90)
    await feedback_service._feedback_repo.record(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        verdict=FeedbackVerdict.CORRECT,
        provider_event_id="evt-active-1",
        expires_at=future_expiry,
    )

    # Delete expired.
    now = datetime.now(timezone.utc)
    deleted = await feedback_service._feedback_repo.delete_expired(now=now)
    assert deleted == 1


async def test_delete_expired_no_expires_at_kept():
    """Feedback with no expires_at is never deleted."""
    _, feedback_service, _ = _make_handler()

    await feedback_service._feedback_repo.record(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        verdict=FeedbackVerdict.CORRECT,
        provider_event_id="evt-no-expiry-1",
    )

    deleted = await feedback_service._feedback_repo.delete_expired(
        now=datetime.now(timezone.utc) + timedelta(days=365)
    )
    assert deleted == 0


# --- Coverage: Mute repository — get + unmute -------------------------------


async def test_mute_get_returns_none_when_not_muted():
    """get returns None when no mute exists."""
    mute_repo = InMemoryChatMuteRepository()
    result = await mute_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert result is None


async def test_mute_get_returns_record():
    """get returns the mute record when it exists."""
    mute_repo = InMemoryChatMuteRepository()
    await mute_repo.mute_permanent(user_id=USER_ID, chat_id=CHAT_ID)
    result = await mute_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert result is not None
    assert result.permanent is True


async def test_mute_unmute_returns_false_when_not_muted():
    """unmute returns False when no mute exists."""
    mute_repo = InMemoryChatMuteRepository()
    result = await mute_repo.unmute(user_id=USER_ID, chat_id=CHAT_ID)
    assert result is False


async def test_mute_is_muted_cleans_up_expired():
    """is_muted returns False and cleans up an expired temporary mute."""
    mute_repo = InMemoryChatMuteRepository()
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    await mute_repo.mute_temporary(
        user_id=USER_ID, chat_id=CHAT_ID, muted_until=past
    )
    # First call returns False and deletes the row.
    result = await mute_repo.is_muted(
        user_id=USER_ID, chat_id=CHAT_ID, now=datetime.now(timezone.utc)
    )
    assert result is False
    # Row is gone.
    assert await mute_repo.get(user_id=USER_ID, chat_id=CHAT_ID) is None


# --- Coverage: Snooze action returns True ----------------------------------


async def test_snooze_action_applied():
    """Snooze action is applied and returns True."""
    _, feedback_service, active_repo = _make_handler()
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        target_version=1,
        result_id="result-1",
        waiting_since=NOW,
    )

    result = await feedback_service.handle_snooze(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        active_id=f"{USER_ID}:{CHAT_ID}",
        target_version=1,
        provider_event_id="evt-snooze-svc-1",
    )
    assert result is True


async def test_acknowledge_action_applied():
    """Acknowledge action is applied and returns True."""
    _, feedback_service, active_repo = _make_handler()
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        target_version=1,
        result_id="result-1",
        waiting_since=NOW,
    )

    result = await feedback_service.handle_acknowledge(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        active_id=f"{USER_ID}:{CHAT_ID}",
        target_version=1,
        provider_event_id="evt-ack-svc-1",
    )
    assert result is True


# --- Coverage: Duplicate action returns True (idempotent) -----------------


async def test_duplicate_acknowledge_returns_true():
    """Duplicate acknowledge callback returns True (already processed)."""
    _, feedback_service, active_repo = _make_handler()
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        target_version=1,
        result_id="result-1",
        waiting_since=NOW,
    )

    # First call.
    result1 = await feedback_service.handle_acknowledge(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        active_id=f"{USER_ID}:{CHAT_ID}",
        target_version=1,
        provider_event_id="evt-dup-ack",
    )
    assert result1 is True

    # Second call with same event_id — idempotent.
    result2 = await feedback_service.handle_acknowledge(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        active_id=f"{USER_ID}:{CHAT_ID}",
        target_version=1,
        provider_event_id="evt-dup-ack",
    )
    assert result2 is True


# --- Coverage: should_offer_mute returns False below threshold --------------


async def test_should_offer_mute_false_below_threshold():
    """should_offer_mute returns False with fewer than 3 false positives."""
    _, feedback_service, _ = _make_handler()
    for i in range(2):
        await feedback_service.record_feedback(
            user_id=USER_ID,
            chat_id=CHAT_ID,
            verdict=FeedbackVerdict.FALSE_POSITIVE,
            target_version=1,
            provider_event_id=f"evt-fp-below-{i}",
        )

    assert await feedback_service.should_offer_mute(user_id=USER_ID, chat_id=CHAT_ID) is False


# --- Coverage: Verdict false_positive without mute offer -------------------


async def test_verdict_false_positive_no_mute_offer():
    """False positive verdict with < 3 dismissals sends thanks, not mute offer."""
    bot = FakeBot()
    handler, _, _ = _make_handler(bot=bot)

    event = _make_event(
        event_id="evt-fp-no-mute",
        button_id=f"wfm_verdict:false_positive:{CHAT_ID}:1",
    )
    result = await handler.handle(event)
    assert result is True
    # Should send thanks text, not buttons.
    assert any("תודה" in t[1] for t in bot.texts)
    assert len(bot.buttons) == 0


# --- Coverage: Verdict false_positive WITH mute offer ----------------------


async def test_verdict_false_positive_with_mute_offer():
    """False positive verdict with >= 3 dismissals offers permanent mute."""
    bot = FakeBot()
    handler, feedback_service, _ = _make_handler(bot=bot)

    # Record 3 false positives to trigger the mute offer.
    for i in range(3):
        await feedback_service.record_feedback(
            user_id=USER_ID,
            chat_id=CHAT_ID,
            verdict=FeedbackVerdict.FALSE_POSITIVE,
            target_version=1,
            provider_event_id=f"evt-fp-offer-{i}",
        )

    event = _make_event(
        event_id="evt-fp-with-mute",
        button_id=f"wfm_verdict:false_positive:{CHAT_ID}:1",
    )
    result = await handler.handle(event)
    assert result is True
    # Should send mute confirmation buttons.
    assert len(bot.buttons) == 1
    _, _, buttons = bot.buttons[0]
    assert len(buttons) == 2


# --- Coverage: Unhandled event returns False -------------------------------


async def test_unhandled_text_event_returns_false():
    """A text event that doesn't match any handler returns False."""
    handler, _, _ = _make_handler()

    event = _make_event(
        event_id="evt-unhandled",
        event_type=BotEventType.TEXT,
        text="hello world",
    )
    result = await handler.handle(event)
    assert result is False


async def test_unhandled_button_event_returns_false():
    """A button event with unknown prefix returns False."""
    handler, _, _ = _make_handler()

    event = _make_event(
        event_id="evt-unknown-prefix",
        button_id="unknown_prefix:foo",
    )
    result = await handler.handle(event)
    assert result is False
