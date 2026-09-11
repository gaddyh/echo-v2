"""Tests for the feedback flyloop — card-based 2-button design.

Tests the new flow:
1. Template button → sends up to 5 individual cards with 2 gateway buttons.
2. Action menu → 3 action buttons (acknowledge/snooze/resolve).
3. Feedback menu → 3 feedback buttons (correct/false_positive/uncertain).
4. Stale action → rejected with message.
5. Stale feedback → still stored.
6. Duplicate callback → idempotent.
7. Mute offer after 3+ false positives.
8. Miss reporting (פספסתי).
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


# --- View details: sends individual cards -----------------------------------


async def test_view_details_sends_cards():
    """Tapping 'צפה בשיחות' sends individual cards with 2 gateway buttons."""
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
        event_id="evt-view",
        event_type=BotEventType.TEXT,
        text="צפה בשיחות",
    )
    result = await handler.handle(event)
    assert result is True
    # Should send 1 card with 2 buttons.
    assert len(bot.buttons) == 1
    _, _body, buttons = bot.buttons[0]
    assert len(buttons) == 2
    assert buttons[0]["title"] == "מה לעשות"
    assert buttons[1]["title"] == "משוב ל־Echo"
    # Callback IDs should contain chat_id and version.
    assert f"menu_action:{CHAT_ID}:1" == buttons[0]["id"]
    assert f"menu_feedback:{CHAT_ID}:1" == buttons[1]["id"]


async def test_view_details_empty_sends_text():
    """No active items → sends 'no items' text."""
    bot = FakeBot()
    handler, _, _ = _make_handler(bot=bot)

    event = _make_event(
        event_id="evt-view-empty",
        event_type=BotEventType.TEXT,
        text="צפה בשיחות",
    )
    result = await handler.handle(event)
    assert result is True
    assert any("אין" in t[1] for t in bot.texts)
    assert len(bot.buttons) == 0


async def test_view_details_max_5_cards():
    """More than 5 active items → only 5 cards sent."""
    bot = FakeBot()
    handler, _, active_repo = _make_handler(bot=bot)
    for i in range(7):
        cid = f"97250{i:07d}@c.us"
        await _setup_chat_state(handler, cid, version=1)
        await active_repo.upsert(
            user_id=USER_ID,
            chat_id=cid,
            target_version=1,
            result_id=f"result-{i}",
            waiting_since=NOW + timedelta(seconds=i),
        )

    event = _make_event(
        event_id="evt-view-7",
        event_type=BotEventType.TEXT,
        text="צפה בשיחות",
    )
    result = await handler.handle(event)
    assert result is True
    assert len(bot.buttons) == 5


async def test_view_details_skips_snoozed():
    """Snoozed items are excluded from cards."""
    bot = FakeBot()
    handler, _, active_repo = _make_handler(bot=bot)
    future_snooze = NOW + timedelta(hours=24)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    await active_repo.upsert(
        user_id=USER_ID, chat_id=CHAT_ID,
        target_version=1, result_id="r1", waiting_since=NOW,
    )
    await active_repo.snooze(
        user_id=USER_ID, chat_id=CHAT_ID, snoozed_until=future_snooze,
    )

    event = _make_event(
        event_id="evt-view-snoozed",
        event_type=BotEventType.TEXT,
        text="צפה בשיחות",
    )
    result = await handler.handle(event)
    assert result is True
    assert len(bot.buttons) == 0
    assert any("אין" in t[1] for t in bot.texts)


async def test_view_details_skips_muted():
    """Muted items are excluded from cards."""
    bot = FakeBot()
    handler, _, active_repo = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    await active_repo.upsert(
        user_id=USER_ID, chat_id=CHAT_ID,
        target_version=1, result_id="r1", waiting_since=NOW,
    )
    await handler._mute_repo.mute_permanent(user_id=USER_ID, chat_id=CHAT_ID)

    event = _make_event(
        event_id="evt-view-muted",
        event_type=BotEventType.TEXT,
        text="צפה בשיחות",
    )
    result = await handler.handle(event)
    assert result is True
    assert len(bot.buttons) == 0


async def test_view_details_skips_stale_version():
    """Active items with mismatched version are excluded."""
    bot = FakeBot()
    handler, _, active_repo = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=2)
    await active_repo.upsert(
        user_id=USER_ID, chat_id=CHAT_ID,
        target_version=1, result_id="r1", waiting_since=NOW,
    )

    event = _make_event(
        event_id="evt-view-stale",
        event_type=BotEventType.TEXT,
        text="צפה בשיחות",
    )
    result = await handler.handle(event)
    assert result is True
    assert len(bot.buttons) == 0


# --- Action menu: 3 buttons -------------------------------------------------


async def test_action_menu_sends_3_buttons():
    """Tapping 'מה לעשות' sends 3 action buttons."""
    bot = FakeBot()
    handler, _, active_repo = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    await active_repo.upsert(
        user_id=USER_ID, chat_id=CHAT_ID,
        target_version=1, result_id="r1", waiting_since=NOW,
    )

    event = _make_event(
        event_id="evt-menu-action",
        button_id=f"menu_action:{CHAT_ID}:1",
    )
    result = await handler.handle(event)
    assert result is True
    assert len(bot.buttons) == 1
    _, _body, buttons = bot.buttons[0]
    assert len(buttons) == 3
    assert buttons[0]["title"] == "מטפל עכשיו"
    assert buttons[1]["title"] == "הזכר לי מחר"
    assert buttons[2]["title"] == "הסר"


async def test_action_menu_stale_sends_message():
    """Stale action menu (old card, active has moved to new version) → stale message."""
    bot = FakeBot()
    handler, _, active_repo = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=2)
    # Active item is now at version=2 (re-analyzed).
    await active_repo.upsert(
        user_id=USER_ID, chat_id=CHAT_ID,
        target_version=2, result_id="r2", waiting_since=NOW,
    )

    # User taps an old card that referenced version=1.
    event = _make_event(
        event_id="evt-menu-stale",
        button_id=f"menu_action:{CHAT_ID}:1",
    )
    result = await handler.handle(event)
    assert result is True
    assert any("השתנתה" in t[1] for t in bot.texts)


# --- Feedback menu: 3 buttons -----------------------------------------------


async def test_feedback_menu_sends_3_buttons():
    """Tapping 'משוב ל־Echo' sends 3 feedback buttons."""
    bot = FakeBot()
    handler, _, _ = _make_handler(bot=bot)

    event = _make_event(
        event_id="evt-menu-feedback",
        button_id=f"menu_feedback:{CHAT_ID}:1",
    )
    result = await handler.handle(event)
    assert result is True
    assert len(bot.buttons) == 1
    _, _body, buttons = bot.buttons[0]
    assert len(buttons) == 3
    assert buttons[0]["title"] == "כן"
    assert buttons[1]["title"] == "לא"
    assert buttons[2]["title"] == "לא בטוח"


# --- Action: acknowledge ----------------------------------------------------


async def test_action_acknowledge():
    """Acknowledge action sets acknowledged_at."""
    bot = FakeBot()
    handler, _feedback_service, active_repo = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    await active_repo.upsert(
        user_id=USER_ID, chat_id=CHAT_ID,
        target_version=1, result_id="r1", waiting_since=NOW,
    )

    event = _make_event(
        event_id="evt-ack",
        button_id=f"action:{CHAT_ID}:1:acknowledge",
    )
    result = await handler.handle(event)
    assert result is True
    assert any("מטופל" in t[1] for t in bot.texts)

    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active is not None
    assert active.acknowledged_at is not None


# --- Action: snooze ---------------------------------------------------------


async def test_action_snooze():
    """Snooze action sets snoozed_until."""
    bot = FakeBot()
    handler, _, active_repo = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    await active_repo.upsert(
        user_id=USER_ID, chat_id=CHAT_ID,
        target_version=1, result_id="r1", waiting_since=NOW,
    )

    event = _make_event(
        event_id="evt-snooze",
        button_id=f"action:{CHAT_ID}:1:snooze",
    )
    result = await handler.handle(event)
    assert result is True
    assert any("אזכיר" in t[1] for t in bot.texts)

    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active is not None
    assert active.snoozed_until is not None


# --- Action: resolve --------------------------------------------------------


async def test_action_resolve():
    """Resolve action removes the active item."""
    bot = FakeBot()
    handler, _, active_repo = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    await active_repo.upsert(
        user_id=USER_ID, chat_id=CHAT_ID,
        target_version=1, result_id="r1", waiting_since=NOW,
    )

    event = _make_event(
        event_id="evt-resolve",
        button_id=f"action:{CHAT_ID}:1:resolve",
    )
    result = await handler.handle(event)
    assert result is True
    assert any("הוסר" in t[1] for t in bot.texts)

    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active is None


async def test_action_resolve_only_that_item():
    """Resolve removes only the selected item, not others."""
    handler, _, active_repo = _make_handler()
    await _setup_chat_state(handler, CHAT_ID, version=1)
    await _setup_chat_state(handler, OTHER_CHAT_ID, version=1)
    await active_repo.upsert(
        user_id=USER_ID, chat_id=CHAT_ID,
        target_version=1, result_id="r1", waiting_since=NOW,
    )
    await active_repo.upsert(
        user_id=USER_ID, chat_id=OTHER_CHAT_ID,
        target_version=1, result_id="r2", waiting_since=NOW,
    )

    event = _make_event(
        event_id="evt-resolve-one",
        button_id=f"action:{CHAT_ID}:1:resolve",
    )
    await handler.handle(event)

    assert await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID) is None
    assert await active_repo.get(user_id=USER_ID, chat_id=OTHER_CHAT_ID) is not None


# --- Stale action rejected --------------------------------------------------


async def test_stale_action_rejected():
    """Action on stale version (old card, active moved to new version) → rejected."""
    bot = FakeBot()
    handler, _, active_repo = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=2)
    # Active item is now at version=2 (re-analyzed).
    await active_repo.upsert(
        user_id=USER_ID, chat_id=CHAT_ID,
        target_version=2, result_id="r2", waiting_since=NOW,
    )

    # User taps an old card that referenced version=1.
    event = _make_event(
        event_id="evt-stale-action",
        button_id=f"action:{CHAT_ID}:1:acknowledge",
    )
    result = await handler.handle(event)
    assert result is True
    assert any("השתנתה" in t[1] for t in bot.texts)

    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active is not None
    assert active.acknowledged_at is None


# --- Feedback: correct ------------------------------------------------------


async def test_feedback_correct():
    """Feedback 'כן' records correct verdict."""
    bot = FakeBot()
    handler, _feedback_service, _ = _make_handler(bot=bot)

    event = _make_event(
        event_id="evt-fb-correct",
        button_id=f"feedback:{CHAT_ID}:1:correct",
    )
    result = await handler.handle(event)
    assert result is True
    assert any("תודה" in t[1] for t in bot.texts)


# --- Feedback: false_positive -----------------------------------------------


async def test_feedback_false_positive():
    """Feedback 'לא' records false_positive verdict."""
    bot = FakeBot()
    handler, _, _ = _make_handler(bot=bot)

    event = _make_event(
        event_id="evt-fb-fp",
        button_id=f"feedback:{CHAT_ID}:1:false_positive",
    )
    result = await handler.handle(event)
    assert result is True
    assert any("תודה" in t[1] for t in bot.texts)


async def test_feedback_false_positive_with_mute_offer():
    """3+ false positives → mute offer buttons."""
    bot = FakeBot()
    handler, feedback_service, _ = _make_handler(bot=bot)

    for i in range(3):
        await feedback_service.record_feedback(
            user_id=USER_ID,
            chat_id=CHAT_ID,
            verdict=FeedbackVerdict.FALSE_POSITIVE,
            target_version=1,
            provider_event_id=f"evt-fp-{i}",
        )

    event = _make_event(
        event_id="evt-fp-mute",
        button_id=f"feedback:{CHAT_ID}:1:false_positive",
    )
    result = await handler.handle(event)
    assert result is True
    # Should send mute confirmation buttons.
    assert len(bot.buttons) == 1
    _, _, buttons = bot.buttons[0]
    assert len(buttons) == 2


# --- Feedback: uncertain ----------------------------------------------------


async def test_feedback_uncertain():
    """Feedback 'לא בטוח' records uncertain verdict."""
    bot = FakeBot()
    handler, _, _ = _make_handler(bot=bot)

    event = _make_event(
        event_id="evt-fb-uncertain",
        button_id=f"feedback:{CHAT_ID}:1:uncertain",
    )
    result = await handler.handle(event)
    assert result is True
    assert any("אשתדל" in t[1] for t in bot.texts)


# --- Stale feedback still stored --------------------------------------------


async def test_stale_feedback_still_stored():
    """Feedback on a stale version is still stored (not rejected)."""
    bot = FakeBot()
    handler, _, active_repo = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=2)
    await active_repo.upsert(
        user_id=USER_ID, chat_id=CHAT_ID,
        target_version=1, result_id="r1", waiting_since=NOW,
    )

    event = _make_event(
        event_id="evt-stale-fb",
        button_id=f"feedback:{CHAT_ID}:1:correct",
    )
    result = await handler.handle(event)
    assert result is True
    # Should NOT send stale message — feedback is always accepted.
    assert not any("השתנתה" in t[1] for t in bot.texts)


# --- Duplicate callback idempotent ------------------------------------------


async def test_duplicate_action_idempotent():
    """Duplicate action callback returns True without re-executing."""
    bot = FakeBot()
    handler, _, active_repo = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    await active_repo.upsert(
        user_id=USER_ID, chat_id=CHAT_ID,
        target_version=1, result_id="r1", waiting_since=NOW,
    )

    event = _make_event(
        event_id="evt-dup",
        button_id=f"action:{CHAT_ID}:1:acknowledge",
    )
    result1 = await handler.handle(event)
    assert result1 is True

    result2 = await handler.handle(event)
    assert result2 is True


# --- Mute confirmation ------------------------------------------------------


async def test_mute_confirm_permanent():
    """Permanent mute confirmation mutes the chat."""
    bot = FakeBot()
    handler, _, _ = _make_handler(bot=bot)

    event = _make_event(
        event_id="evt-mute-confirm",
        button_id=f"wfm_mute:{CHAT_ID}",
    )
    result = await handler.handle(event)
    assert result is True

    is_muted = await handler._mute_repo.is_muted(
        user_id=USER_ID, chat_id=CHAT_ID, now=datetime.now(timezone.utc)
    )
    assert is_muted is True


async def test_mute_decline_does_nothing():
    """Declining mute returns True without action."""
    bot = FakeBot()
    handler, _, _ = _make_handler(bot=bot)

    event = _make_event(
        event_id="evt-nomute",
        button_id=f"wfm_nomute:{CHAT_ID}",
    )
    result = await handler.handle(event)
    assert result is True
    is_muted = await handler._mute_repo.is_muted(
        user_id=USER_ID, chat_id=CHAT_ID, now=datetime.now(timezone.utc)
    )
    assert is_muted is False


# --- Miss reporting ----------------------------------------------------------


async def test_miss_report():
    """'פספסתי' records false_negative feedback."""
    bot = FakeBot()
    handler, _, _ = _make_handler(bot=bot)

    event = _make_event(
        event_id="evt-miss",
        event_type=BotEventType.TEXT,
        text="פספסתי",
    )
    result = await handler.handle(event)
    assert result is True
    assert any("דיווחת" in t[1] for t in bot.texts)


# --- Unknown user returns False ---------------------------------------------


async def test_view_details_unknown_user():
    handler, _, _ = _make_handler(user_id=None)
    event = _make_event(
        event_id="evt-unknown",
        event_type=BotEventType.TEXT,
        text="צפה בשיחות",
    )
    assert await handler.handle(event) is False


async def test_action_menu_unknown_user():
    handler, _, _ = _make_handler(user_id=None)
    event = _make_event(
        event_id="evt-unknown",
        button_id=f"menu_action:{CHAT_ID}:1",
    )
    assert await handler.handle(event) is False


async def test_action_unknown_user():
    handler, _, _ = _make_handler(user_id=None)
    event = _make_event(
        event_id="evt-unknown",
        button_id=f"action:{CHAT_ID}:1:acknowledge",
    )
    assert await handler.handle(event) is False


async def test_feedback_unknown_user():
    handler, _, _ = _make_handler(user_id=None)
    event = _make_event(
        event_id="evt-unknown",
        button_id=f"feedback:{CHAT_ID}:1:correct",
    )
    assert await handler.handle(event) is False


async def test_miss_report_unknown_user():
    handler, _, _ = _make_handler(user_id=None)
    event = _make_event(
        event_id="evt-unknown",
        event_type=BotEventType.TEXT,
        text="פספסתי",
    )
    assert await handler.handle(event) is False


# --- Malformed callbacks -----------------------------------------------------


async def test_malformed_menu_action():
    handler, _, _ = _make_handler()
    event = _make_event(
        event_id="evt-bad",
        button_id="menu_action:garbage",
    )
    assert await handler.handle(event) is False


async def test_malformed_action():
    handler, _, _ = _make_handler()
    event = _make_event(
        event_id="evt-bad",
        button_id="action:chat:abc:acknowledge",
    )
    assert await handler.handle(event) is False


async def test_unknown_action_type():
    handler, _, _ = _make_handler()
    event = _make_event(
        event_id="evt-bad",
        button_id=f"action:{CHAT_ID}:1:unknown",
    )
    assert await handler.handle(event) is False


async def test_unknown_feedback_verdict():
    handler, _, _ = _make_handler()
    event = _make_event(
        event_id="evt-bad",
        button_id=f"feedback:{CHAT_ID}:1:unknown",
    )
    assert await handler.handle(event) is False


async def test_unhandled_text_returns_false():
    handler, _, _ = _make_handler()
    event = _make_event(
        event_id="evt-unhandled",
        event_type=BotEventType.TEXT,
        text="hello world",
    )
    assert await handler.handle(event) is False


async def test_unhandled_button_returns_false():
    handler, _, _ = _make_handler()
    event = _make_event(
        event_id="evt-unhandled",
        button_id="unknown_prefix:foo",
    )
    assert await handler.handle(event) is False


# --- Feedback service: mute/unmute -----------------------------------------


async def test_handle_mute_chat_permanent():
    _, feedback_service, _ = _make_handler()
    await feedback_service.handle_mute_chat(
        user_id=USER_ID, chat_id=CHAT_ID, permanent=True,
        provider_event_id="evt-mute-svc-1",
    )
    is_muted = await feedback_service._mute_repo.is_muted(
        user_id=USER_ID, chat_id=CHAT_ID, now=datetime.now(timezone.utc)
    )
    assert is_muted is True


async def test_handle_mute_chat_temporary():
    _, feedback_service, _ = _make_handler()
    await feedback_service.handle_mute_chat(
        user_id=USER_ID, chat_id=CHAT_ID, permanent=False,
        provider_event_id="evt-mute-svc-2",
    )
    is_muted = await feedback_service._mute_repo.is_muted(
        user_id=USER_ID, chat_id=CHAT_ID, now=datetime.now(timezone.utc)
    )
    assert is_muted is True
    far_future = datetime.now(timezone.utc) + timedelta(hours=49)
    assert await feedback_service._mute_repo.is_muted(
        user_id=USER_ID, chat_id=CHAT_ID, now=far_future
    ) is False


async def test_handle_unmute_chat():
    _, feedback_service, _ = _make_handler()
    await feedback_service.handle_mute_chat(
        user_id=USER_ID, chat_id=CHAT_ID, permanent=True,
        provider_event_id="evt-mute-svc-3",
    )
    await feedback_service.handle_unmute_chat(
        user_id=USER_ID, chat_id=CHAT_ID,
        provider_event_id="evt-unmute-svc-1",
    )
    assert await feedback_service._mute_repo.is_muted(
        user_id=USER_ID, chat_id=CHAT_ID, now=datetime.now(timezone.utc)
    ) is False


# --- Feedback repository: delete_expired ------------------------------------


async def test_delete_expired_feedback():
    _, feedback_service, _ = _make_handler()
    past_expiry = datetime.now(timezone.utc) - timedelta(days=1)
    await feedback_service._feedback_repo.record(
        user_id=USER_ID, chat_id=CHAT_ID,
        verdict=FeedbackVerdict.FALSE_POSITIVE,
        provider_event_id="evt-expired-1",
        expires_at=past_expiry,
    )
    future_expiry = datetime.now(timezone.utc) + timedelta(days=90)
    await feedback_service._feedback_repo.record(
        user_id=USER_ID, chat_id=CHAT_ID,
        verdict=FeedbackVerdict.CORRECT,
        provider_event_id="evt-active-1",
        expires_at=future_expiry,
    )
    deleted = await feedback_service._feedback_repo.delete_expired(
        now=datetime.now(timezone.utc)
    )
    assert deleted == 1


# --- Snooze expiry returns to list ------------------------------------------


async def test_expired_snooze_returns_to_list():
    """Expired snooze item reappears in the card list."""
    bot = FakeBot()
    handler, _, active_repo = _make_handler(bot=bot)
    past_snooze = datetime.now(timezone.utc) - timedelta(hours=1)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    await active_repo.upsert(
        user_id=USER_ID, chat_id=CHAT_ID,
        target_version=1, result_id="r1", waiting_since=NOW,
    )
    await active_repo.snooze(
        user_id=USER_ID, chat_id=CHAT_ID, snoozed_until=past_snooze,
    )

    event = _make_event(
        event_id="evt-view-expired",
        event_type=BotEventType.TEXT,
        text="צפה בשיחות",
    )
    result = await handler.handle(event)
    assert result is True
    assert len(bot.buttons) == 1
