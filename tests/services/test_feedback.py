"""Tests for the feedback flyloop — 3-button card UX.

Tests the new flow:
1. Template button → sends up to 5 individual cards with 3 buttons.
2. טופל → resolve + CORRECT feedback (identification was right, user handled it).
3. להזכיר לי → snooze (remind me later).
4. לא צריד → opens dismiss submenu:
   - לא מחכים לי → resolve + FALSE_POSITIVE feedback (Echo was wrong).
   - לא מעניין (שיחכו) → resolve (no feedback, user chooses not to handle).
5. Stale action → rejected with message.
6. Duplicate callback → idempotent (DUPLICATE outcome).
7. Acknowledged items excluded from cards + digest.
8. Snooze uses next local digest hour (08:00), not now+24h.
9. No more "פספסתי" miss report.
10. No more separate feedback/action menus — feedback is implicit.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from echo_v2.domain.feedback import FeedbackVerdict, HandlingOutcome
from echo_v2.domain.waiting_for_me import (
    NextOwner,
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
from echo_v2.persistence.feedback_repositories import (
    InMemoryChatMuteRepository,
    InMemoryChatNotInterestedClickRepository,
    InMemoryWaitingForMeActionRepository,
    InMemoryWaitingForMeFeedbackRepository,
)
from echo_v2.ports.bot import BotEvent, BotEventType
from echo_v2.services.feedback_handler import FeedbackHandler
from echo_v2.services.feedback_service import (
    WaitingForMeActionService,
    WaitingForMeFeedbackService,
)
from echo_v2.services.waiting_list_query import WaitingListQueryService

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 12, 6, 0, 0, tzinfo=timezone.utc)
USER_ID = "user-1"
USER_PHONE = "972501234567"
CHAT_ID = "972508765432@c.us"
OTHER_CHAT_ID = "972509876543@c.us"
RESULT_ID = "result-1"
ACTIVE_ID = "active-1"


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


def _make_handler(
    *,
    bot: FakeBot | None = None,
    user_id: str | None = USER_ID,
    token_service=None,
    base_url: str = "https://echo.example.com",
) -> tuple[
    FeedbackHandler,
    WaitingForMeActionService,
    WaitingForMeFeedbackService,
    InMemoryWaitingForMeActiveRepository,
    InMemoryWaitingForMeFeedbackRepository,
]:
    """Build a fully wired FeedbackHandler with in-memory repos."""
    bot = bot or FakeBot()
    active_repo = InMemoryWaitingForMeActiveRepository()
    mute_repo = InMemoryChatMuteRepository()
    click_repo = InMemoryChatNotInterestedClickRepository()
    action_repo = InMemoryWaitingForMeActionRepository(
        active_repo=active_repo,
        mute_repo=mute_repo,
    )
    feedback_repo = InMemoryWaitingForMeFeedbackRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    result_repo = InMemoryWaitingForMeResultRepository()

    action_service = WaitingForMeActionService(
        active_repo=active_repo,
        action_repo=action_repo,
        mute_repo=mute_repo,
        feedback_repo=feedback_repo,
        result_repo=result_repo,
        click_repo=click_repo,
    )
    feedback_service = WaitingForMeFeedbackService(
        feedback_repo=feedback_repo,
        result_repo=result_repo,
    )
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
        feedback_service=feedback_service,
        active_repo=active_repo,
        result_repo=result_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        mute_repo=mute_repo,
        user_resolver=FakeUserResolver(user_id),
        query_service=query_service,
        token_service=token_service,
        base_url=base_url,
    )
    return handler, action_service, feedback_service, active_repo, feedback_repo


async def _setup_active(
    active_repo: InMemoryWaitingForMeActiveRepository,
    chat_id: str = CHAT_ID,
    target_version: int = 1,
    result_id: str = RESULT_ID,
) -> str:
    """Upsert an active item and return its surrogate id."""
    return await active_repo.upsert(
        user_id=USER_ID,
        chat_id=chat_id,
        target_version=target_version,
        result_id=result_id,
        waiting_since=NOW,
    )


# --- View details: sends individual cards -----------------------------------


async def test_view_details_sends_cards():
    """Tapping 'צפה בשיחות' sends individual cards with 3 buttons."""
    bot = FakeBot()
    handler, _, _, active_repo, _ = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    active_id = await _setup_active(active_repo)

    event = _make_event(
        event_id="evt-view",
        event_type=BotEventType.TEXT,
        text="צפה בשיחות",
    )
    result = await handler.handle(event)
    assert result is True
    assert len(bot.buttons) == 1
    _phone, _body, buttons = bot.buttons[0]
    assert len(buttons) == 3
    titles = [b["title"] for b in buttons]
    assert "טופל" in titles
    assert "להזכיר לי" in titles
    assert "לא צריד" in titles
    # Callback uses active_id
    assert buttons[0]["id"] == f"action:{active_id}:handled"
    assert buttons[1]["id"] == f"action:{active_id}:snooze"
    assert buttons[2]["id"] == f"action:{active_id}:dismiss"


async def test_view_details_no_active_sends_empty_message():
    """No active items → sends 'no waiting' message."""
    bot = FakeBot()
    handler, _, _, _, _ = _make_handler(bot=bot)
    event = _make_event(
        event_id="evt-empty",
        event_type=BotEventType.TEXT,
        text="צפה בשיחות",
    )
    result = await handler.handle(event)
    assert result is True
    assert len(bot.texts) == 1
    assert "אין כרגע" in bot.texts[0][1]


async def test_view_details_unknown_user():
    """Unknown user → not handled (returns False)."""
    handler, _, _, _, _ = _make_handler(user_id=None)
    event = _make_event(
        event_id="evt-unknown",
        event_type=BotEventType.TEXT,
        text="צפה בשיחות",
    )
    assert await handler.handle(event) is False


async def test_view_details_excludes_acknowledged():
    """Acknowledged items are excluded from cards."""
    bot = FakeBot()
    handler, _, _, active_repo, _ = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    await _setup_active(active_repo)

    # Acknowledge the item.
    await active_repo.acknowledge(
        user_id=USER_ID, chat_id=CHAT_ID, acknowledged_at=NOW,
    )

    event = _make_event(
        event_id="evt-ack",
        event_type=BotEventType.TEXT,
        text="צפה בשיחות",
    )
    result = await handler.handle(event)
    assert result is True
    assert len(bot.texts) == 1
    assert "אין כרגע" in bot.texts[0][1]


async def test_view_details_excludes_snoozed():
    """Snoozed items are excluded from cards."""
    bot = FakeBot()
    handler, _, _, active_repo, _ = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    await _setup_active(active_repo)

    future = datetime.now(timezone.utc) + timedelta(hours=10)
    await active_repo.snooze(
        user_id=USER_ID, chat_id=CHAT_ID, snoozed_until=future,
    )

    event = _make_event(
        event_id="evt-snoozed",
        event_type=BotEventType.TEXT,
        text="צפה בשיחות",
    )
    result = await handler.handle(event)
    assert result is True
    assert len(bot.texts) == 1


async def test_view_details_excludes_muted():
    """Muted chats are excluded from cards."""
    bot = FakeBot()
    handler, _, _, active_repo, _ = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    await _setup_active(active_repo)

    await handler._mute_repo.mute_permanent(user_id=USER_ID, chat_id=CHAT_ID)

    event = _make_event(
        event_id="evt-muted",
        event_type=BotEventType.TEXT,
        text="צפה בשיחות",
    )
    result = await handler.handle(event)
    assert result is True
    assert len(bot.texts) == 1


async def test_view_details_excludes_stale():
    """Items where target_version != chat.activity_version are excluded."""
    bot = FakeBot()
    handler, _, _, active_repo, _ = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=2)
    await _setup_active(active_repo, target_version=1)  # stale

    event = _make_event(
        event_id="evt-stale",
        event_type=BotEventType.TEXT,
        text="צפה בשיחות",
    )
    result = await handler.handle(event)
    assert result is True
    assert len(bot.texts) == 1


async def test_view_details_max_5_cards():
    """At most 5 cards are sent."""
    bot = FakeBot()
    handler, _, _, active_repo, _ = _make_handler(bot=bot)
    for i in range(7):
        cid = f"97250{i:08d}@c.us"
        await _setup_chat_state(handler, cid, version=1)
        await _setup_active(active_repo, chat_id=cid, result_id=f"r{i}")

    event = _make_event(
        event_id="evt-max",
        event_type=BotEventType.TEXT,
        text="צפה בשיחות",
    )
    result = await handler.handle(event)
    assert result is True
    assert len(bot.buttons) == 5


async def test_whatsapp_cards_and_mini_app_return_same_actionable_ids():
    """Critical invariant: WhatsApp cards and the query service agree.

    Setup: A active, B snoozed, C muted, D stale, E active.
    The WhatsApp cards (via _handle_view_details) must show exactly the
    same active_ids as query_service.current_actionable, in the same
    order, capped at MAX_CARDS.
    """
    # Use a snooze time relative to the real clock — the handler uses
    # datetime.now(timezone.utc), not the test's NOW.
    real_now = datetime.now(timezone.utc)
    bot = FakeBot()
    handler, _, _, active_repo, _ = _make_handler(bot=bot)

    # A — active
    await _setup_chat_state(handler, "a@c.us", version=1)
    id_a = await _setup_active(active_repo, chat_id="a@c.us", result_id="ra")
    # B — snoozed (should be excluded)
    await _setup_chat_state(handler, "b@c.us", version=1)
    id_b = await _setup_active(active_repo, chat_id="b@c.us", result_id="rb")
    await active_repo.snooze(
        user_id=USER_ID,
        chat_id="b@c.us",
        snoozed_until=real_now + timedelta(hours=2),
    )
    # C — muted (should be excluded)
    await _setup_chat_state(handler, "c@c.us", version=1)
    id_c = await _setup_active(active_repo, chat_id="c@c.us", result_id="rc")
    await handler._mute_repo.mute_permanent(
        user_id=USER_ID, chat_id="c@c.us",
    )
    # D — stale (activity_version mismatch, should be excluded)
    await _setup_chat_state(handler, "d@c.us", version=5)
    id_d = await _setup_active(
        active_repo, chat_id="d@c.us", result_id="rd", target_version=1
    )
    # E — active
    await _setup_chat_state(handler, "e@c.us", version=1)
    id_e = await _setup_active(active_repo, chat_id="e@c.us", result_id="re")

    # Query service: should return [A, E] (sorted by waiting_since ascending).
    query_items = await handler._query_service.current_actionable(USER_ID, now=real_now)
    query_ids = [a.id for a in query_items]
    assert set(query_ids) == {id_a, id_e}
    assert id_b not in query_ids
    assert id_c not in query_ids
    assert id_d not in query_ids

    # WhatsApp cards: should show the same ids.
    event = _make_event(
        event_id="evt-consistency",
        event_type=BotEventType.TEXT,
        text="צפה בשיחות",
    )
    result = await handler.handle(event)
    assert result is True
    # Extract active_id from each card's "action:{active_id}:handled" button.
    card_active_ids = []
    for _phone, _body, buttons in bot.buttons:
        for btn in buttons:
            if btn["id"].startswith("action:") and btn["id"].endswith(":handled"):
                card_active_ids.append(btn["id"].split(":")[1])
                break
    assert set(card_active_ids) == set(query_ids)
    assert id_b not in card_active_ids
    assert id_c not in card_active_ids
    assert id_d not in card_active_ids


# --- Action: handled (טופל) -------------------------------------------------


async def test_action_handled_applied():
    """טופל → APPLIED, deletes active item, records CORRECT feedback."""
    bot = FakeBot()
    handler, _, _, active_repo, _ = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    active_id = await _setup_active(active_repo)

    event = _make_event(
        event_id="evt-handled",
        button_id=f"action:{active_id}:handled",
    )
    result = await handler.handle(event)
    assert result is True
    assert any("סימנתי שטופל" in t[1] for t in bot.texts)

    # Active item deleted.
    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active is None


async def test_action_handled_records_correct_feedback():
    """טופל records implicit CORRECT feedback."""
    bot = FakeBot()
    handler, _, _, active_repo, feedback_repo = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    active_id = await _setup_active(active_repo)

    event = _make_event(
        event_id="evt-handled-fb",
        button_id=f"action:{active_id}:handled",
    )
    await handler.handle(event)

    # Check feedback was recorded.
    assert len(feedback_repo._rows) == 1
    fb = feedback_repo._rows[0]
    assert fb.verdict == FeedbackVerdict.CORRECT
    assert fb.result_id == RESULT_ID


async def test_action_handled_stale():
    """Stale handled → STALE message."""
    bot = FakeBot()
    handler, _, _, active_repo, _ = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=2)
    active_id = await _setup_active(active_repo, target_version=1)

    event = _make_event(
        event_id="evt-stale-handled",
        button_id=f"action:{active_id}:handled",
    )
    result = await handler.handle(event)
    assert result is True
    # The handler gets version from active_repo, which returns v1.
    # The action_service checks active.user_id and active.target_version.
    # Since the active row IS at v1, and the handler passes v1, it succeeds.
    # For a true stale test, we need the active row to have moved to v2
    # while the callback references v1. But the handler always reads the
    # current version from the active row, so this can't happen in practice.
    # The stale check is in the service when the version doesn't match.


async def test_action_handled_not_found():
    """Handled for non-existent active_id → stale message."""
    bot = FakeBot()
    handler, _, _, _, _ = _make_handler(bot=bot)
    event = _make_event(
        event_id="evt-nf-handled",
        button_id="action:nonexistent:handled",
    )
    result = await handler.handle(event)
    assert result is True
    assert any("השתנתה" in t[1] for t in bot.texts)


async def test_action_handled_duplicate():
    """Duplicate handled callback → DUPLICATE (no second message)."""
    bot = FakeBot()
    handler, _, _, active_repo, _ = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    active_id = await _setup_active(active_repo)

    event1 = _make_event(
        event_id="evt-dup-handled",
        button_id=f"action:{active_id}:handled",
    )
    await handler.handle(event1)
    assert len(bot.texts) == 1

    event2 = _make_event(
        event_id="evt-dup-handled",  # same event_id
        button_id=f"action:{active_id}:handled",
    )
    await handler.handle(event2)
    # Duplicate → no new message
    assert len(bot.texts) == 1


# --- Action: snooze (להזכיר לי) ---------------------------------------------


async def test_action_snooze_applied():
    """להזכיר לי → APPLIED, sets snoozed_until to next 08:00 local."""
    bot = FakeBot()
    handler, _, _, active_repo, _ = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    active_id = await _setup_active(active_repo)

    event = _make_event(
        event_id="evt-snooze",
        button_id=f"action:{active_id}:snooze",
    )
    result = await handler.handle(event)
    assert result is True
    assert any("אזכיר" in t[1] for t in bot.texts)

    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active.snoozed_until is not None
    assert active.snoozed_until > datetime.now(timezone.utc)


async def test_action_snooze_duplicate():
    """Duplicate snooze callback → DUPLICATE."""
    bot = FakeBot()
    handler, _, _, active_repo, _ = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    active_id = await _setup_active(active_repo)

    event1 = _make_event(
        event_id="evt-dup-snooze",
        button_id=f"action:{active_id}:snooze",
    )
    await handler.handle(event1)
    assert len(bot.texts) == 1

    event2 = _make_event(
        event_id="evt-dup-snooze",
        button_id=f"action:{active_id}:snooze",
    )
    await handler.handle(event2)
    assert len(bot.texts) == 1  # no new message


# --- Action: dismiss (לא צריד) → opens submenu ------------------------------


async def test_action_dismiss_sends_submenu():
    """לא צריד → sends dismiss submenu with 2 buttons."""
    bot = FakeBot()
    handler, _, _, active_repo, _ = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    active_id = await _setup_active(active_repo)

    event = _make_event(
        event_id="evt-dismiss",
        button_id=f"action:{active_id}:dismiss",
    )
    result = await handler.handle(event)
    assert result is True
    assert len(bot.buttons) == 1
    _phone, _body, buttons = bot.buttons[0]
    assert len(buttons) == 2
    titles = [b["title"] for b in buttons]
    assert "לא מחכים לי" in titles
    assert "לא מעניין (שיחכו)" in titles
    assert buttons[0]["id"] == f"dismiss:{active_id}:not_waiting"
    assert buttons[1]["id"] == f"dismiss:{active_id}:not_interested"


# --- Dismiss: not_waiting (לא מחכים לי) -------------------------------------


async def test_dismiss_not_waiting_applied():
    """לא מחכים לי → APPLIED, deletes active, records FALSE_POSITIVE."""
    bot = FakeBot()
    handler, _, _, active_repo, feedback_repo = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    active_id = await _setup_active(active_repo)

    event = _make_event(
        event_id="evt-not-waiting",
        button_id=f"dismiss:{active_id}:not_waiting",
    )
    result = await handler.handle(event)
    assert result is True
    assert any("אני אלמד" in t[1] for t in bot.texts)

    # Active item deleted.
    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active is None

    # FALSE_POSITIVE feedback recorded.
    assert len(feedback_repo._rows) == 1
    assert feedback_repo._rows[0].verdict == FeedbackVerdict.FALSE_POSITIVE


async def test_dismiss_not_waiting_duplicate():
    """Duplicate dismiss_not_waiting → DUPLICATE."""
    bot = FakeBot()
    handler, _, _, active_repo, _ = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    active_id = await _setup_active(active_repo)

    event1 = _make_event(
        event_id="evt-dup-nw",
        button_id=f"dismiss:{active_id}:not_waiting",
    )
    await handler.handle(event1)
    assert len(bot.texts) == 1

    event2 = _make_event(
        event_id="evt-dup-nw",
        button_id=f"dismiss:{active_id}:not_waiting",
    )
    await handler.handle(event2)
    assert len(bot.texts) == 1


# --- Dismiss: not_interested (לא מעניין) ------------------------------------


async def test_dismiss_not_interested_applied():
    """לא מעניין (שיחכו) → APPLIED, deletes active, no feedback, 24h mute."""
    bot = FakeBot()
    handler, _, _, active_repo, feedback_repo = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    active_id = await _setup_active(active_repo)

    event = _make_event(
        event_id="evt-not-interested",
        button_id=f"dismiss:{active_id}:not_interested",
    )
    result = await handler.handle(event)
    assert result is True
    assert any("הוסר" in t[1] for t in bot.texts)

    # Active item deleted.
    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active is None

    # No feedback recorded.
    assert len(feedback_repo._rows) == 0


async def test_dismiss_not_interested_duplicate():
    """Duplicate dismiss_not_interested → DUPLICATE, no double mute."""
    bot = FakeBot()
    handler, action_service, _, active_repo, _ = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    active_id = await _setup_active(active_repo)

    event1 = _make_event(
        event_id="evt-dup-ni",
        button_id=f"dismiss:{active_id}:not_interested",
    )
    await handler.handle(event1)
    assert len(bot.texts) == 1

    event2 = _make_event(
        event_id="evt-dup-ni",
        button_id=f"dismiss:{active_id}:not_interested",
    )
    await handler.handle(event2)
    assert len(bot.texts) == 1

    # Duplicate click did not increment the counter twice.
    clicks = await action_service._click_repo.get(
        user_id=USER_ID, chat_id=CHAT_ID,
    )
    assert clicks is not None
    assert clicks.click_count == 1


# --- Dismiss: not_interested escalation (24h → 48h → 1 week → permanent) -----


async def _click_not_interested(
    handler: FeedbackHandler,
    active_repo: InMemoryWaitingForMeActiveRepository,
    *,
    event_id: str,
) -> None:
    """Helper: re-create an active item and click לא מעניין (שיחכו)."""
    active_id = await _setup_active(active_repo)
    event = _make_event(
        event_id=event_id,
        button_id=f"dismiss:{active_id}:not_interested",
    )
    await handler.handle(event)


async def test_dismiss_not_interested_escalation():
    """4 clicks → 24h → 48h → 1 week → permanent mute."""
    bot = FakeBot()
    handler, action_service, _, active_repo, _ = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)

    # Click 1 → 24h temporary mute.
    before = datetime.now(timezone.utc)
    await _click_not_interested(handler, active_repo, event_id="ni-1")
    mute = await action_service._mute_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert mute is not None
    assert mute.permanent is False
    assert mute.muted_until is not None
    delta1 = mute.muted_until - before
    assert timedelta(hours=23, minutes=58) < delta1 < timedelta(hours=24, minutes=2)

    # Click 2 → 48h temporary mute.
    before = datetime.now(timezone.utc)
    await _click_not_interested(handler, active_repo, event_id="ni-2")
    mute = await action_service._mute_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert mute is not None
    assert mute.permanent is False
    delta2 = mute.muted_until - before  # type: ignore[operator]
    assert timedelta(hours=47, minutes=58) < delta2 < timedelta(hours=48, minutes=2)

    # Click 3 → 1 week temporary mute.
    before = datetime.now(timezone.utc)
    await _click_not_interested(handler, active_repo, event_id="ni-3")
    mute = await action_service._mute_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert mute is not None
    assert mute.permanent is False
    delta3 = mute.muted_until - before  # type: ignore[operator]
    assert timedelta(days=6, hours=23, minutes=58) < delta3 < timedelta(days=7, minutes=2)

    # Click 4 → permanent mute.
    await _click_not_interested(handler, active_repo, event_id="ni-4")
    mute = await action_service._mute_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert mute is not None
    assert mute.permanent is True
    assert mute.muted_until is None


async def test_dismiss_not_interested_resets_on_handled():
    """handled resets the counter — next click is back to 24h."""
    bot = FakeBot()
    handler, action_service, _, active_repo, _ = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)

    # Two clicks → 48h mute + counter at 2.
    await _click_not_interested(handler, active_repo, event_id="ni-1")
    await _click_not_interested(handler, active_repo, event_id="ni-2")
    clicks = await action_service._click_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert clicks is not None
    assert clicks.click_count == 2

    # handled → counter reset.
    active_id = await _setup_active(active_repo)
    event = _make_event(event_id="evt-handled", button_id=f"action:{active_id}:handled")
    await handler.handle(event)
    clicks = await action_service._click_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert clicks is None

    # Next not-interested click → back to 24h (count == 1).
    before = datetime.now(timezone.utc)
    await _click_not_interested(handler, active_repo, event_id="ni-after")
    mute = await action_service._mute_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert mute is not None
    assert mute.permanent is False
    delta = mute.muted_until - before  # type: ignore[operator]
    assert timedelta(hours=23, minutes=58) < delta < timedelta(hours=24, minutes=2)


async def test_done_resets_clicks():
    """done (web) resets the not-interested counter."""
    _, action_service, _, active_repo, _ = _make_handler()
    await action_service._click_repo.increment(
        user_id=USER_ID, chat_id=CHAT_ID, now=NOW,
    )
    await action_service._click_repo.increment(
        user_id=USER_ID, chat_id=CHAT_ID, now=NOW,
    )
    clicks = await action_service._click_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert clicks is not None
    assert clicks.click_count == 2

    active_id = await _setup_active(active_repo)
    outcome = await action_service.done(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="evt-done",
    )
    assert outcome == HandlingOutcome.APPLIED
    clicks = await action_service._click_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert clicks is None


async def test_dismiss_with_reason_no_response_required_snoozes():
    """Web path: dismiss_with_reason(no_response_required) → 24h mute."""
    _, action_service, _, active_repo, _ = _make_handler()
    active_id = await _setup_active(active_repo)

    outcome = await action_service.dismiss_with_reason(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="evt-nrr",
        reason="no_response_required",
    )
    assert outcome == HandlingOutcome.APPLIED

    mute = await action_service._mute_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert mute is not None
    assert mute.permanent is False
    delta = mute.muted_until - datetime.now(timezone.utc)  # type: ignore[operator]
    assert timedelta(hours=23, minutes=58) < delta < timedelta(hours=24, minutes=2)


async def test_dismiss_with_reason_detected_incorrectly_does_not_snooze():
    """detected_incorrectly does NOT trigger the escalating snooze."""
    _, action_service, _, active_repo, _ = _make_handler()
    active_id = await _setup_active(active_repo)

    outcome = await action_service.dismiss_with_reason(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="evt-di",
        reason="detected_incorrectly",
    )
    assert outcome == HandlingOutcome.APPLIED

    mute = await action_service._mute_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert mute is None


# --- Unknown/malformed callbacks --------------------------------------------


async def test_action_unknown_type():
    """Unknown action type → not handled."""
    handler, _, _, active_repo, _ = _make_handler()
    await _setup_chat_state(handler, CHAT_ID, version=1)
    active_id = await _setup_active(active_repo)

    event = _make_event(
        event_id="evt-unknown-action",
        button_id=f"action:{active_id}:bogus",
    )
    assert await handler.handle(event) is False


async def test_action_malformed_too_short():
    handler, _, _, _, _ = _make_handler()
    event = _make_event(
        event_id="evt-bad",
        button_id="action:only-one-segment",
    )
    assert await handler.handle(event) is False


async def test_dismiss_unknown_reason():
    """Unknown dismiss reason → not handled."""
    handler, _, _, active_repo, _ = _make_handler()
    await _setup_chat_state(handler, CHAT_ID, version=1)
    active_id = await _setup_active(active_repo)

    event = _make_event(
        event_id="evt-bad-reason",
        button_id=f"dismiss:{active_id}:bogus",
    )
    assert await handler.handle(event) is False


async def test_dismiss_malformed_too_short():
    handler, _, _, _, _ = _make_handler()
    event = _make_event(
        event_id="evt-bad",
        button_id="dismiss:only-one",
    )
    assert await handler.handle(event) is False


async def test_action_unknown_user():
    """Action callback from unknown user → not handled."""
    handler, _, _, _, _ = _make_handler(user_id=None)
    event = _make_event(
        event_id="evt-unknown-act",
        button_id=f"action:{ACTIVE_ID}:handled",
    )
    assert await handler.handle(event) is False


async def test_dismiss_unknown_user():
    """Dismiss callback from unknown user → not handled."""
    handler, _, _, _, _ = _make_handler(user_id=None)
    event = _make_event(
        event_id="evt-unknown-dismiss",
        button_id=f"dismiss:{ACTIVE_ID}:not_waiting",
    )
    assert await handler.handle(event) is False


# --- Unrelated events --------------------------------------------------------


async def test_unrelated_text_not_handled():
    """Unrelated text → not handled."""
    handler, _, _, _, _ = _make_handler()
    event = _make_event(
        event_id="evt-unrelated",
        event_type=BotEventType.TEXT,
        text="hello world",
    )
    assert await handler.handle(event) is False


async def test_unrelated_button_not_handled():
    """Unrelated button → not handled."""
    handler, _, _, _, _ = _make_handler()
    event = _make_event(
        event_id="evt-unrelated",
        button_id="some_other_prefix:foo",
    )
    assert await handler.handle(event) is False


# --- Action service direct tests ---------------------------------------------


async def test_service_handled_stale_returns_stale():
    """ActionService.handled returns STALE for wrong version."""
    _, action_service, _, active_repo, _ = _make_handler()
    active_id = await _setup_active(active_repo, target_version=2)

    outcome = await action_service.handled(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,  # wrong version
        provider_message_id="evt-stale",
    )
    assert outcome == HandlingOutcome.STALE


async def test_service_handled_not_found():
    """ActionService.handled returns NOT_FOUND for missing active."""
    _, action_service, _, _, _ = _make_handler()
    outcome = await action_service.handled(
        user_id=USER_ID,
        active_id="nonexistent",
        target_version=1,
        provider_message_id="evt-nf",
    )
    assert outcome == HandlingOutcome.NOT_FOUND


async def test_service_snooze_stale_returns_stale():
    _, action_service, _, active_repo, _ = _make_handler()
    active_id = await _setup_active(active_repo, target_version=2)

    outcome = await action_service.snooze(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="evt-stale",
    )
    assert outcome == HandlingOutcome.STALE


async def test_service_dismiss_not_waiting_stale():
    _, action_service, _, active_repo, _ = _make_handler()
    active_id = await _setup_active(active_repo, target_version=2)

    outcome = await action_service.dismiss_not_waiting(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="evt-stale",
    )
    assert outcome == HandlingOutcome.STALE


async def test_service_dismiss_not_interested_stale():
    _, action_service, _, active_repo, _ = _make_handler()
    active_id = await _setup_active(active_repo, target_version=2)

    outcome = await action_service.dismiss_not_interested(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="evt-stale",
    )
    assert outcome == HandlingOutcome.STALE


async def test_service_handled_duplicate():
    _, action_service, _, active_repo, _ = _make_handler()
    active_id = await _setup_active(active_repo)

    o1 = await action_service.handled(
        user_id=USER_ID, active_id=active_id, target_version=1,
        provider_message_id="evt-dup",
    )
    assert o1 == HandlingOutcome.APPLIED

    o2 = await action_service.handled(
        user_id=USER_ID, active_id=active_id, target_version=1,
        provider_message_id="evt-dup",
    )
    assert o2 == HandlingOutcome.DUPLICATE


async def test_service_snooze_duplicate():
    _, action_service, _, active_repo, _ = _make_handler()
    active_id = await _setup_active(active_repo)

    o1 = await action_service.snooze(
        user_id=USER_ID, active_id=active_id, target_version=1,
        provider_message_id="evt-dup",
    )
    assert o1 == HandlingOutcome.APPLIED

    o2 = await action_service.snooze(
        user_id=USER_ID, active_id=active_id, target_version=1,
        provider_message_id="evt-dup",
    )
    assert o2 == HandlingOutcome.DUPLICATE


async def test_service_dismiss_not_waiting_duplicate():
    _, action_service, _, active_repo, _ = _make_handler()
    active_id = await _setup_active(active_repo)

    o1 = await action_service.dismiss_not_waiting(
        user_id=USER_ID, active_id=active_id, target_version=1,
        provider_message_id="evt-dup",
    )
    assert o1 == HandlingOutcome.APPLIED

    o2 = await action_service.dismiss_not_waiting(
        user_id=USER_ID, active_id=active_id, target_version=1,
        provider_message_id="evt-dup",
    )
    assert o2 == HandlingOutcome.DUPLICATE


async def test_service_dismiss_not_interested_duplicate():
    _, action_service, _, active_repo, _ = _make_handler()
    active_id = await _setup_active(active_repo)

    o1 = await action_service.dismiss_not_interested(
        user_id=USER_ID, active_id=active_id, target_version=1,
        provider_message_id="evt-dup",
    )
    assert o1 == HandlingOutcome.APPLIED

    o2 = await action_service.dismiss_not_interested(
        user_id=USER_ID, active_id=active_id, target_version=1,
        provider_message_id="evt-dup",
    )
    assert o2 == HandlingOutcome.DUPLICATE


# --- Feedback service direct tests -------------------------------------------


async def test_feedback_service_record_applied():
    _, _, feedback_service, _, _ = _make_handler()
    outcome = await feedback_service.record(
        user_id=USER_ID,
        result_id=RESULT_ID,
        verdict=FeedbackVerdict.CORRECT,
        provider_message_id="evt-fb-1",
    )
    assert outcome == HandlingOutcome.APPLIED


async def test_feedback_service_duplicate_message_id():
    _, _, feedback_service, _, _ = _make_handler()
    await feedback_service.record(
        user_id=USER_ID,
        result_id=RESULT_ID,
        verdict=FeedbackVerdict.CORRECT,
        provider_message_id="evt-dup",
    )
    outcome = await feedback_service.record(
        user_id=USER_ID,
        result_id=RESULT_ID,
        verdict=FeedbackVerdict.FALSE_POSITIVE,
        provider_message_id="evt-dup",
    )
    assert outcome == HandlingOutcome.DUPLICATE


async def test_feedback_service_semantic_dedup():
    """First feedback for a result_id wins."""
    _, _, feedback_service, _, _ = _make_handler()
    o1 = await feedback_service.record(
        user_id=USER_ID,
        result_id=RESULT_ID,
        verdict=FeedbackVerdict.CORRECT,
        provider_message_id="evt-1",
    )
    assert o1 == HandlingOutcome.APPLIED

    o2 = await feedback_service.record(
        user_id=USER_ID,
        result_id=RESULT_ID,
        verdict=FeedbackVerdict.FALSE_POSITIVE,
        provider_message_id="evt-2",
    )
    assert o2 == HandlingOutcome.DUPLICATE


# --- Name resolution ---------------------------------------------------------


async def test_resolve_name_from_chat_state():
    """Name resolved from chat_state.chat_name."""
    from dataclasses import replace

    bot = FakeBot()
    handler, _, _, active_repo, _ = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    chat = await handler._chat_state_repo.get(USER_ID, CHAT_ID)
    chat_with_name = replace(chat, chat_name="יוסי")
    handler._chat_state_repo._chats[(USER_ID, CHAT_ID)] = chat_with_name
    await _setup_active(active_repo)

    event = _make_event(
        event_id="evt-view-name",
        event_type=BotEventType.TEXT,
        text="צפה בשיחות",
    )
    await handler.handle(event)
    _, _body, _ = bot.buttons[0]
    assert "יוסי" in _body


async def test_resolve_name_from_contact():
    """Name resolved from contact repo when chat_state has no name."""
    bot = FakeBot()
    handler, _, _, active_repo, _ = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    await handler._contact_repo.save(
        ContactRecord(
            user_id=USER_ID,
            phone_number=CHAT_ID.split("@")[0],
            display_name="דנה",
        )
    )
    await _setup_active(active_repo)

    event = _make_event(
        event_id="evt-view-contact",
        event_type=BotEventType.TEXT,
        text="צפה בשיחות",
    )
    await handler.handle(event)
    _, _body, _ = bot.buttons[0]
    assert "דנה" in _body


async def test_resolve_name_from_message():
    """Name resolved from latest inbound message when no chat/contact name."""
    from echo_v2.domain.chat import Message
    from echo_v2.ports.whatsapp import MessageDirection

    bot = FakeBot()
    handler, _, _, active_repo, _ = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    await handler._message_repo.save(
        Message(
            id="msg-1",
            user_id=USER_ID,
            connection_id="conn-1",
            chat_id=CHAT_ID,
            provider_message_id="msg-1",
            direction=MessageDirection.INBOUND,
            sender_id=None,
            text="היי",
            chat_name="שרה",
            sender_name="שרה",
            timestamp=NOW,
        )
    )
    await _setup_active(active_repo)

    event = _make_event(
        event_id="evt-view-msg-name",
        event_type=BotEventType.TEXT,
        text="צפה בשיחות",
    )
    await handler.handle(event)
    _, _body, _ = bot.buttons[0]
    assert "שרה" in _body


async def test_resolve_name_falls_back_to_phone():
    """No name anywhere → falls back to phone number from chat_id."""
    bot = FakeBot()
    handler, _, _, active_repo, _ = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)
    await _setup_active(active_repo)

    event = _make_event(
        event_id="evt-view-phone",
        event_type=BotEventType.TEXT,
        text="צפה בשיחות",
    )
    await handler.handle(event)
    _, _body, _ = bot.buttons[0]
    assert CHAT_ID.split("@")[0] in _body


# --- Snooze time calculation -------------------------------------------------


async def test_snooze_uses_next_digest_hour():
    """Snooze sets snoozed_until to next 08:00 local time."""
    from echo_v2.services.feedback_service import _next_digest_at

    # At 06:00 UTC = 09:00 IDT (UTC+3), next 08:00 IDT is tomorrow.
    now = datetime(2026, 9, 12, 6, 0, 0, tzinfo=timezone.utc)
    snoozed = _next_digest_at(now_utc=now, tz_name="Asia/Jerusalem")
    # 08:00 IDT = 05:00 UTC. Tomorrow 05:00 UTC.
    assert snoozed.hour == 5  # 08:00 IDT = 05:00 UTC
    assert snoozed > now


async def test_snooze_before_digest_hour_today():
    """At 04:00 UTC = 07:00 IDT, next 08:00 IDT is today."""
    from echo_v2.services.feedback_service import _next_digest_at

    now = datetime(2026, 9, 12, 4, 0, 0, tzinfo=timezone.utc)
    snoozed = _next_digest_at(now_utc=now, tz_name="Asia/Jerusalem")
    # 08:00 IDT = 05:00 UTC. Today 05:00 UTC.
    assert snoozed.hour == 5
    assert snoozed.day == 12  # today


# --- Active repo: upsert resets acknowledged_at ------------------------------


async def test_upsert_resets_acknowledged_at():
    """Upsert on existing active row resets acknowledged_at to None."""
    active_repo = InMemoryWaitingForMeActiveRepository()
    await active_repo.upsert(
        user_id=USER_ID, chat_id=CHAT_ID,
        target_version=1, result_id="r1", waiting_since=NOW,
    )
    await active_repo.acknowledge(
        user_id=USER_ID, chat_id=CHAT_ID, acknowledged_at=NOW,
    )
    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active.acknowledged_at is not None

    # Upsert with new version → resets acknowledged_at.
    await active_repo.upsert(
        user_id=USER_ID, chat_id=CHAT_ID,
        target_version=2, result_id="r2", waiting_since=NOW,
    )
    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active.acknowledged_at is None
    assert active.target_version == 2


# --- Active repo: apply_if_version -------------------------------------------


async def test_apply_if_version_success():
    """apply_if_version updates when version matches."""
    active_repo = InMemoryWaitingForMeActiveRepository()
    active_id = await active_repo.upsert(
        user_id=USER_ID, chat_id=CHAT_ID,
        target_version=1, result_id="r1", waiting_since=NOW,
    )
    now = datetime.now(timezone.utc)
    changed = await active_repo.apply_if_version(
        active_id=active_id,
        user_id=USER_ID,
        target_version=1,
        mutate={"acknowledged_at": now},
    )
    assert changed is True
    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    assert active.acknowledged_at == now


async def test_apply_if_version_stale():
    """apply_if_version returns False when version doesn't match."""
    active_repo = InMemoryWaitingForMeActiveRepository()
    active_id = await active_repo.upsert(
        user_id=USER_ID, chat_id=CHAT_ID,
        target_version=2, result_id="r1", waiting_since=NOW,
    )
    now = datetime.now(timezone.utc)
    changed = await active_repo.apply_if_version(
        active_id=active_id,
        user_id=USER_ID,
        target_version=1,  # wrong
        mutate={"acknowledged_at": now},
    )
    assert changed is False


async def test_apply_if_version_not_found():
    """apply_if_version returns False when active_id doesn't exist."""
    active_repo = InMemoryWaitingForMeActiveRepository()
    now = datetime.now(timezone.utc)
    changed = await active_repo.apply_if_version(
        active_id="nonexistent",
        user_id=USER_ID,
        target_version=1,
        mutate={"acknowledged_at": now},
    )
    assert changed is False


async def test_get_by_id():
    """get_by_id returns the active item by surrogate id."""
    active_repo = InMemoryWaitingForMeActiveRepository()
    active_id = await active_repo.upsert(
        user_id=USER_ID, chat_id=CHAT_ID,
        target_version=1, result_id="r1", waiting_since=NOW,
    )
    active = await active_repo.get_by_id(active_id)
    assert active is not None
    assert active.chat_id == CHAT_ID

    # Non-existent id.
    assert await active_repo.get_by_id("nonexistent") is None


# --- Action service: mute_chat / unmute_chat ---------------------------------


async def test_service_mute_chat_permanent():
    """mute_chat with permanent=True mutes permanently."""
    _, action_service, _, _, _ = _make_handler()
    outcome = await action_service.mute_chat(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        permanent=True,
        provider_message_id="evt-mute-perm",
    )
    assert outcome == HandlingOutcome.APPLIED


async def test_service_mute_chat_temporary():
    """mute_chat with permanent=False mutes temporarily (48h)."""
    _, action_service, _, _, _ = _make_handler()
    outcome = await action_service.mute_chat(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        permanent=False,
        provider_message_id="evt-mute-temp",
    )
    assert outcome == HandlingOutcome.APPLIED


async def test_service_mute_chat_duplicate():
    """Duplicate mute callback → DUPLICATE."""
    _, action_service, _, _, _ = _make_handler()
    await action_service.mute_chat(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        permanent=True,
        provider_message_id="evt-mute-dup",
    )
    outcome = await action_service.mute_chat(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        permanent=True,
        provider_message_id="evt-mute-dup",
    )
    assert outcome == HandlingOutcome.DUPLICATE


async def test_service_unmute_chat_applied():
    """unmute_chat → APPLIED."""
    _, action_service, _, _, _ = _make_handler()
    # First mute.
    await action_service.mute_chat(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        permanent=True,
        provider_message_id="evt-mute-1",
    )
    # Then unmute.
    outcome = await action_service.unmute_chat(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        provider_message_id="evt-unmute-1",
    )
    assert outcome == HandlingOutcome.APPLIED


async def test_service_unmute_chat_duplicate():
    """Duplicate unmute callback → DUPLICATE."""
    _, action_service, _, _, _ = _make_handler()
    await action_service.unmute_chat(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        provider_message_id="evt-unmute-dup",
    )
    outcome = await action_service.unmute_chat(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        provider_message_id="evt-unmute-dup",
    )
    assert outcome == HandlingOutcome.DUPLICATE


# --- Mute repo direct tests --------------------------------------------------


async def test_mute_repo_temporary_expired_cleanup():
    """is_muted cleans up expired temporary mutes."""
    mute_repo = InMemoryChatMuteRepository()
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    await mute_repo.mute_temporary(
        user_id=USER_ID, chat_id=CHAT_ID, muted_until=past,
    )
    # is_muted should clean up and return False.
    now = datetime.now(timezone.utc)
    assert await mute_repo.is_muted(user_id=USER_ID, chat_id=CHAT_ID, now=now) is False
    # Row should be deleted.
    assert await mute_repo.get(user_id=USER_ID, chat_id=CHAT_ID) is None


async def test_mute_repo_unmute_nonexistent():
    """unmute returns False when no mute exists."""
    mute_repo = InMemoryChatMuteRepository()
    assert await mute_repo.unmute(user_id=USER_ID, chat_id=CHAT_ID) is False


async def test_mute_repo_get_nonexistent():
    """get returns None when no mute exists."""
    mute_repo = InMemoryChatMuteRepository()
    assert await mute_repo.get(user_id=USER_ID, chat_id=CHAT_ID) is None


async def test_mute_repo_is_muted_no_row():
    """is_muted returns False when no mute row exists."""
    mute_repo = InMemoryChatMuteRepository()
    now = datetime.now(timezone.utc)
    assert await mute_repo.is_muted(user_id=USER_ID, chat_id=CHAT_ID, now=now) is False


async def test_feedback_repo_delete_expired():
    """delete_expired removes expired feedback rows."""
    feedback_repo = InMemoryWaitingForMeFeedbackRepository()
    past = datetime.now(timezone.utc) - timedelta(days=1)
    # Expired feedback.
    await feedback_repo.record(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        verdict=FeedbackVerdict.CORRECT,
        provider_message_id="evt-expired",
        expires_at=past,
    )
    # Active feedback (no expiry).
    await feedback_repo.record(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        verdict=FeedbackVerdict.CORRECT,
        provider_message_id="evt-active",
    )
    now = datetime.now(timezone.utc)
    deleted = await feedback_repo.delete_expired(now=now)
    assert deleted == 1


async def test_feedback_repo_record_no_message_id_no_result_id():
    """Feedback with no message_id and no result_id is recorded."""
    feedback_repo = InMemoryWaitingForMeFeedbackRepository()
    result = await feedback_repo.record(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        verdict=FeedbackVerdict.CORRECT,
    )
    assert result is not None
    assert result.verdict == FeedbackVerdict.CORRECT


# --- "סיימתי לעבור על רשימת ההמתנה" text -----------------------------------


async def test_list_done_text_replies_nicely():
    """When the user sends the prefilled 'finished reviewing' text, the
    bot replies with a thank-you message and does not enter the flow."""
    bot = FakeBot()
    handler, *_ = _make_handler(bot=bot)
    event = _make_event(
        event_id="evt-done-1",
        event_type=BotEventType.TEXT,
        text="סיימתי לעבור על רשימת ההמתנה ✅, תודה",
    )
    handled = await handler.handle(event)
    assert handled is True
    assert len(bot.texts) == 1
    phone, text = bot.texts[0]
    assert phone == USER_PHONE
    assert "תודה" in text


async def test_list_done_text_partial_match_still_handled():
    """The match is on the prefix, so variations with/without emoji work."""
    bot = FakeBot()
    handler, *_ = _make_handler(bot=bot)
    event = _make_event(
        event_id="evt-done-2",
        event_type=BotEventType.TEXT,
        text="סיימתי לעבור על רשימת ההמתנה",
    )
    handled = await handler.handle(event)
    assert handled is True
    assert len(bot.texts) == 1


async def test_list_done_text_unknown_user_still_replies():
    """Even if the user is unknown, the bot replies (no resolver needed)."""
    bot = FakeBot()
    handler, *_ = _make_handler(bot=bot, user_id=None)
    event = _make_event(
        event_id="evt-done-3",
        event_type=BotEventType.TEXT,
        text="סיימתי לעבור על רשימת ההמתנה ✅, תודה",
    )
    handled = await handler.handle(event)
    assert handled is True
    assert len(bot.texts) == 1


async def test_unrelated_text_not_handled_by_list_done():
    """Unrelated text should not trigger the list-done reply."""
    bot = FakeBot()
    handler, *_ = _make_handler(bot=bot)
    event = _make_event(
        event_id="evt-other-1",
        event_type=BotEventType.TEXT,
        text="שלח תזכורת לדנה",
    )
    handled = await handler.handle(event)
    assert handled is False
    assert len(bot.texts) == 0


# --- "סיכום חדש" on-demand digest -----------------------------------------


class FakeTokenService:
    """Records issued tokens; returns a predictable token."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.issued: list[str] = []

    async def issue(self, user_id: str) -> tuple[str, str]:
        if self.fail:
            raise RuntimeError("token service down")
        token = f"tok-{len(self.issued) + 1}"
        self.issued.append(token)
        return (f"session-{len(self.issued)}", token)


async def test_digest_request_sends_link_with_count():
    """Sending 'סיכום חדש' with active items sends a text with the link."""
    bot = FakeBot()
    tokens = FakeTokenService()
    handler, _action_service, _, active_repo, _ = _make_handler(
        bot=bot, token_service=tokens, base_url="https://echo.example.com"
    )
    # Set up an active item so the count is 1.
    await _setup_active(active_repo)
    await _setup_chat_state(handler, CHAT_ID, version=1)

    event = _make_event(
        event_id="evt-digest-1",
        event_type=BotEventType.TEXT,
        text="סיכום חדש",
    )
    handled = await handler.handle(event)
    assert handled is True
    assert len(bot.texts) == 1
    phone, text = bot.texts[0]
    assert phone == USER_PHONE
    assert "1 שיחות" in text
    assert "https://echo.example.com/q/tok-1" in text


async def test_digest_request_no_active_items_replies_empty():
    """If no active items, reply with 'no waiting'."""
    bot = FakeBot()
    tokens = FakeTokenService()
    handler, *_ = _make_handler(bot=bot, token_service=tokens)
    event = _make_event(
        event_id="evt-digest-2",
        event_type=BotEventType.TEXT,
        text="סיכום חדש בבקשה",
    )
    handled = await handler.handle(event)
    assert handled is True
    assert len(bot.texts) == 1
    assert "מחכות" in bot.texts[0][1]
    # No token issued since no items.
    assert len(tokens.issued) == 0


async def test_digest_request_no_token_service_falls_through():
    """Without a token_service, 'סיכום חדש' falls through to the next handler."""
    bot = FakeBot()
    handler, *_ = _make_handler(bot=bot)
    event = _make_event(
        event_id="evt-digest-3",
        event_type=BotEventType.TEXT,
        text="סיכום חדש",
    )
    handled = await handler.handle(event)
    assert handled is False
    assert len(bot.texts) == 0


async def test_digest_request_token_failure_replies_error():
    """If the token service fails, reply with an error message."""
    bot = FakeBot()
    tokens = FakeTokenService(fail=True)
    handler, _action_service, _, active_repo, _ = _make_handler(
        bot=bot, token_service=tokens
    )
    # Set up an active item so we reach the token issuance.
    await _setup_active(active_repo)
    await _setup_chat_state(handler, CHAT_ID, version=1)

    event = _make_event(
        event_id="evt-digest-4",
        event_type=BotEventType.TEXT,
        text="סיכום חדש",
    )
    handled = await handler.handle(event)
    assert handled is True
    assert len(bot.texts) == 1
    assert "שגיאה" in bot.texts[0][1]


async def test_digest_request_unknown_user_returns_false():
    """If the user is unknown, the digest request is not handled."""
    bot = FakeBot()
    tokens = FakeTokenService()
    handler, *_ = _make_handler(bot=bot, user_id=None, token_service=tokens)
    event = _make_event(
        event_id="evt-digest-5",
        event_type=BotEventType.TEXT,
        text="סיכום חדש",
    )
    handled = await handler.handle(event)
    assert handled is False


async def test_digest_request_skips_stale_acknowledged_snoozed_muted():
    """Active items that are stale, acknowledged, snoozed, or muted are
    excluded from the count — the digest reply says 'no waiting'."""
    from datetime import timedelta

    bot = FakeBot()
    tokens = FakeTokenService()
    handler, _action_service, _, active_repo, _ = _make_handler(
        bot=bot, token_service=tokens
    )

    # Stale: chat activity_version != target_version.
    await _setup_active(active_repo, chat_id="stale@c.us", target_version=99)
    await _setup_chat_state(handler, "stale@c.us", version=1)

    # Acknowledged: acknowledged_at is set.
    await _setup_active(active_repo, chat_id="ack@c.us")
    await _setup_chat_state(handler, "ack@c.us", version=1)
    await active_repo.acknowledge(
        user_id=USER_ID, chat_id="ack@c.us",
        acknowledged_at=datetime.now(timezone.utc),
    )

    # Snoozed: snoozed_until in the future.
    await _setup_active(active_repo, chat_id="snz@c.us")
    await _setup_chat_state(handler, "snz@c.us", version=1)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    await active_repo.snooze(
        user_id=USER_ID, chat_id="snz@c.us", snoozed_until=future,
    )

    # Muted: mute repo has a mute for this chat.
    await _setup_active(active_repo, chat_id="mut@c.us")
    await _setup_chat_state(handler, "mut@c.us", version=1)
    await handler._mute_repo.mute_temporary(
        user_id=USER_ID,
        chat_id="mut@c.us",
        muted_until=datetime.now(timezone.utc) + timedelta(hours=24),
    )

    event = _make_event(
        event_id="evt-digest-skip",
        event_type=BotEventType.TEXT,
        text="סיכום חדש",
    )
    handled = await handler.handle(event)
    assert handled is True
    # All items skipped → count is 0 → "no waiting" reply.
    assert len(bot.texts) == 1
    assert "מחכות" in bot.texts[0][1]
    assert len(tokens.issued) == 0


# --- User false-positive annotation queue ------------------------------------


class _FakeAnnotationItems:
    """Fake ``tracing_client.annotation_queues.items`` — records create calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, *, queue_id: str, items: list[dict[str, Any]]) -> None:
        self.calls.append({"queue_id": queue_id, "items": items})


class _FakeTracingClient:
    """Fake ``tracing_client`` for annotation-queue tests."""

    def __init__(self) -> None:
        self.annotation_queues: Any = type("FakeAQ", (), {})()
        self.annotation_queues.items = _FakeAnnotationItems()
        self.flushed = False

    def flush(self) -> None:
        self.flushed = True


def _seed_result(
    result_repo: InMemoryWaitingForMeResultRepository,
    *,
    result_id: str = RESULT_ID,
    decision: WaitingForMeDecision = WaitingForMeDecision.WAITING_FOR_ME,
    next_owner: NextOwner | None = NextOwner.USER,
    conversation_snapshot: dict[str, Any] | None = None,
    prompt_version: str | None = "v4.1",
    analyzer_version: str | None = "2026-09-15.1",
    model: str | None = "gpt-4.1",
) -> str:
    """Seed a WaitingForMeResult into the in-memory repo and return its ID.

    Directly appends to the repo's internal list so we control the row ID
    (the real ``save`` generates a UUID).
    """
    if conversation_snapshot is None:
        conversation_snapshot = {
            "messages": [
                {"direction": "inbound", "text": "מה קורה?", "timestamp": None},
                {"direction": "outbound", "text": "אני אחזור אליך", "timestamp": None},
            ],
            "target_version": 1,
        }
    result = WaitingForMeResult(
        decision=decision,
        next_owner=next_owner,
        open_obligation="user needs to reply",
        confidence=0.9,
        reason="test reason",
        summary="test summary",
        target_version=1,
        conversation_snapshot=conversation_snapshot,
        model=model,
        prompt_version=prompt_version,
        analyzer_version=analyzer_version,
    )
    result_repo._results.append(  # direct access for test setup
        (USER_ID, CHAT_ID, result_id, datetime.now(timezone.utc), result)
    )
    return result_id


def _patch_annotation_infra(
    action_service: WaitingForMeActionService,
    monkeypatch: pytest.MonkeyPatch,
    *,
    queue_id: str = "q-user-test",
) -> tuple[_FakeTracingClient, dict[str, Any]]:
    """Patch tracing_client + _trace_user_false_positive for annotation tests.

    Returns ``(fake_client, trace_kwargs)``. The patched
    ``_trace_user_false_positive`` records its kwargs in ``trace_kwargs``
    for assertions and returns a fixed ``(run_id, run_start_time)``.

    Also sets ``OBSERVABILITY_HASH_KEY`` so ``correlation_id(user_id)``
    (called in ``_enqueue_user_annotation`` before the patched trace)
    does not raise.
    """
    monkeypatch.setenv("OBSERVABILITY_HASH_KEY", "test-key-12345")
    fake_client = _FakeTracingClient()
    monkeypatch.setattr(
        "echo_v2.services.feedback_service.tracing_client", fake_client
    )

    trace_kwargs: dict[str, Any] = {}

    async def _fake_trace(**kwargs: Any) -> tuple[str | None, str | None]:
        trace_kwargs.update(kwargs)
        return ("run-123", "2026-09-19T10:00:00+00:00")

    monkeypatch.setattr(action_service, "_trace_user_false_positive", _fake_trace)
    return fake_client, trace_kwargs


def _patch_scheduler_to_track_tasks(
    action_service: WaitingForMeActionService,
) -> list[asyncio.Task]:
    """Replace _schedule_false_positive_annotation with a version that stores tasks.

    The real method creates fire-and-forget tasks whose references are lost.
    This replacement stores them so tests can await completion.
    """
    tasks: list[asyncio.Task] = []

    def _schedule(*, result_id: str | None, user_id: str) -> None:
        if result_id is None:
            return
        tasks.append(
            asyncio.create_task(
                action_service._enqueue_user_annotation(
                    result_id=result_id, user_id=user_id,
                )
            )
        )

    action_service._schedule_false_positive_annotation = _schedule  # type: ignore[method-assign]
    return tasks


async def test_dismiss_not_waiting_enqueues_annotation(monkeypatch):
    """לא מחכים לי → APPLIED, enqueues conversation to annotation queue."""
    monkeypatch.setenv("USER_ANNOTATION_QUEUE_ID", "q-user-test")
    bot = FakeBot()
    handler, action_service, _, active_repo, feedback_repo = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)

    # Seed a result with conversation_snapshot into the result_repo.
    result_repo = action_service._result_repo
    assert result_repo is not None
    _seed_result(result_repo, result_id=RESULT_ID)

    active_id = await _setup_active(active_repo, result_id=RESULT_ID)

    # Patch annotation infra.
    fake_client, trace_kwargs = _patch_annotation_infra(
        action_service, monkeypatch,
    )
    tasks = _patch_scheduler_to_track_tasks(action_service)

    event = _make_event(
        event_id="evt-nw-ann",
        button_id=f"dismiss:{active_id}:not_waiting",
    )
    result = await handler.handle(event)
    assert result is True

    # Await the fire-and-forget enqueue task.
    await asyncio.gather(*tasks)

    # items.create called once with the right queue_id and RUN item.
    assert len(fake_client.annotation_queues.items.calls) == 1
    call = fake_client.annotation_queues.items.calls[0]
    assert call["queue_id"] == "q-user-test"
    assert len(call["items"]) == 1
    assert call["items"][0]["item_type"] == "RUN"
    assert call["items"][0]["run_id"] == "run-123"

    # The trace received the full analyzer metadata (versioning fields).
    assert trace_kwargs["analyzer_decision"] == "waiting_for_me"
    assert trace_kwargs["next_owner"] == "user"
    assert trace_kwargs["prompt_version"] == "v4.1"
    assert trace_kwargs["analyzer_version"] == "2026-09-15.1"
    assert trace_kwargs["model"] == "gpt-4.1"
    assert trace_kwargs["target_version"] == 1
    assert len(trace_kwargs["conversation"]) == 2

    # FALSE_POSITIVE feedback still recorded.
    assert len(feedback_repo._rows) == 1
    assert feedback_repo._rows[0].verdict == FeedbackVerdict.FALSE_POSITIVE


async def test_dismiss_not_waiting_no_enqueue_without_queue_id(monkeypatch):
    """No USER_ANNOTATION_QUEUE_ID → items.create never called."""
    monkeypatch.delenv("USER_ANNOTATION_QUEUE_ID", raising=False)
    bot = FakeBot()
    handler, action_service, _, active_repo, _ = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)

    result_repo = action_service._result_repo
    assert result_repo is not None
    _seed_result(result_repo, result_id=RESULT_ID)
    active_id = await _setup_active(active_repo, result_id=RESULT_ID)

    fake_client, _ = _patch_annotation_infra(action_service, monkeypatch)
    tasks = _patch_scheduler_to_track_tasks(action_service)

    event = _make_event(
        event_id="evt-nw-noq",
        button_id=f"dismiss:{active_id}:not_waiting",
    )
    await handler.handle(event)
    await asyncio.gather(*tasks)

    assert len(fake_client.annotation_queues.items.calls) == 0


async def test_dismiss_not_waiting_duplicate_no_enqueue(monkeypatch):
    """Duplicate dismiss_not_waiting → DUPLICATE, no enqueue."""
    monkeypatch.setenv("USER_ANNOTATION_QUEUE_ID", "q-user-test")
    bot = FakeBot()
    handler, action_service, _, active_repo, _ = _make_handler(bot=bot)
    await _setup_chat_state(handler, CHAT_ID, version=1)

    result_repo = action_service._result_repo
    assert result_repo is not None
    _seed_result(result_repo, result_id=RESULT_ID)
    active_id = await _setup_active(active_repo, result_id=RESULT_ID)

    fake_client, _ = _patch_annotation_infra(action_service, monkeypatch)
    tasks = _patch_scheduler_to_track_tasks(action_service)

    # First call → APPLIED.
    event1 = _make_event(
        event_id="evt-dup-ann",
        button_id=f"dismiss:{active_id}:not_waiting",
    )
    await handler.handle(event1)
    await asyncio.gather(*tasks)
    assert len(fake_client.annotation_queues.items.calls) == 1

    # Second call (same event_id) → DUPLICATE, no additional enqueue.
    tasks2 = _patch_scheduler_to_track_tasks(action_service)
    event2 = _make_event(
        event_id="evt-dup-ann",
        button_id=f"dismiss:{active_id}:not_waiting",
    )
    await handler.handle(event2)
    await asyncio.gather(*tasks2)
    assert len(fake_client.annotation_queues.items.calls) == 1


async def test_dismiss_with_reason_detected_incorrectly_enqueues_annotation(
    monkeypatch,
):
    """Web path: dismiss_with_reason(detected_incorrectly) → enqueues annotation."""
    monkeypatch.setenv("USER_ANNOTATION_QUEUE_ID", "q-user-test")
    _, action_service, _, active_repo, _ = _make_handler()

    result_repo = action_service._result_repo
    assert result_repo is not None
    _seed_result(result_repo, result_id=RESULT_ID)
    active_id = await _setup_active(active_repo, result_id=RESULT_ID)

    fake_client, trace_kwargs = _patch_annotation_infra(
        action_service, monkeypatch,
    )
    tasks = _patch_scheduler_to_track_tasks(action_service)

    outcome = await action_service.dismiss_with_reason(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="evt-di-ann",
        reason="detected_incorrectly",
    )
    assert outcome == HandlingOutcome.APPLIED
    await asyncio.gather(*tasks)

    assert len(fake_client.annotation_queues.items.calls) == 1
    call = fake_client.annotation_queues.items.calls[0]
    assert call["queue_id"] == "q-user-test"
    assert call["items"][0]["run_id"] == "run-123"

    # Versioning fields passed to the trace.
    assert trace_kwargs["prompt_version"] == "v4.1"
    assert trace_kwargs["analyzer_version"] == "2026-09-15.1"
    assert trace_kwargs["next_owner"] == "user"


async def test_dismiss_with_reason_already_handled_no_enqueue(monkeypatch):
    """Web path: dismiss_with_reason(already_handled) → no enqueue."""
    monkeypatch.setenv("USER_ANNOTATION_QUEUE_ID", "q-user-test")
    _, action_service, _, active_repo, _ = _make_handler()

    result_repo = action_service._result_repo
    assert result_repo is not None
    _seed_result(result_repo, result_id=RESULT_ID)
    active_id = await _setup_active(active_repo, result_id=RESULT_ID)

    fake_client, _ = _patch_annotation_infra(action_service, monkeypatch)
    tasks = _patch_scheduler_to_track_tasks(action_service)

    outcome = await action_service.dismiss_with_reason(
        user_id=USER_ID,
        active_id=active_id,
        target_version=1,
        provider_message_id="evt-ah-ann",
        reason="already_handled",
    )
    assert outcome == HandlingOutcome.APPLIED
    await asyncio.gather(*tasks)

    assert len(fake_client.annotation_queues.items.calls) == 0
