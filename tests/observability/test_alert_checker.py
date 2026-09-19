"""Tests for :mod:`echo_v2.observability.alert_checker`.

Covers the helper count functions, the per-rule check functions, the
``AlertChecker`` orchestration (``check_once``, ``run_loop``, ``_send_summary``)
and the cooldown / error-handling paths.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from echo_v2.observability.alert_checker import (
    COOLDOWN_SECONDS,
    DEFAULT_ALERT_RULES,
    AlertChecker,
    AlertRule,
    AlertSender,
    _count_overdue_chats,
    _count_status_since,
    check_analyzer_stuck,
    check_bot_send_errors,
    check_bot_send_indeterminate,
    check_high_error_rate,
)


# --- helpers ----------------------------------------------------------------


def _make_session(counts: list[int]) -> AsyncMock:
    """Build a mock AsyncSession whose ``execute().scalar_one()`` returns counts in order."""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one = MagicMock(side_effect=list(counts))
    session.execute = AsyncMock(return_value=result)
    return session


def _make_session_factory(session: AsyncMock) -> MagicMock:
    """Build a mock ``async_sessionmaker`` whose call returns an async cm yielding ``session``."""
    factory = MagicMock()
    cm = AsyncMock()
    cm.__aenter__.return_value = session
    factory.return_value = cm
    return factory


def _make_checker(
    session: AsyncMock | None = None,
    *,
    bot: AsyncMock | None = None,
    rules: list[AlertRule] | None = None,
    poll_interval_seconds: float = 300.0,
    cooldown_seconds: int = COOLDOWN_SECONDS,
) -> tuple[AlertChecker, AsyncMock, MagicMock]:
    session = session if session is not None else AsyncMock()
    bot = bot if bot is not None else AsyncMock()
    factory = _make_session_factory(session)
    checker = AlertChecker(
        bot=bot,
        owner_phone="+972500000000",
        session_factory=factory,
        rules=rules,
        poll_interval_seconds=poll_interval_seconds,
        cooldown_seconds=cooldown_seconds,
    )
    return checker, bot, factory


_NOW = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


# --- _count_status_since ----------------------------------------------------


async def test_count_status_since_executes_query_and_returns_int():
    session = _make_session([5])
    since = _NOW - timedelta(minutes=60)

    count = await _count_status_since(session, "failed", since)

    assert count == 5
    assert isinstance(count, int)
    session.execute.assert_awaited_once()


async def test_count_status_since_returns_zero():
    session = _make_session([0])
    count = await _count_status_since(session, "succeeded", _NOW)
    assert count == 0


# --- _count_overdue_chats ---------------------------------------------------


async def test_count_overdue_chats_executes_query_and_returns_int():
    session = _make_session([3])
    count = await _count_overdue_chats(session, _NOW, grace_minutes=10)
    assert count == 3
    session.execute.assert_awaited_once()


async def test_count_overdue_chats_returns_zero():
    session = _make_session([0])
    count = await _count_overdue_chats(session, _NOW)
    assert count == 0


# --- check_bot_send_errors --------------------------------------------------


async def test_check_bot_send_errors_triggered():
    session = _make_session([2])
    msg = await check_bot_send_errors(session, _NOW)
    assert msg is not None
    assert "Bot send failures" in msg
    assert "2" in msg


async def test_check_bot_send_errors_not_triggered():
    session = _make_session([0])
    msg = await check_bot_send_errors(session, _NOW)
    assert msg is None


# --- check_bot_send_indeterminate -------------------------------------------


async def test_check_bot_send_indeterminate_triggered():
    session = _make_session([1])
    msg = await check_bot_send_indeterminate(session, _NOW)
    assert msg is not None
    assert "Bot send indeterminate" in msg
    assert "1" in msg


async def test_check_bot_send_indeterminate_not_triggered():
    session = _make_session([0])
    msg = await check_bot_send_indeterminate(session, _NOW)
    assert msg is None


# --- check_analyzer_stuck ---------------------------------------------------


async def test_check_analyzer_stuck_triggered():
    session = _make_session([4])
    msg = await check_analyzer_stuck(session, _NOW)
    assert msg is not None
    assert "Analyzer stuck" in msg
    assert "4" in msg


async def test_check_analyzer_stuck_not_triggered():
    session = _make_session([0])
    msg = await check_analyzer_stuck(session, _NOW)
    assert msg is None


# --- check_high_error_rate --------------------------------------------------


async def test_check_high_error_rate_no_actions_returns_none():
    # succeeded=0, failed=0 -> total 0 -> None
    session = _make_session([0, 0])
    msg = await check_high_error_rate(session, _NOW)
    assert msg is None


async def test_check_high_error_rate_below_threshold_returns_none():
    # succeeded=9, failed=1 -> 10% -> None
    session = _make_session([9, 1])
    msg = await check_high_error_rate(session, _NOW)
    assert msg is None


async def test_check_high_error_rate_above_threshold_triggered():
    # succeeded=1, failed=4 -> 80% -> triggered
    session = _make_session([1, 4])
    msg = await check_high_error_rate(session, _NOW)
    assert msg is not None
    assert "High error rate" in msg
    assert "80%" in msg
    assert "4/5" in msg


async def test_check_high_error_rate_all_failures_triggered():
    # succeeded=0, failed=3 -> 100% -> triggered
    session = _make_session([0, 3])
    msg = await check_high_error_rate(session, _NOW)
    assert msg is not None
    assert "100%" in msg
    assert "3/3" in msg


# --- AlertChecker.__init__ --------------------------------------------------


def test_init_defaults():
    bot = AsyncMock()
    factory = MagicMock()
    checker = AlertChecker(
        bot=bot,
        owner_phone="+972500000000",
        session_factory=factory,
    )
    assert checker._bot is bot
    assert checker._owner_phone == "+972500000000"
    assert checker._session_factory is factory
    assert checker._rules is DEFAULT_ALERT_RULES
    assert checker._poll_interval == 300.0
    assert checker._cooldown == COOLDOWN_SECONDS
    assert checker._last_sent is None


def test_init_custom_values():
    bot = AsyncMock()
    factory = MagicMock()
    rules = [AlertRule(name="custom", check="bot_send_errors")]
    checker = AlertChecker(
        bot=bot,
        owner_phone="+972500000001",
        session_factory=factory,
        rules=rules,
        poll_interval_seconds=10.0,
        cooldown_seconds=60,
    )
    assert checker._rules is rules
    assert checker._poll_interval == 10.0
    assert checker._cooldown == 60


def test_alert_sender_protocol_runtime_checkable():
    class _FakeSender:
        async def send_text(self, recipient: str, text: str) -> str:
            return "ok"

    assert isinstance(_FakeSender(), AlertSender)


# --- check_once -------------------------------------------------------------


async def test_check_once_returns_triggered_messages():
    # 4 default rules; provide counts for each check.
    # bot_send_errors -> 1 (triggered)
    # bot_send_indeterminate -> 0 (not triggered)
    # analyzer_stuck -> 2 (triggered)
    # high_error_rate -> succeeded=1, failed=3 (triggered)
    session = _make_session([1, 0, 2, 1, 3])
    checker, bot, _ = _make_checker(session)

    triggered = await checker.check_once()

    assert len(triggered) == 3
    assert any("Bot send failures" in m for m in triggered)
    assert any("Analyzer stuck" in m for m in triggered)
    assert any("High error rate" in m for m in triggered)


async def test_check_once_no_alerts_returns_empty():
    # all checks return 0 / None
    session = _make_session([0, 0, 0, 0, 0])
    checker, _, _ = _make_checker(session)

    triggered = await checker.check_once()
    assert triggered == []


class _CaptureHandler(logging.Handler):
    """Logging handler that captures records, immune to caplog pollution."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _attach_capture(level: int) -> tuple[_CaptureHandler, logging.Logger]:
    """Attach a capture handler to the alert_checker logger. Returns (handler, logger).

    Other tests in the suite may disable this logger (setting ``.disabled =
    True``); we force it back on so our handler receives records.
    """
    logger = logging.getLogger("echo_v2.observability.alert_checker")
    handler = _CaptureHandler()
    handler.setLevel(level)
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.disabled = False  # re-enable if another test disabled it
    logger.propagate = True
    return handler, logger


async def test_check_once_unknown_check_skipped():
    session = AsyncMock()
    checker, _, _ = _make_checker(
        session,
        rules=[AlertRule(name="bogus", check="does_not_exist")],
    )
    handler, logger = _attach_capture(logging.WARNING)

    try:
        triggered = await checker.check_once()
        assert triggered == []
        assert any("unknown check" in r.getMessage() for r in handler.records)
    finally:
        logger.removeHandler(handler)


async def test_check_once_check_raises_is_caught_and_logged():
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=RuntimeError("boom"))
    checker, _, _ = _make_checker(
        session,
        rules=[AlertRule(name="Bot send errors", check="bot_send_errors")],
    )
    handler, logger = _attach_capture(logging.ERROR)

    try:
        triggered = await checker.check_once()
        assert triggered == []
        assert any("error checking alert rule" in r.getMessage() for r in handler.records)
    finally:
        logger.removeHandler(handler)


async def test_check_once_custom_rules_subset():
    session = _make_session([5])
    checker, _, _ = _make_checker(
        session,
        rules=[AlertRule(name="Bot send errors", check="bot_send_errors")],
    )

    triggered = await checker.check_once()
    assert len(triggered) == 1
    assert "Bot send failures" in triggered[0]


# --- _send_summary ----------------------------------------------------------


async def test_send_summary_sends_text_and_sets_last_sent():
    checker, bot, _ = _make_checker()
    assert checker._last_sent is None
    now = _NOW

    await checker._send_summary(["🔴 alert one", "🔴 alert two"], now)

    bot.send_text.assert_awaited_once()
    args = bot.send_text.call_args.args
    assert args[0] == "+972500000000"
    assert "Alert Summary" in args[1]
    assert "alert one" in args[1]
    assert "alert two" in args[1]
    assert "2" in args[1]
    assert checker._last_sent is now


async def test_send_summary_send_failure_is_logged():
    checker, bot, _ = _make_checker()
    bot.send_text = AsyncMock(side_effect=RuntimeError("network down"))
    handler, logger = _attach_capture(logging.ERROR)

    try:
        # should not raise
        await checker._send_summary(["🔴 alert"], _NOW)
        assert checker._last_sent == _NOW  # last_sent set even on failure
        assert any("failed to send alert summary" in r.getMessage() for r in handler.records)
    finally:
        logger.removeHandler(handler)


# --- run_loop ---------------------------------------------------------------


async def test_run_loop_sends_on_first_alert_then_cancels():
    checker, bot, _ = _make_checker(poll_interval_seconds=0.01)
    checker.check_once = AsyncMock(return_value=["🔴 alert"])

    task = asyncio.create_task(checker.run_loop())
    # let it run one iteration
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    bot.send_text.assert_awaited()
    assert checker._last_sent is not None


async def test_run_loop_suppresses_during_cooldown():
    checker, bot, _ = _make_checker(poll_interval_seconds=0.01, cooldown_seconds=3600)
    # pretend we already sent recently
    checker._last_sent = datetime.now(timezone.utc)
    checker.check_once = AsyncMock(return_value=["🔴 alert"])

    task = asyncio.create_task(checker.run_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    bot.send_text.assert_not_awaited()  # suppressed by cooldown


async def test_run_loop_sends_after_cooldown_elapsed():
    checker, bot, _ = _make_checker(poll_interval_seconds=0.01, cooldown_seconds=1)
    # last sent well in the past -> cooldown elapsed
    checker._last_sent = datetime.now(timezone.utc) - timedelta(hours=2)
    checker.check_once = AsyncMock(return_value=["🔴 alert"])

    task = asyncio.create_task(checker.run_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    bot.send_text.assert_awaited()


async def test_run_loop_no_alerts_does_not_send():
    checker, bot, _ = _make_checker(poll_interval_seconds=0.01)
    checker.check_once = AsyncMock(return_value=[])

    task = asyncio.create_task(checker.run_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    bot.send_text.assert_not_awaited()


async def test_run_loop_check_once_raises_continues():
    handler, logger = _attach_capture(logging.ERROR)
    checker, bot, _ = _make_checker(poll_interval_seconds=0.01)

    call_count = 0

    async def flaky_check():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient")
        return ["🔴 recovered"]

    checker.check_once = AsyncMock(side_effect=flaky_check)

    task = asyncio.create_task(checker.run_loop())
    await asyncio.sleep(0.08)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    try:
        # first iteration logged the error, second sent the summary
        assert any("alert checker error, continuing" in r.getMessage() for r in handler.records)
        bot.send_text.assert_awaited()
    finally:
        logger.removeHandler(handler)


async def test_run_loop_cancelled_during_sleep():
    checker, bot, _ = _make_checker(poll_interval_seconds=10.0)
    checker.check_once = AsyncMock(return_value=[])

    task = asyncio.create_task(checker.run_loop())
    await asyncio.sleep(0.05)  # it's now sleeping
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_run_loop_cancelled_during_check():
    checker, bot, _ = _make_checker(poll_interval_seconds=10.0)

    async def slow_check():
        await asyncio.sleep(5)
        return []

    checker.check_once = slow_check

    task = asyncio.create_task(checker.run_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_run_loop_logs_start():
    handler, logger = _attach_capture(logging.INFO)
    checker, _, _ = _make_checker(poll_interval_seconds=0.01)
    checker.check_once = AsyncMock(return_value=[])

    task = asyncio.create_task(checker.run_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    try:
        assert any("alert checker started" in r.getMessage() for r in handler.records)
    finally:
        logger.removeHandler(handler)
