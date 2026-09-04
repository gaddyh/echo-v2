"""Tests for the SchedulingFlowService state machine."""

from __future__ import annotations

from echo_v2.domain.scheduling import ScheduledActionType
from echo_v2.persistence.conversation_state import InMemoryConversationStateRepository
from echo_v2.persistence.scheduled_actions import InMemoryScheduledActionRepository
from echo_v2.persistence.user_resolver import InMemoryUserResolver
from echo_v2.ports.bot import BotContact, BotEvent, BotEventType
from echo_v2.runtime.idempotency import InMemoryIdempotencyStore
from echo_v2.services.scheduling import SchedulingService
from echo_v2.services.scheduling_flow import SchedulingFlowService
from echo_v2.services.time_parser import CombinedTimeParser

# --- fakes -----------------------------------------------------------------


class FakeBot:
    """Records sent messages."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, user_phone: str, text: str) -> None:
        self.sent.append((user_phone, text))


class FakeMessaging:
    """Not called in these tests, but needed for SchedulingService."""

    async def send_message(self, connection, chat_id, message) -> str:
        return "MSG_FAKE"


class FakeConnectionRepo:
    async def get_by_user(self, user_id: str):
        return None


def _make_flow_service(
    *,
    user_phone: str = "972500000001",
    user_id: str = "user-1",
    timezone: str = "Asia/Jerusalem",
) -> tuple[SchedulingFlowService, FakeBot, InMemoryScheduledActionRepository]:
    bot = FakeBot()
    state_repo = InMemoryConversationStateRepository()
    action_repo = InMemoryScheduledActionRepository()
    scheduling_service = SchedulingService(
        action_repo=action_repo,
        connection_repo=FakeConnectionRepo(),
        messaging=FakeMessaging(),
        idempotency_store=InMemoryIdempotencyStore(),
    )
    user_resolver = InMemoryUserResolver()
    user_resolver.add_user(user_phone, user_id, timezone)
    time_parser = CombinedTimeParser(llm_parser=None)

    flow = SchedulingFlowService(
        bot=bot,
        state_repo=state_repo,
        scheduling_service=scheduling_service,
        time_parser=time_parser,
        user_resolver=user_resolver,
    )
    return flow, bot, action_repo


def _contact_event(
    phone: str = "972526610653",
    name: str = "Dana",
    user_phone: str = "972500000001",
) -> BotEvent:
    return BotEvent(
        event_id="wamid.C1",
        user_phone=user_phone,
        type=BotEventType.CONTACT,
        contact=BotContact(phone=phone, name=name),
    )


def _text_event(
    text: str,
    event_id: str = "wamid.T1",
    user_phone: str = "972500000001",
) -> BotEvent:
    return BotEvent(
        event_id=event_id,
        user_phone=user_phone,
        type=BotEventType.TEXT,
        text=text,
    )


# --- step 1: contact -------------------------------------------------------


async def test_contact_starts_flow():
    flow, bot, _ = _make_flow_service()
    await flow.handle(_contact_event(name="Dana"))

    assert len(bot.sent) == 1
    phone, msg = bot.sent[0]
    assert phone == "972500000001"
    assert "Dana" in msg
    # State should be AWAITING_MESSAGE.
    ctx = await flow._state_repo.get("user-1")
    assert ctx.state.name == "AWAITING_MESSAGE"
    assert ctx.recipient_phone == "972526610653@c.us"


async def test_contact_replaces_existing_recipient():
    """Sending a new vCard in AWAITING_MESSAGE replaces the old recipient."""
    flow, _bot, _ = _make_flow_service()
    await flow.handle(_contact_event(name="Dana", phone="972526610653"))
    await flow.handle(_contact_event(name="Bob", phone="972555555555"))

    ctx = await flow._state_repo.get("user-1")
    assert ctx.recipient_name == "Bob"
    assert ctx.recipient_phone == "972555555555@c.us"


# --- step 2: message text --------------------------------------------------


async def test_text_in_idle_asks_for_contact():
    flow, bot, _ = _make_flow_service()
    await flow.handle(_text_event("hello"))

    assert len(bot.sent) == 1
    assert "איש קשר" in bot.sent[0][1] or "contact" in bot.sent[0][1].lower()


async def test_message_text_transitions_to_awaiting_time():
    flow, bot, _ = _make_flow_service()
    await flow.handle(_contact_event(name="Dana"))
    bot.sent.clear()

    await flow.handle(_text_event("hey, can you call me?"))

    ctx = await flow._state_repo.get("user-1")
    assert ctx.state.name == "AWAITING_TIME"
    assert ctx.message == "hey, can you call me?"
    assert "מתי" in bot.sent[0][1] or "when" in bot.sent[0][1].lower()


# --- step 3: time ----------------------------------------------------------


async def test_time_creates_scheduled_action():
    flow, bot, action_repo = _make_flow_service()
    await flow.handle(_contact_event(name="Dana"))
    await flow.handle(_text_event("call me please"))
    bot.sent.clear()

    # Use a relative time to avoid clock dependency.
    await flow.handle(_text_event("בעוד שעה", event_id="wamid.T2"))

    actions = await action_repo.list_pending("user-1")
    assert len(actions) == 1
    action = actions[0]
    assert action.type is ScheduledActionType.SEND_WHATSAPP_MESSAGE
    assert action.payload["chat_id"] == "972526610653@c.us"
    assert action.payload["message"] == "call me please"

    # Flow state should be cleared.
    ctx = await flow._state_repo.get("user-1")
    assert ctx.state.name == "IDLE"

    # Confirmation sent.
    assert len(bot.sent) == 1
    assert "✅" in bot.sent[0][1]


async def test_time_parse_failure_asks_to_rephrase():
    flow, bot, _ = _make_flow_service()
    await flow.handle(_contact_event(name="Dana"))
    await flow.handle(_text_event("call me"))
    bot.sent.clear()

    await flow.handle(_text_event("sometime next week maybe", event_id="wamid.T2"))

    # Should still be in AWAITING_TIME.
    ctx = await flow._state_repo.get("user-1")
    assert ctx.state.name == "AWAITING_TIME"
    # Should have sent a rephrase request.
    assert len(bot.sent) == 1


# --- cancel ----------------------------------------------------------------


async def test_cancel_resets_to_idle():
    flow, bot, _ = _make_flow_service()
    await flow.handle(_contact_event(name="Dana"))
    await flow.handle(_text_event("call me"))

    await flow.handle(_text_event("בטל", event_id="wamid.CANCEL"))

    ctx = await flow._state_repo.get("user-1")
    assert ctx.state.name == "IDLE"
    assert "בוטל" in bot.sent[-1][1]


# --- unknown user ----------------------------------------------------------


async def test_unknown_user_gets_message():
    flow, bot, _ = _make_flow_service()
    await flow.handle(_text_event("hi", user_phone="999999999999"))

    assert len(bot.sent) == 1
    assert "connect" in bot.sent[0][1].lower() or "know" in bot.sent[0][1].lower()
