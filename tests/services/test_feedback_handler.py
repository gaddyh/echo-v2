"""Tests for FeedbackHandler public command methods.

These are the methods called by the BotCommandRouter (the public
``handle_responsibility_*`` / ``handle_digest_open`` / ``handle_list_done``
entry points), plus edge cases for the private ``_handle_digest_request``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from echo_v2.domain.feedback import HandlingOutcome
from echo_v2.persistence.chat_repositories import (
    InMemoryChatStateRepository,
    InMemoryMessageRepository,
    InMemoryWaitingForMeActiveRepository,
    InMemoryWaitingForMeResultRepository,
)
from echo_v2.persistence.contacts import InMemoryContactRepository
from echo_v2.persistence.feedback_repositories import InMemoryChatMuteRepository
from echo_v2.ports.bot import BotEvent, BotEventType
from echo_v2.services.feedback_handler import (
    _LIST_DONE_REPLY,
    _STALE_MESSAGE,
    FeedbackHandler,
)
from echo_v2.services.waiting_list_query import WaitingListQueryService

pytestmark = pytest.mark.asyncio

USER_PHONE = "972501234567"
USER_ID = "user-1"
ACTIVE_ID = "active-1"
EVENT_ID = "evt-1"


def _make_event(
    event_id: str = EVENT_ID,
    event_type: BotEventType = BotEventType.BUTTON_REPLY,
    text: str | None = None,
    button_id: str | None = None,
    user_phone: str = USER_PHONE,
) -> BotEvent:
    return BotEvent(
        event_id=event_id,
        user_phone=user_phone,
        type=event_type,
        text=text,
        button_id=button_id,
    )


_NOT_SET = object()


def _make_handler(
    *,
    user_resolver_return=_NOT_SET,
    action_service=None,
    bot=None,
    token_service=_NOT_SET,
    active_repo=None,
    base_url: str = "https://echo.example.com",
) -> tuple[FeedbackHandler, object, object, object, object]:
    """Build a FeedbackHandler with mocked services and in-memory repos."""
    if user_resolver_return is _NOT_SET:
        user_resolver_return = (USER_ID, "active", None)

    user_resolver = AsyncMock()
    user_resolver.resolve = AsyncMock(return_value=user_resolver_return)

    action_service = action_service or AsyncMock()
    bot = bot or AsyncMock()
    if token_service is _NOT_SET:
        token_service = AsyncMock()

    active_repo = active_repo or InMemoryWaitingForMeActiveRepository()
    result_repo = InMemoryWaitingForMeResultRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    mute_repo = InMemoryChatMuteRepository()

    query_service = WaitingListQueryService(
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        mute_repo=mute_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        result_repo=result_repo,
    )

    handler = FeedbackHandler(
        bot=bot,
        action_service=action_service,
        feedback_service=AsyncMock(),
        active_repo=active_repo,
        result_repo=result_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        mute_repo=mute_repo,
        user_resolver=user_resolver,
        query_service=query_service,
        token_service=token_service,
        base_url=base_url,
    )
    return handler, action_service, bot, user_resolver, active_repo


# --- handle_responsibility_done --------------------------------------------


async def test_handle_responsibility_done_user_not_found_returns_early():
    """Unknown user → resolver returns None → no action taken."""
    handler, action_service, bot, user_resolver, _ = _make_handler(
        user_resolver_return=None,
    )
    event = _make_event()

    await handler.handle_responsibility_done(event, ACTIVE_ID)

    user_resolver.resolve.assert_awaited_once_with(USER_PHONE)
    action_service.handled.assert_not_awaited()
    bot.send_text.assert_not_awaited()


async def test_handle_responsibility_done_success_calls_handled_and_sends_response():
    """Known user → action_service.handled called, response sent."""
    action_service = AsyncMock()
    action_service.handled = AsyncMock(return_value=HandlingOutcome.APPLIED)
    handler, action_service, bot, _, _ = _make_handler(action_service=action_service)
    event = _make_event()

    await handler.handle_responsibility_done(event, ACTIVE_ID)

    action_service.handled.assert_awaited_once_with(
        user_id=USER_ID,
        active_id=ACTIVE_ID,
        target_version=0,
        provider_message_id=EVENT_ID,
    )
    bot.send_text.assert_awaited_once()
    phone, text = bot.send_text.await_args.args
    assert phone == USER_PHONE
    assert "סימנתי שטופל" in text


async def test_handle_responsibility_done_stale_outcome_still_sends_response():
    """STALE outcome → response still sent (stale message)."""
    action_service = AsyncMock()
    action_service.handled = AsyncMock(return_value=HandlingOutcome.STALE)
    handler, action_service, bot, _, _ = _make_handler(action_service=action_service)
    event = _make_event()

    await handler.handle_responsibility_done(event, ACTIVE_ID)

    action_service.handled.assert_awaited_once()
    bot.send_text.assert_awaited_once()
    phone, text = bot.send_text.await_args.args
    assert phone == USER_PHONE
    assert text == _STALE_MESSAGE


# --- handle_responsibility_snooze -------------------------------------------


async def test_handle_responsibility_snooze_user_not_found_returns_early():
    """Unknown user → returns early."""
    handler, action_service, bot, user_resolver, _ = _make_handler(
        user_resolver_return=None,
    )
    event = _make_event()

    await handler.handle_responsibility_snooze(event, ACTIVE_ID, preset=None)

    user_resolver.resolve.assert_awaited_once_with(USER_PHONE)
    action_service.snooze.assert_not_awaited()
    bot.send_text.assert_not_awaited()


async def test_handle_responsibility_snooze_success_with_preset_none():
    """Success with preset=None → snooze called with preset=None."""
    action_service = AsyncMock()
    action_service.snooze = AsyncMock(return_value=HandlingOutcome.APPLIED)
    handler, action_service, bot, _, _ = _make_handler(action_service=action_service)
    event = _make_event()

    await handler.handle_responsibility_snooze(event, ACTIVE_ID, preset=None)

    action_service.snooze.assert_awaited_once_with(
        user_id=USER_ID,
        active_id=ACTIVE_ID,
        target_version=0,
        provider_message_id=EVENT_ID,
        snooze_preset=None,
    )
    bot.send_text.assert_awaited_once()
    phone, text = bot.send_text.await_args.args
    assert phone == USER_PHONE
    assert "אזכיר לך" in text


async def test_handle_responsibility_snooze_success_with_preset_1h():
    """Success with preset='1h' → snooze called with preset='1h'."""
    action_service = AsyncMock()
    action_service.snooze = AsyncMock(return_value=HandlingOutcome.APPLIED)
    handler, action_service, bot, _, _ = _make_handler(action_service=action_service)
    event = _make_event()

    await handler.handle_responsibility_snooze(event, ACTIVE_ID, preset="1h")

    action_service.snooze.assert_awaited_once_with(
        user_id=USER_ID,
        active_id=ACTIVE_ID,
        target_version=0,
        provider_message_id=EVENT_ID,
        snooze_preset="1h",
    )
    bot.send_text.assert_awaited_once()


async def test_handle_responsibility_snooze_not_found_outcome_still_sends_response():
    """NOT_FOUND outcome → response still sent (stale message)."""
    action_service = AsyncMock()
    action_service.snooze = AsyncMock(return_value=HandlingOutcome.NOT_FOUND)
    handler, action_service, bot, _, _ = _make_handler(action_service=action_service)
    event = _make_event()

    await handler.handle_responsibility_snooze(event, ACTIVE_ID, preset=None)

    action_service.snooze.assert_awaited_once()
    bot.send_text.assert_awaited_once()
    _phone, text = bot.send_text.await_args.args
    assert text == _STALE_MESSAGE


# --- handle_responsibility_dismiss ------------------------------------------


async def test_handle_responsibility_dismiss_user_not_found_returns_early():
    """Unknown user → returns early."""
    handler, action_service, bot, user_resolver, _ = _make_handler(
        user_resolver_return=None,
    )
    event = _make_event()

    await handler.handle_responsibility_dismiss(event, ACTIVE_ID, reason=None)

    user_resolver.resolve.assert_awaited_once_with(USER_PHONE)
    action_service.dismiss_not_waiting.assert_not_awaited()
    action_service.dismiss_not_interested.assert_not_awaited()
    bot.send_buttons.assert_not_awaited()


async def test_handle_responsibility_dismiss_reason_none_opens_submenu():
    """reason=None → opens dismiss submenu (send_buttons)."""
    handler, action_service, bot, _, _ = _make_handler()
    event = _make_event()

    await handler.handle_responsibility_dismiss(event, ACTIVE_ID, reason=None)

    action_service.dismiss_not_waiting.assert_not_awaited()
    action_service.dismiss_not_interested.assert_not_awaited()
    bot.send_buttons.assert_awaited_once()
    call = bot.send_buttons.await_args
    phone = call.args[0]
    body_text = call.kwargs["body_text"]
    buttons = call.kwargs["buttons"]
    assert phone == USER_PHONE
    assert "למה לא צריך?" in body_text
    ids = [b["id"] for b in buttons]
    assert f"dismiss:{ACTIVE_ID}:not_waiting" in ids
    assert f"dismiss:{ACTIVE_ID}:not_interested" in ids


async def test_handle_responsibility_dismiss_reason_not_waiting_calls_dismiss():
    """reason='not_waiting' → calls dismiss_not_waiting, sends response."""
    action_service = AsyncMock()
    action_service.dismiss_not_waiting = AsyncMock(
        return_value=HandlingOutcome.APPLIED,
    )
    handler, action_service, bot, _, _ = _make_handler(action_service=action_service)
    event = _make_event()

    await handler.handle_responsibility_dismiss(event, ACTIVE_ID, reason="not_waiting")

    action_service.dismiss_not_waiting.assert_awaited_once_with(
        user_id=USER_ID,
        active_id=ACTIVE_ID,
        target_version=0,
        provider_message_id=EVENT_ID,
    )
    action_service.dismiss_not_interested.assert_not_awaited()
    bot.send_text.assert_awaited_once()
    _phone, text = bot.send_text.await_args.args
    assert "משוב" in text


async def test_handle_responsibility_dismiss_reason_not_interested_calls_dismiss():
    """reason='not_interested' → calls dismiss_not_interested, sends response."""
    action_service = AsyncMock()
    action_service.dismiss_not_interested = AsyncMock(
        return_value=HandlingOutcome.APPLIED,
    )
    handler, action_service, bot, _, _ = _make_handler(action_service=action_service)
    event = _make_event()

    await handler.handle_responsibility_dismiss(
        event, ACTIVE_ID, reason="not_interested",
    )

    action_service.dismiss_not_interested.assert_awaited_once_with(
        user_id=USER_ID,
        active_id=ACTIVE_ID,
        target_version=0,
        provider_message_id=EVENT_ID,
    )
    action_service.dismiss_not_waiting.assert_not_awaited()
    bot.send_text.assert_awaited_once()
    _phone, text = bot.send_text.await_args.args
    assert "הוסר מהרשימה" in text


async def test_handle_responsibility_dismiss_unknown_reason_logs_warning_no_action():
    """Unknown reason → logs warning, no action taken."""
    handler, action_service, bot, _, _ = _make_handler()
    event = _make_event()

    await handler.handle_responsibility_dismiss(event, ACTIVE_ID, reason="unknown")

    action_service.dismiss_not_waiting.assert_not_awaited()
    action_service.dismiss_not_interested.assert_not_awaited()
    bot.send_text.assert_not_awaited()
    bot.send_buttons.assert_not_awaited()


async def test_handle_responsibility_dismiss_not_waiting_stale_still_sends_response():
    """dismiss_not_waiting returns STALE → response still sent (stale message)."""
    action_service = AsyncMock()
    action_service.dismiss_not_waiting = AsyncMock(
        return_value=HandlingOutcome.STALE,
    )
    handler, action_service, bot, _, _ = _make_handler(action_service=action_service)
    event = _make_event()

    await handler.handle_responsibility_dismiss(event, ACTIVE_ID, reason="not_waiting")

    action_service.dismiss_not_waiting.assert_awaited_once()
    bot.send_text.assert_awaited_once()
    _phone, text = bot.send_text.await_args.args
    assert text == _STALE_MESSAGE


# --- handle_responsibility_list ---------------------------------------------


async def test_handle_responsibility_list_delegates_to_view_details():
    """handle_responsibility_list delegates to _handle_view_details."""
    handler, _, bot, _, _active_repo = _make_handler()
    # No active items → sends the empty message.
    event = _make_event(event_type=BotEventType.TEXT, text="צפה בשיחות")

    await handler.handle_responsibility_list(event)

    bot.send_text.assert_awaited_once()
    phone, text = bot.send_text.await_args.args
    assert phone == USER_PHONE
    assert "אין כרגע" in text


async def test_handle_responsibility_list_with_items_sends_cards():
    """With active items, handle_responsibility_list sends cards."""
    from datetime import datetime, timezone

    from echo_v2.ports.whatsapp import MessageDirection

    handler, _, bot, _, active_repo = _make_handler()
    await handler._chat_state_repo.upsert_on_message(
        user_id=USER_ID,
        chat_id="972508765432@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=datetime.now(timezone.utc),
        next_analysis_at=None,
    )
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id="972508765432@c.us",
        target_version=1,
        result_id="result-1",
        waiting_since=datetime.now(timezone.utc),
    )
    event = _make_event(event_type=BotEventType.TEXT, text="צפה בשיחות")

    await handler.handle_responsibility_list(event)

    bot.send_buttons.assert_awaited_once()
    call = bot.send_buttons.await_args
    buttons = call.kwargs["buttons"]
    assert len(buttons) == 3


# --- handle_digest_open -----------------------------------------------------


async def test_handle_digest_open_delegates_to_digest_request():
    """handle_digest_open delegates to _handle_digest_request."""
    token_service = AsyncMock()
    token_service.issue = AsyncMock(return_value=("session-1", "tok-1"))
    handler, _, bot, _, _ = _make_handler(token_service=token_service)
    # No active items → empty message (no token issued).
    event = _make_event(event_type=BotEventType.TEXT, text="סיכום חדש")

    await handler.handle_digest_open(event)

    bot.send_text.assert_awaited_once()
    phone, text = bot.send_text.await_args.args
    assert phone == USER_PHONE
    assert "אין כרגע" in text


# --- handle_list_done -------------------------------------------------------


async def test_handle_list_done_sends_list_done_reply():
    """handle_list_done sends the _LIST_DONE_REPLY text."""
    handler, _, bot, _, _ = _make_handler()
    event = _make_event(event_type=BotEventType.TEXT)

    await handler.handle_list_done(event)

    bot.send_text.assert_awaited_once_with(USER_PHONE, _LIST_DONE_REPLY)


# --- _handle_digest_request edge cases --------------------------------------


async def test_digest_request_no_active_items_sends_empty_message():
    """No active items → sends the 'no waiting' message."""
    token_service = AsyncMock()
    token_service.issue = AsyncMock(return_value=("session-1", "tok-1"))
    handler, _, bot, _, _ = _make_handler(token_service=token_service)
    event = _make_event(event_type=BotEventType.TEXT, text="סיכום חדש")

    result = await handler._handle_digest_request(event)

    assert result is True
    bot.send_text.assert_awaited_once()
    _phone, text = bot.send_text.await_args.args
    assert "אין כרגע" in text
    token_service.issue.assert_not_awaited()


async def test_digest_request_token_service_none_sends_error():
    """token_service is None → sends error message, returns False."""
    from datetime import datetime, timezone

    from echo_v2.ports.whatsapp import MessageDirection

    handler, _, bot, _, active_repo = _make_handler(token_service=None)
    # Set up an active item so we reach the token issuance branch.
    await handler._chat_state_repo.upsert_on_message(
        user_id=USER_ID,
        chat_id="972508765432@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=datetime.now(timezone.utc),
        next_analysis_at=None,
    )
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id="972508765432@c.us",
        target_version=1,
        result_id="result-1",
        waiting_since=datetime.now(timezone.utc),
    )
    event = _make_event(event_type=BotEventType.TEXT, text="סיכום חדש")

    result = await handler._handle_digest_request(event)

    assert result is False
    bot.send_text.assert_awaited_once()
    _phone, text = bot.send_text.await_args.args
    assert "שגיאה" in text


async def test_digest_request_token_issue_raises_sends_error():
    """token_service.issue raises → sends error message, returns True."""
    from datetime import datetime, timezone

    from echo_v2.ports.whatsapp import MessageDirection

    token_service = AsyncMock()
    token_service.issue = AsyncMock(side_effect=RuntimeError("down"))
    handler, _, bot, _, active_repo = _make_handler(token_service=token_service)
    await handler._chat_state_repo.upsert_on_message(
        user_id=USER_ID,
        chat_id="972508765432@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=datetime.now(timezone.utc),
        next_analysis_at=None,
    )
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id="972508765432@c.us",
        target_version=1,
        result_id="result-1",
        waiting_since=datetime.now(timezone.utc),
    )
    event = _make_event(event_type=BotEventType.TEXT, text="סיכום חדש")

    result = await handler._handle_digest_request(event)

    assert result is True
    token_service.issue.assert_awaited_once_with(USER_ID)
    bot.send_text.assert_awaited_once()
    _phone, text = bot.send_text.await_args.args
    assert "שגיאה" in text


async def test_digest_request_success_sends_link_with_count():
    """Success → sends link with count."""
    from datetime import datetime, timezone

    from echo_v2.ports.whatsapp import MessageDirection

    token_service = AsyncMock()
    token_service.issue = AsyncMock(return_value=("session-1", "tok-1"))
    handler, _, bot, _, active_repo = _make_handler(
        token_service=token_service, base_url="https://echo.example.com",
    )
    await handler._chat_state_repo.upsert_on_message(
        user_id=USER_ID,
        chat_id="972508765432@c.us",
        direction=MessageDirection.INBOUND,
        observed_at=datetime.now(timezone.utc),
        next_analysis_at=None,
    )
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id="972508765432@c.us",
        target_version=1,
        result_id="result-1",
        waiting_since=datetime.now(timezone.utc),
    )
    event = _make_event(event_type=BotEventType.TEXT, text="סיכום חדש")

    result = await handler._handle_digest_request(event)

    assert result is True
    token_service.issue.assert_awaited_once_with(USER_ID)
    bot.send_text.assert_awaited_once()
    phone, text = bot.send_text.await_args.args
    assert phone == USER_PHONE
    assert "1 שיחות" in text
    assert "https://echo.example.com/q/tok-1" in text
