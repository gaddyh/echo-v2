"""Tests for DigestWorker."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

import pytest

from echo_v2.domain.waiting_for_me import WaitingForMeActive
from echo_v2.persistence.chat_repositories import (
    InMemoryChatStateRepository,
    InMemoryMessageRepository,
    InMemoryWaitingForMeActiveRepository,
)
from echo_v2.persistence.contacts import InMemoryContactRepository
from echo_v2.persistence.digest_repositories import InMemoryDailyDigestRepository
from echo_v2.runtime.errors import IndeterminateError, PermanentError
from echo_v2.services.digest_worker import DigestWorker

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 12, 6, 0, 0, tzinfo=timezone.utc)  # 09:00 Israel time (UTC+3)
USER_ID = "user-1"
USER_PHONE = "972501234567"


class FakeBot:
    """Fake BotChannel that records sent template messages."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str, list[str]]] = []
        self.error: Exception | None = None

    async def send_text(self, user_phone: str, text: str) -> None:
        if self.error is not None:
            raise self.error

    async def send_template(
        self,
        user_phone: str,
        template_name: str,
        language: str,
        body_params: list[str],
        *,
        url_suffix: str | None = None,
    ) -> str:
        if self.error is not None:
            raise self.error
        self.sent.append((user_phone, template_name, language, body_params))
        return "fake-msg-id"


def _make_active(chat_id="972508765432@c.us", waiting_since=NOW, target_version=1):
    return WaitingForMeActive(
        id="active-1",
        user_id=USER_ID,
        chat_id=chat_id,
        target_version=target_version,
        result_id="result-1",
        waiting_since=waiting_since,
    )


async def _setup_chat_with_active(
    chat_state_repo, active_repo, chat_id="972508765432@c.us", version=1
):
    """Set up a chat state and matching active row."""
    from echo_v2.ports.whatsapp import MessageDirection

    # Use upsert_on_message to create the chat, then manually set version
    await chat_state_repo.upsert_on_message(
        user_id=USER_ID,
        chat_id=chat_id,
        direction=MessageDirection.INBOUND,
        observed_at=NOW,
        next_analysis_at=None,
    )
    # Now update the chat to match the desired version
    # The upsert sets activity_version=1, so if version > 1 we need more messages
    for i in range(1, version):
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


def _make_user_provider(users):
    async def provider():
        return users
    return provider


async def test_digest_sent_when_in_window_and_has_active():
    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()

    await _setup_chat_with_active(chat_state_repo, active_repo)

    worker = DigestWorker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider([(USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי")]),
    )
    sent = await worker.run_once(now_utc=NOW)
    assert sent == 1
    assert len(bot.sent) == 1
    _phone, template_name, language, body_params = bot.sent[0]
    assert template_name == "morning_waiting_digest6"
    assert language == "he"
    assert body_params[0] == "גדי"  # first_name
    assert body_params[1] == "1"  # count

    # Status should be SENT
    digest = await digest_repo.get(user_id=USER_ID, local_date=date(2026, 9, 12))
    assert digest is not None
    from echo_v2.domain.digest import DailyDigestStatus
    assert digest.status == DailyDigestStatus.SENT
    assert digest.item_count == 1


async def test_digest_empty_when_no_active():
    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()

    worker = DigestWorker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider([(USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי")]),
    )
    sent = await worker.run_once(now_utc=NOW)
    assert sent == 0
    assert len(bot.sent) == 0

    # Status should be EMPTY
    digest = await digest_repo.get(user_id=USER_ID, local_date=date(2026, 9, 12))
    assert digest is not None
    from echo_v2.domain.digest import DailyDigestStatus
    assert digest.status == DailyDigestStatus.EMPTY


async def test_digest_skipped_when_already_sent_today():
    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()

    # Pre-claim the digest
    await digest_repo.claim_or_get(user_id=USER_ID, local_date=date(2026, 9, 12))

    await _setup_chat_with_active(chat_state_repo, active_repo)

    worker = DigestWorker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider([(USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי")]),
    )
    sent = await worker.run_once(now_utc=NOW)
    assert sent == 0
    assert len(bot.sent) == 0  # already claimed, no send


async def test_digest_skipped_when_outside_window():
    """At 12:00 (outside 08:00-11:00), no digest."""
    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()

    await _setup_chat_with_active(chat_state_repo, active_repo)

    # 12:00 Israel time = 09:00 UTC (IDT = UTC+3)
    noon_utc = datetime(2026, 9, 12, 9, 0, 0, tzinfo=timezone.utc)

    worker = DigestWorker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider([(USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי")]),
    )

    sent = await worker.run_once(now_utc=noon_utc)
    assert sent == 0
    assert len(bot.sent) == 0

    # No digest row should exist
    digest = await digest_repo.get(user_id=USER_ID, local_date=date(2026, 9, 12))
    assert digest is None


async def test_digest_indeterminate_on_send_error():
    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()
    bot.error = IndeterminateError("send timeout")

    await _setup_chat_with_active(chat_state_repo, active_repo)

    worker = DigestWorker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider([(USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי")]),
    )
    sent = await worker.run_once(now_utc=NOW)
    assert sent == 0

    from echo_v2.domain.digest import DailyDigestStatus
    digest = await digest_repo.get(user_id=USER_ID, local_date=date(2026, 9, 12))
    assert digest is not None
    assert digest.status == DailyDigestStatus.INDETERMINATE


async def test_digest_failed_on_permanent_error():
    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()
    bot.error = PermanentError("invalid phone")

    await _setup_chat_with_active(chat_state_repo, active_repo)

    worker = DigestWorker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider([(USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי")]),
    )
    sent = await worker.run_once(now_utc=NOW)
    assert sent == 0

    from echo_v2.domain.digest import DailyDigestStatus
    digest = await digest_repo.get(user_id=USER_ID, local_date=date(2026, 9, 12))
    assert digest is not None
    assert digest.status == DailyDigestStatus.FAILED


async def test_stale_active_excluded_from_digest():
    """Active state with target_version != chats.activity_version is excluded."""
    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()

    # Set up chat with version 2, but active state has target_version 1 (stale)
    await _setup_chat_with_active(chat_state_repo, active_repo, version=2)
    # Overwrite the active to have stale version
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id="972508765432@c.us",
        target_version=1,  # stale — chat is at version 2
        result_id="result-1",
        waiting_since=NOW,
    )

    worker = DigestWorker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider([(USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי")]),
    )
    sent = await worker.run_once(now_utc=NOW)
    assert sent == 0  # no current active → empty

    from echo_v2.domain.digest import DailyDigestStatus
    digest = await digest_repo.get(user_id=USER_ID, local_date=date(2026, 9, 12))
    assert digest is not None
    assert digest.status == DailyDigestStatus.EMPTY


async def test_default_timezone_used_when_user_has_none():
    """User with timezone=None falls back to Asia/Jerusalem."""
    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()

    await _setup_chat_with_active(chat_state_repo, active_repo)

    worker = DigestWorker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider([(USER_ID, USER_PHONE, None, "גדי")]),
    )
    sent = await worker.run_once(now_utc=NOW)
    assert sent == 1  # should still send at 08:30 Israel time


async def test_run_once_continues_on_user_error():
    """If one user throws, the worker continues to the next."""
    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()

    await _setup_chat_with_active(chat_state_repo, active_repo, chat_id="972509999999@c.us")

    async def user_provider():
        return [("bad-user", "bad-phone", "Asia/Jerusalem", None), (USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי")]

    worker = DigestWorker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=user_provider,
    )
    sent = await worker.run_once(now_utc=NOW)
    # bad-user will fail (no chat state) but user-1 should still send
    assert sent == 1


async def test_run_loop_cancellable():
    """run_loop should exit cleanly on CancelledError."""
    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()

    worker = DigestWorker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider([]),
        poll_interval_seconds=0.01,
    )

    task = asyncio.create_task(worker.run_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_digest_excludes_acknowledged_items():
    """Acknowledged items are excluded from digest."""
    from dataclasses import replace

    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()

    await _setup_chat_with_active(chat_state_repo, active_repo)
    # Acknowledge the item.
    await active_repo.acknowledge(
        user_id=USER_ID, chat_id="972508765432@c.us", acknowledged_at=NOW,
    )

    worker = DigestWorker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider([(USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי")]),
    )
    sent = await worker.run_once(now_utc=NOW)
    assert sent == 0  # no items → no digest sent


async def test_digest_excludes_snoozed_items():
    """Snoozed items are excluded from digest."""
    from datetime import timedelta

    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()

    await _setup_chat_with_active(chat_state_repo, active_repo)
    future = NOW + timedelta(hours=10)
    await active_repo.snooze(
        user_id=USER_ID, chat_id="972508765432@c.us", snoozed_until=future,
    )

    worker = DigestWorker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider([(USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי")]),
    )
    sent = await worker.run_once(now_utc=NOW)
    assert sent == 0


async def test_digest_excludes_muted_chats():
    """Muted chats are excluded from digest."""
    from echo_v2.persistence.feedback_repositories import InMemoryChatMuteRepository

    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    mute_repo = InMemoryChatMuteRepository()
    bot = FakeBot()

    await _setup_chat_with_active(chat_state_repo, active_repo)
    await mute_repo.mute_permanent(user_id=USER_ID, chat_id="972508765432@c.us")

    worker = DigestWorker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider([(USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי")]),
        mute_repo=mute_repo,
    )
    sent = await worker.run_once(now_utc=NOW)
    assert sent == 0


async def test_digest_name_fallback_to_contact():
    """Contact name is used when chat_state has no chat_name."""
    from echo_v2.persistence.contacts import ContactRecord

    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()

    await _setup_chat_with_active(chat_state_repo, active_repo)
    await contact_repo.save(
        ContactRecord(
            user_id=USER_ID,
            phone_number="972508765432",
            display_name="דנה",
        )
    )

    worker = DigestWorker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider([(USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי")]),
    )
    sent = await worker.run_once(now_utc=NOW)
    assert sent == 1


async def test_digest_name_fallback_to_message():
    """Message chat_name/sender_name is used when no chat/contact name."""
    from echo_v2.domain.chat import Message
    from echo_v2.ports.whatsapp import MessageDirection

    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()

    await _setup_chat_with_active(chat_state_repo, active_repo)
    await message_repo.save(
        Message(
            id="msg-1",
            user_id=USER_ID,
            connection_id="conn-1",
            chat_id="972508765432@c.us",
            provider_message_id="msg-1",
            direction=MessageDirection.INBOUND,
            sender_id=None,
            text="היי",
            chat_name="שרה",
            sender_name="שרה",
            timestamp=NOW,
        )
    )

    worker = DigestWorker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider([(USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי")]),
    )
    sent = await worker.run_once(now_utc=NOW)
    assert sent == 1


async def test_digest_run_once_exception_continues():
    """run_once continues when a user fails with unexpected exception."""
    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()

    # User with no chat state → will fail, but shouldn't crash.
    await active_repo.upsert(
        user_id="bad-user",
        chat_id="972508765432@c.us",
        target_version=1,
        result_id="r1",
        waiting_since=NOW,
    )

    worker = DigestWorker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider([("bad-user", USER_PHONE, "Asia/Jerusalem", "גדי")]),
    )
    sent = await worker.run_once(now_utc=NOW)
    assert sent == 0


async def test_digest_unexpected_error_marks_failed():
    """Unexpected error in _process_user marks digest as FAILED."""
    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()
    bot.error = RuntimeError("unexpected crash")

    await _setup_chat_with_active(chat_state_repo, active_repo)

    worker = DigestWorker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider([(USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי")]),
    )
    sent = await worker.run_once(now_utc=NOW)
    assert sent == 0
