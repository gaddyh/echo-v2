"""Tests for DigestReplyService."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from echo_v2.domain.waiting_for_me import WaitingForMeActive
from echo_v2.persistence.chat_repositories import (
    InMemoryChatStateRepository,
    InMemoryMessageRepository,
    InMemoryWaitingForMeActiveRepository,
)
from echo_v2.persistence.contacts import InMemoryContactRepository
from echo_v2.ports.bot import BotEvent, BotEventType
from echo_v2.services.digest_reply import DigestReplyService

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 12, 6, 0, 0, tzinfo=timezone.utc)
USER_ID = "user-1"
USER_PHONE = "972501234567"


class FakeBot:
    """Fake BotChannel that records sent messages."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, user_phone: str, text: str) -> None:
        self.sent.append((user_phone, text))

    async def send_template(self, user_phone, template_name, language, body_params) -> str:
        return "fake-msg-id"


class FakeUserResolver:
    """Returns (user_id, timezone) or None."""

    def __init__(self, user_id: str | None = USER_ID, timezone: str = "Asia/Jerusalem"):
        self._user_id = user_id
        self._timezone = timezone

    async def resolve(self, phone: str) -> tuple[str, str] | None:
        if self._user_id is None:
            return None
        return (self._user_id, self._timezone)


def _make_active(chat_id="972508765432@c.us", waiting_since=NOW, target_version=1):
    return WaitingForMeActive(
        user_id=USER_ID,
        chat_id=chat_id,
        target_version=target_version,
        result_id="result-1",
        waiting_since=waiting_since,
    )


async def _setup_chat_with_active(
    chat_state_repo, active_repo, chat_id="972508765432@c.us", version=1
):
    from echo_v2.ports.whatsapp import MessageDirection

    await chat_state_repo.upsert_on_message(
        user_id=USER_ID,
        chat_id=chat_id,
        direction=MessageDirection.INBOUND,
        observed_at=NOW,
        next_analysis_at=None,
    )
    for _ in range(1, version):
        await chat_state_repo.upsert_on_message(
            user_id=USER_ID,
            chat_id=chat_id,
            direction=MessageDirection.INBOUND,
            observed_at=NOW,
            next_analysis_at=None,
        )
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id=chat_id,
        target_version=version,
        result_id="result-1",
        waiting_since=NOW,
    )


def _make_event(text="הצג הכול"):
    return BotEvent(
        event_id="wamid.TEST",
        user_phone=USER_PHONE,
        type=BotEventType.TEXT,
        text=text,
        timestamp=NOW,
    )


async def test_handles_show_all_button():
    bot = FakeBot()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()

    await _setup_chat_with_active(chat_state_repo, active_repo)

    service = DigestReplyService(
        bot=bot,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        user_resolver=FakeUserResolver(),
    )
    handled = await service.handle(_make_event())
    assert handled is True
    assert len(bot.sent) == 1
    assert "הרשימה המלאה" in bot.sent[0][1]


async def test_does_not_handle_unrelated_text():
    bot = FakeBot()
    service = DigestReplyService(
        bot=bot,
        active_repo=InMemoryWaitingForMeActiveRepository(),
        chat_state_repo=InMemoryChatStateRepository(),
        message_repo=InMemoryMessageRepository(),
        contact_repo=InMemoryContactRepository(),
        user_resolver=FakeUserResolver(),
    )
    handled = await service.handle(_make_event(text="מחר ב-8"))
    assert handled is False
    assert len(bot.sent) == 0


async def test_does_not_handle_contact_event():
    bot = FakeBot()
    service = DigestReplyService(
        bot=bot,
        active_repo=InMemoryWaitingForMeActiveRepository(),
        chat_state_repo=InMemoryChatStateRepository(),
        message_repo=InMemoryMessageRepository(),
        contact_repo=InMemoryContactRepository(),
        user_resolver=FakeUserResolver(),
    )
    event = BotEvent(
        event_id="wamid.C",
        user_phone=USER_PHONE,
        type=BotEventType.CONTACT,
        timestamp=NOW,
    )
    handled = await service.handle(event)
    assert handled is False


async def test_unknown_user_sends_message():
    bot = FakeBot()
    service = DigestReplyService(
        bot=bot,
        active_repo=InMemoryWaitingForMeActiveRepository(),
        chat_state_repo=InMemoryChatStateRepository(),
        message_repo=InMemoryMessageRepository(),
        contact_repo=InMemoryContactRepository(),
        user_resolver=FakeUserResolver(user_id=None),
    )
    handled = await service.handle(_make_event())
    assert handled is True
    assert len(bot.sent) == 1
    assert "don't know you" in bot.sent[0][1]


async def test_no_active_states_sends_empty_message():
    bot = FakeBot()
    service = DigestReplyService(
        bot=bot,
        active_repo=InMemoryWaitingForMeActiveRepository(),
        chat_state_repo=InMemoryChatStateRepository(),
        message_repo=InMemoryMessageRepository(),
        contact_repo=InMemoryContactRepository(),
        user_resolver=FakeUserResolver(),
    )
    handled = await service.handle(_make_event())
    assert handled is True
    assert len(bot.sent) == 1
    assert "אין כרגע" in bot.sent[0][1]


async def test_stale_active_excluded():
    bot = FakeBot()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()

    await _setup_chat_with_active(chat_state_repo, active_repo, version=2)
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id="972508765432@c.us",
        target_version=1,  # stale
        result_id="result-1",
        waiting_since=NOW,
    )

    service = DigestReplyService(
        bot=bot,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        user_resolver=FakeUserResolver(),
    )
    handled = await service.handle(_make_event())
    assert handled is True
    assert len(bot.sent) == 1
    assert "אין כרגע" in bot.sent[0][1]


async def test_full_list_includes_all_items():
    bot = FakeBot()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()

    await _setup_chat_with_active(chat_state_repo, active_repo, chat_id="chat-a@c.us")
    await _setup_chat_with_active(chat_state_repo, active_repo, chat_id="chat-b@c.us")

    service = DigestReplyService(
        bot=bot,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        user_resolver=FakeUserResolver(),
    )
    handled = await service.handle(_make_event())
    assert handled is True
    text = bot.sent[0][1]
    # Both chat IDs should appear (as phone fallback since no contacts)
    assert "chat-a" in text
    assert "chat-b" in text
