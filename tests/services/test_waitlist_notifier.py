"""Tests for the Dialog360 waitlist notifier."""

from __future__ import annotations

import pytest

from echo_v2.services.waitlist_notifier import Dialog360WaitlistNotifier

pytestmark = pytest.mark.asyncio


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, recipient: str, text: str) -> str:
        self.sent.append((recipient, text))
        return "msg_id"


async def test_notify_sends_formatted_message():
    bot = _FakeBot()
    notifier = Dialog360WaitlistNotifier(
        bot=bot, owner_phone="0546610653",
    )
    await notifier.notify(
        name="דנה לוי",
        phone="+972546610653",
        willingness_to_pay="30_70",
    )
    assert len(bot.sent) == 1
    recipient, text = bot.sent[0]
    assert recipient == "+972546610653"
    assert "דנה לוי" in text
    assert "+972546610653" in text
    assert "30–70 ₪" in text


async def test_notify_without_wtp_shows_dash():
    bot = _FakeBot()
    notifier = Dialog360WaitlistNotifier(
        bot=bot, owner_phone="+972546610653",
    )
    await notifier.notify(name="דנה", phone="+972546610653")
    _, text = bot.sent[0]
    assert "WTP: —" in text


async def test_notify_swallows_bot_failure():
    class BoomBot:
        async def send_text(self, recipient, text):
            raise RuntimeError("boom")

    notifier = Dialog360WaitlistNotifier(
        bot=BoomBot(), owner_phone="+972546610653",
    )
    # Must not raise.
    await notifier.notify(name="דנה", phone="+972546610653")


def test_invalid_owner_phone_raises():
    import pytest as _pytest

    with _pytest.raises(ValueError):
        Dialog360WaitlistNotifier(bot=_FakeBot(), owner_phone="123456")
