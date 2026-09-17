"""Periodic alert checker — queries PostgreSQL and sends WhatsApp summaries.

A background task that polls the database every ``poll_interval`` seconds,
checks configured alert rules, and sends a WhatsApp summary to
``ECHO_OWNER_PHONE`` via the 360dialog bot when any rules fire.
Rate-limited to at most one message per hour.

Alert rules are defined in :data:`DEFAULT_ALERT_RULES` and can be
overridden via the constructor.

Usage::

    checker = AlertChecker(
        bot=d360_client,
        owner_phone="+972...",
        session_factory=repos.session_factory,
    )
    await checker.run_loop()  # or checker.check_once()
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.persistence.orm import (
    ChatRow,
    ScheduledActionRow,
)

__all__ = ["DEFAULT_ALERT_RULES", "AlertChecker", "AlertRule", "AlertSender"]

_logger = logging.getLogger("echo_v2.observability.alert_checker")

COOLDOWN_SECONDS = 3600  # at most one alert per hour


@runtime_checkable
class AlertSender(Protocol):
    async def send_text(self, recipient: str, text: str) -> str: ...


@dataclass(frozen=True)
class AlertRule:
    """A single alert condition checked against the local database.

    Each rule has a ``check`` coroutine that receives the session and
    the current time, and returns a message string if triggered or
    ``None`` otherwise.
    """

    name: str
    check: str  # name of the check method on AlertChecker


async def _count_status_since(
    session: AsyncSession, status: str, since: datetime
) -> int:
    """Count scheduled_actions with given status updated since ``since``."""
    result = await session.execute(
        select(func.count()).select_from(ScheduledActionRow).where(
            ScheduledActionRow.status == status,
            ScheduledActionRow.updated_at >= since,
        )
    )
    return int(result.scalar_one())


async def _count_overdue_chats(
    session: AsyncSession, now: datetime, grace_minutes: int = 10
) -> int:
    """Count chats that are due for analysis but haven't been processed.

    A chat is "overdue" if:
    - ``next_analysis_at`` is set and past (due for analysis)
    - ``activity_version > last_processed_version`` (not caught up)
    - ``next_analysis_at`` is older than ``grace_minutes`` (not just waiting
      for the poll cycle)
    """
    threshold = now - timedelta(minutes=grace_minutes)
    result = await session.execute(
        select(func.count()).select_from(ChatRow).where(
            ChatRow.next_analysis_at.is_not(None),
            ChatRow.next_analysis_at <= threshold,
            ChatRow.activity_version > ChatRow.last_processed_version,
        )
    )
    return int(result.scalar_one())


# --- check functions (referenced by AlertRule.check) -----------------------


async def check_bot_send_errors(session: AsyncSession, now: datetime) -> str | None:
    """Failed scheduled actions (bot sends) in the last 60 min."""
    since = now - timedelta(minutes=60)
    count = await _count_status_since(session, "failed", since)
    if count >= 1:
        return f"🔴 Bot send failures ({count} in last 60min)"
    return None


async def check_bot_send_indeterminate(
    session: AsyncSession, now: datetime
) -> str | None:
    """Indeterminate scheduled actions (send outcome unknown) in last 60 min."""
    since = now - timedelta(minutes=60)
    count = await _count_status_since(session, "indeterminate", since)
    if count >= 1:
        return f"🔴 Bot send indeterminate ({count} in last 60min)"
    return None


async def check_analyzer_stuck(session: AsyncSession, now: datetime) -> str | None:
    """Chats overdue for analysis by more than 10 minutes."""
    count = await _count_overdue_chats(session, now, grace_minutes=10)
    if count >= 1:
        return f"🔴 Analyzer stuck ({count} chats overdue >10min)"
    return None


async def check_high_error_rate(session: AsyncSession, now: datetime) -> str | None:
    """Scheduled action failure rate > 20% in the last 60 min."""
    since = now - timedelta(minutes=60)
    succeeded = await _count_status_since(session, "succeeded", since)
    failed = await _count_status_since(session, "failed", since)
    total = succeeded + failed
    if total == 0:
        return None
    rate = failed / total
    if rate > 0.2:
        pct = int(rate * 100)
        return f"🔴 High error rate ({pct}% in last 60min, {failed}/{total} failed)"
    return None


CHECKS = {
    "bot_send_errors": check_bot_send_errors,
    "bot_send_indeterminate": check_bot_send_indeterminate,
    "analyzer_stuck": check_analyzer_stuck,
    "high_error_rate": check_high_error_rate,
}


DEFAULT_ALERT_RULES: list[AlertRule] = [
    AlertRule(name="Bot send errors", check="bot_send_errors"),
    AlertRule(name="Bot send indeterminate", check="bot_send_indeterminate"),
    AlertRule(name="Analyzer stuck", check="analyzer_stuck"),
    AlertRule(name="High error rate", check="high_error_rate"),
]


class AlertChecker:
    """Polls the database and sends WhatsApp alert summaries.

    Args:
        bot: A ``send_text(recipient, text)`` capable client (Dialog360Client).
        owner_phone: Phone number to send alerts to.
        session_factory: SQLAlchemy async session factory.
        rules: Alert rules to check. Defaults to :data:`DEFAULT_ALERT_RULES`.
        poll_interval_seconds: How often to run checks. Default 300 (5 min).
        cooldown_seconds: Min seconds between alert messages. Default 3600 (1h).
    """

    def __init__(
        self,
        *,
        bot: AlertSender,
        owner_phone: str,
        session_factory: async_sessionmaker[AsyncSession],
        rules: list[AlertRule] | None = None,
        poll_interval_seconds: float = 300.0,
        cooldown_seconds: int = COOLDOWN_SECONDS,
    ) -> None:
        self._bot = bot
        self._owner_phone = owner_phone
        self._session_factory = session_factory
        self._rules = rules or DEFAULT_ALERT_RULES
        self._poll_interval = poll_interval_seconds
        self._cooldown = cooldown_seconds
        self._last_sent: datetime | None = None

    async def check_once(self) -> list[str]:
        """Run all alert rules once. Returns list of triggered alert messages."""
        now = datetime.now(timezone.utc)
        triggered: list[str] = []

        async with self._session_factory() as session:
            for rule in self._rules:
                check_fn = CHECKS.get(rule.check)
                if check_fn is None:
                    _logger.warning("unknown check: %s", rule.check)
                    continue
                try:
                    msg = await check_fn(session, now)
                    if msg:
                        triggered.append(msg)
                except Exception:
                    _logger.exception("error checking alert rule: %s", rule.name)

        return triggered

    async def run_loop(self) -> None:
        """Poll forever, sending summaries when alerts fire.

        Rate-limited: at most one WhatsApp message per ``cooldown_seconds``.
        If alerts fire during cooldown, they are silently skipped (the next
        check will catch them if they persist).
        """
        _logger.info("alert checker started (poll every %.0fs)", self._poll_interval)
        while True:
            try:
                triggered = await self.check_once()
                if triggered:
                    now = datetime.now(timezone.utc)
                    if self._last_sent is not None:
                        elapsed = (now - self._last_sent).total_seconds()
                        if elapsed < self._cooldown:
                            _logger.debug(
                                "alert suppressed (cooldown %.0fs/%.0fs): %d rules",
                                elapsed, self._cooldown, len(triggered),
                            )
                        else:
                            await self._send_summary(triggered, now)
                    else:
                        await self._send_summary(triggered, now)
            except asyncio.CancelledError:
                _logger.info("alert checker cancelled")
                raise
            except Exception:
                _logger.exception("alert checker error, continuing")
            try:
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                _logger.info("alert checker cancelled during sleep")
                raise

    async def _send_summary(self, triggered: list[str], now: datetime) -> None:
        """Send a WhatsApp summary of triggered alerts."""
        lines = [f"⚠️ Echo v2 — Alert Summary ({len(triggered)})\n"]
        lines.extend(triggered)
        text = "\n".join(lines)
        self._last_sent = now
        try:
            await self._bot.send_text(self._owner_phone, text)
            _logger.info("alert summary sent (%d rules)", len(triggered))
        except Exception:
            _logger.exception("failed to send alert summary")
