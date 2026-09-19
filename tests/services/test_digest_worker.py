"""Tests for DigestWorker."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch

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
        self.url_suffixes: list[str | None] = []
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
        self.url_suffixes.append(url_suffix)
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


def _make_query_service(
    *,
    active_repo=None,
    chat_state_repo=None,
    message_repo=None,
    contact_repo=None,
    mute_repo=None,
):
    """Build a WaitingListQueryService wired with view-supporting repos."""
    from echo_v2.services.waiting_list_query import WaitingListQueryService

    return WaitingListQueryService(
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        mute_repo=mute_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
    )


def _make_worker(
    *,
    digest_repo,
    active_repo,
    chat_state_repo,
    message_repo,
    contact_repo,
    bot,
    user_provider,
    mute_repo=None,
    token_service=None,
    poll_interval_seconds=None,
    template_name=None,
):
    """Build a DigestWorker wired with the shared query service."""
    query_service = _make_query_service(
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        mute_repo=mute_repo,
    )
    kwargs = {
        "digest_repo": digest_repo,
        "query_service": query_service,
        "bot": bot,
        "user_provider": user_provider,
    }
    if token_service is not None:
        kwargs["token_service"] = token_service
    if poll_interval_seconds is not None:
        kwargs["poll_interval_seconds"] = poll_interval_seconds
    if template_name is not None:
        kwargs["template_name"] = template_name
    return DigestWorker(**kwargs)


async def test_digest_sent_when_in_window_and_has_active():
    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()

    await _setup_chat_with_active(chat_state_repo, active_repo)

    worker = _make_worker(
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

    worker = _make_worker(
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

    worker = _make_worker(
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

    worker = _make_worker(
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

    worker = _make_worker(
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

    worker = _make_worker(
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

    worker = _make_worker(
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

    worker = _make_worker(
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

    worker = _make_worker(
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

    worker = _make_worker(
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

    worker = _make_worker(
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
    future = datetime.now(timezone.utc) + timedelta(hours=10)
    await active_repo.snooze(
        user_id=USER_ID, chat_id="972508765432@c.us", snoozed_until=future,
    )

    worker = _make_worker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider([(USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי")]),
    )
    sent = await worker.run_once(now_utc=datetime.now(timezone.utc))
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

    worker = _make_worker(
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

    worker = _make_worker(
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

    worker = _make_worker(
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


# --- Edge cases ---


async def test_digest_run_once_continues_on_user_exception():
    """If _process_user raises, run_once continues to the next user."""
    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()

    await _setup_chat_with_active(chat_state_repo, active_repo, chat_id="chat-b@c.us")

    # First user raises (bad tz), second user succeeds.
    worker = _make_worker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider([
            ("user-bad", USER_PHONE, "Invalid/Zone", "בדיקה"),
            (USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי"),
        ]),
    )
    sent = await worker.run_once(now_utc=NOW)
    # user-bad raises, user-1 succeeds.
    assert sent == 1


async def test_digest_uses_query_service_when_provided():
    """When query_service is wired, it's used instead of _get_current_active."""
    from echo_v2.persistence.feedback_repositories import InMemoryChatMuteRepository

    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    mute_repo = InMemoryChatMuteRepository()
    bot = FakeBot()

    await _setup_chat_with_active(chat_state_repo, active_repo)

    worker = _make_worker(
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
    assert sent == 1
    assert len(bot.sent) == 1


async def test_digest_token_issue_failure_still_sends():
    """If token issue fails, the digest is still sent without url_suffix."""
    from echo_v2.persistence.waiting_list_tokens import (
        InMemoryWaitingListSessionRepository,
    )
    from echo_v2.services.waiting_list_token_service import WaitingListTokenService

    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()

    await _setup_chat_with_active(chat_state_repo, active_repo)

    # Create a token service with a broken repo.
    class BrokenRepo(InMemoryWaitingListSessionRepository):
        async def create(self, **kwargs):
            raise RuntimeError("db down")

    token_service = WaitingListTokenService(BrokenRepo())

    worker = _make_worker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider([(USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי")]),
        token_service=token_service,
    )
    sent = await worker.run_once(now_utc=NOW)
    assert sent == 1
    assert len(bot.sent) == 1


async def test_digest_with_token_service_sends_url_suffix():
    """When token service is wired and works, url_suffix is passed to send_template."""
    from echo_v2.persistence.waiting_list_tokens import (
        InMemoryWaitingListSessionRepository,
    )
    from echo_v2.services.waiting_list_token_service import WaitingListTokenService

    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()

    await _setup_chat_with_active(chat_state_repo, active_repo)

    token_service = WaitingListTokenService(InMemoryWaitingListSessionRepository())

    worker = _make_worker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider([(USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי")]),
        token_service=token_service,
    )
    sent = await worker.run_once(now_utc=NOW)
    assert sent == 1
    assert len(bot.sent) == 1


async def test_digest_no_first_name_uses_fallback():
    """When user has no first_name, the fallback 'חבר' is used."""
    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()

    await _setup_chat_with_active(chat_state_repo, active_repo)

    worker = _make_worker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider([(USER_ID, USER_PHONE, "Asia/Jerusalem", None)]),
    )
    sent = await worker.run_once(now_utc=NOW)
    assert sent == 1
    _phone, _name, _lang, body_params = bot.sent[0]
    assert body_params[0] == "חבר"


# --- send_digest_for_user ---


async def test_send_digest_for_user_no_active_items():
    """No active items → returns False, no template sent."""
    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()

    worker = _make_worker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider(
            [(USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי")]
        ),
    )
    result = await worker.send_digest_for_user(
        USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי"
    )
    assert result is False
    assert len(bot.sent) == 0


async def test_send_digest_for_user_success():
    """Active items + success → returns True, template sent with correct params."""
    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()

    await _setup_chat_with_active(chat_state_repo, active_repo)

    worker = _make_worker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider(
            [(USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי")]
        ),
    )
    result = await worker.send_digest_for_user(
        USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי"
    )
    assert result is True
    assert len(bot.sent) == 1
    _phone, template_name, language, body_params = bot.sent[0]
    assert template_name == "morning_waiting_digest6"
    assert language == "he"
    assert body_params[0] == "גדי"
    assert body_params[1] == "1"


async def test_send_digest_for_user_send_failure():
    """Active items + send failure → returns False."""
    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()
    bot.error = RuntimeError("send failed")

    await _setup_chat_with_active(chat_state_repo, active_repo)

    worker = _make_worker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider(
            [(USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי")]
        ),
    )
    result = await worker.send_digest_for_user(
        USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי"
    )
    assert result is False


async def test_send_digest_for_user_with_token_service():
    """With token_service → url_suffix included in send_template call."""
    from echo_v2.persistence.waiting_list_tokens import (
        InMemoryWaitingListSessionRepository,
    )
    from echo_v2.services.waiting_list_token_service import WaitingListTokenService

    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()

    await _setup_chat_with_active(chat_state_repo, active_repo)

    token_service = WaitingListTokenService(InMemoryWaitingListSessionRepository())

    worker = _make_worker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider(
            [(USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי")]
        ),
        token_service=token_service,
    )
    result = await worker.send_digest_for_user(
        USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי"
    )
    assert result is True
    assert len(bot.sent) == 1
    assert bot.url_suffixes[0] is not None


async def test_send_digest_for_user_without_token_service():
    """Without token_service → url_suffix is None."""
    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()

    await _setup_chat_with_active(chat_state_repo, active_repo)

    worker = _make_worker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider(
            [(USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי")]
        ),
    )
    result = await worker.send_digest_for_user(
        USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי"
    )
    assert result is True
    assert bot.url_suffixes[0] is None


async def test_send_digest_for_user_first_name_none():
    """First name None → uses fallback 'חבר'."""
    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()

    await _setup_chat_with_active(chat_state_repo, active_repo)

    worker = _make_worker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider(
            [(USER_ID, USER_PHONE, "Asia/Jerusalem", None)]
        ),
    )
    result = await worker.send_digest_for_user(
        USER_ID, USER_PHONE, "Asia/Jerusalem", None
    )
    assert result is True
    _phone, _name, _lang, body_params = bot.sent[0]
    assert body_params[0] == "חבר"


async def test_send_digest_for_user_token_service_raises():
    """Token service raises → url_suffix is None, digest still sent."""
    from echo_v2.persistence.waiting_list_tokens import (
        InMemoryWaitingListSessionRepository,
    )
    from echo_v2.services.waiting_list_token_service import WaitingListTokenService

    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()

    await _setup_chat_with_active(chat_state_repo, active_repo)

    class BrokenRepo(InMemoryWaitingListSessionRepository):
        async def create(self, **kwargs):
            raise RuntimeError("db down")

    token_service = WaitingListTokenService(BrokenRepo())

    worker = _make_worker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider(
            [(USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי")]
        ),
        token_service=token_service,
    )
    result = await worker.send_digest_for_user(
        USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי"
    )
    assert result is True
    assert bot.url_suffixes[0] is None


# --- run_loop error handling ---


@patch.object(DigestWorker, "run_once", new_callable=AsyncMock)
async def test_run_loop_continues_on_run_once_exception(mock_run_once: AsyncMock) -> None:
    """When run_once raises an exception, loop continues (sleep then run_once again)."""
    mock_run_once.side_effect = ValueError("boom")

    worker = _make_worker(
        digest_repo=InMemoryDailyDigestRepository(),
        active_repo=InMemoryWaitingForMeActiveRepository(),
        chat_state_repo=InMemoryChatStateRepository(),
        message_repo=InMemoryMessageRepository(),
        contact_repo=InMemoryContactRepository(),
        bot=FakeBot(),
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
    assert mock_run_once.call_count >= 2


@patch.object(DigestWorker, "run_once", new_callable=AsyncMock)
async def test_run_loop_succeeds_then_sleeps(mock_run_once: AsyncMock) -> None:
    """When run_once succeeds, loop sleeps and continues."""
    mock_run_once.return_value = 0

    worker = _make_worker(
        digest_repo=InMemoryDailyDigestRepository(),
        active_repo=InMemoryWaitingForMeActiveRepository(),
        chat_state_repo=InMemoryChatStateRepository(),
        message_repo=InMemoryMessageRepository(),
        contact_repo=InMemoryContactRepository(),
        bot=FakeBot(),
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
    assert mock_run_once.call_count >= 2


@patch("echo_v2.services.digest_worker.asyncio.sleep", new_callable=AsyncMock)
@patch.object(DigestWorker, "run_once", new_callable=AsyncMock)
async def test_run_loop_cancelled_during_run_once(
    mock_run_once: AsyncMock, mock_sleep: AsyncMock
) -> None:
    """CancelledError during run_once is re-raised."""
    mock_run_once.side_effect = asyncio.CancelledError()

    worker = _make_worker(
        digest_repo=InMemoryDailyDigestRepository(),
        active_repo=InMemoryWaitingForMeActiveRepository(),
        chat_state_repo=InMemoryChatStateRepository(),
        message_repo=InMemoryMessageRepository(),
        contact_repo=InMemoryContactRepository(),
        bot=FakeBot(),
        user_provider=_make_user_provider([]),
        poll_interval_seconds=0.01,
    )
    with pytest.raises(asyncio.CancelledError):
        await worker.run_loop()
    mock_sleep.assert_not_called()


@patch("echo_v2.services.digest_worker.asyncio.sleep", new_callable=AsyncMock)
@patch.object(DigestWorker, "run_once", new_callable=AsyncMock)
async def test_run_loop_cancelled_during_sleep(
    mock_run_once: AsyncMock, mock_sleep: AsyncMock
) -> None:
    """CancelledError during sleep is re-raised."""
    mock_run_once.return_value = 0
    mock_sleep.side_effect = asyncio.CancelledError()

    worker = _make_worker(
        digest_repo=InMemoryDailyDigestRepository(),
        active_repo=InMemoryWaitingForMeActiveRepository(),
        chat_state_repo=InMemoryChatStateRepository(),
        message_repo=InMemoryMessageRepository(),
        contact_repo=InMemoryContactRepository(),
        bot=FakeBot(),
        user_provider=_make_user_provider([]),
        poll_interval_seconds=0.01,
    )
    with pytest.raises(asyncio.CancelledError):
        await worker.run_loop()
    assert mock_run_once.call_count == 1


async def test_run_once_propagates_cancelled_error():
    """CancelledError from _process_user is re-raised (not swallowed)."""
    digest_repo = InMemoryDailyDigestRepository()
    active_repo = InMemoryWaitingForMeActiveRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    bot = FakeBot()

    worker = _make_worker(
        digest_repo=digest_repo,
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        bot=bot,
        user_provider=_make_user_provider([(USER_ID, USER_PHONE, "Asia/Jerusalem", "גדי")]),
    )

    with patch.object(
        worker, "_process_user", side_effect=asyncio.CancelledError()
    ):
        with pytest.raises(asyncio.CancelledError):
            await worker.run_once(now_utc=NOW)
