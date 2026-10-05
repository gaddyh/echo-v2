from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from echo_v2.bot.commands import BotCommandParser, GuardOpen
from echo_v2.persistence.guard_repositories import InMemoryGuardianChildLinkRepository
from echo_v2.ports.bot import BotEvent, BotEventType
from tests.services.test_feedback_handler import _make_event, _make_handler

pytestmark = pytest.mark.asyncio


def test_guard_command_parser() -> None:
    event = BotEvent(event_id="guard", user_phone="972501234567", type=BotEventType.TEXT, text=" guard ")
    assert isinstance(BotCommandParser().parse(event), GuardOpen)


async def test_guard_command_sends_link_to_active_guardian() -> None:
    links = InMemoryGuardianChildLinkRepository()
    from datetime import datetime, timezone
    await links.upsert_active(guardian_user_id="user-1", child_user_id="child", now=datetime.now(timezone.utc))
    tokens = AsyncMock()
    tokens.issue = AsyncMock(return_value=("session", "raw-token"))
    handler, _, bot, _, _ = _make_handler(token_service=tokens, guardian_child_links=links)
    await handler.handle_guard_open(_make_event(event_type=BotEventType.TEXT, text="guard"))
    tokens.issue.assert_awaited_once_with("user-1")
    bot.send_text.assert_awaited_once()
    assert "/q/guard/raw-token" in bot.send_text.await_args.args[1]


async def test_guard_command_is_silent_for_non_guardian() -> None:
    links = InMemoryGuardianChildLinkRepository()
    tokens = AsyncMock()
    handler, _, bot, _, _ = _make_handler(token_service=tokens, guardian_child_links=links)
    await handler.handle_guard_open(_make_event(event_type=BotEventType.TEXT, text="guard"))
    tokens.issue.assert_not_awaited()
    bot.send_text.assert_not_awaited()


async def test_guard_command_returns_when_dependencies_are_missing() -> None:
    handler, _, bot, _, _ = _make_handler(token_service=None, guardian_child_links=None)
    await handler.handle_guard_open(_make_event(event_type=BotEventType.TEXT, text="guard"))
    bot.send_text.assert_not_awaited()


async def test_guard_command_reports_token_failure() -> None:
    links = InMemoryGuardianChildLinkRepository()
    from datetime import datetime, timezone
    await links.upsert_active(guardian_user_id="user-1", child_user_id="child", now=datetime.now(timezone.utc))
    tokens = AsyncMock()
    tokens.issue = AsyncMock(side_effect=RuntimeError("expected"))
    handler, _, bot, _, _ = _make_handler(token_service=tokens, guardian_child_links=links)
    await handler.handle_guard_open(_make_event(event_type=BotEventType.TEXT, text="guard"))
    bot.send_text.assert_awaited_once_with("972501234567", "אירעה שגיאה. נסה שוב.")
