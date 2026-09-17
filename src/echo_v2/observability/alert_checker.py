"""Periodic alert checker — queries LangSmith and sends WhatsApp summaries.

A background task that polls LangSmith every ``poll_interval`` seconds,
checks configured alert rules, and sends a WhatsApp summary to
``ECHO_OWNER_PHONE`` via the 360dialog bot when any rules fire.
Rate-limited to at most one message per hour.

Alert rules are defined in :data:`DEFAULT_ALERT_RULES` and can be
overridden via the constructor.

Usage::

    checker = AlertChecker(
        bot=d360_client,
        owner_phone="+972...",
        project_id="7e3d5b21-...",
        api_key="lsv2_pt_...",
    )
    await checker.run_loop()  # or checker.check_once()
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable

import httpx

__all__ = ["DEFAULT_ALERT_RULES", "AlertChecker", "AlertRule", "AlertSender"]

_logger = logging.getLogger("echo_v2.observability.alert_checker")

API_BASE = "https://api.smith.langchain.com"
COOLDOWN_SECONDS = 3600  # at most one alert per hour


@runtime_checkable
class AlertSender(Protocol):
    async def send_text(self, recipient: str, text: str) -> str: ...


@dataclass(frozen=True)
class AlertRule:
    """A single alert condition.

    Modes:
    - ``"errors"``: count runs matching ``filter`` with ``status=error``
      in the last ``window_minutes``. Fire if count >= ``threshold``.
    - ``"absence"``: count all runs matching ``filter`` in the last
      ``window_minutes``. Fire if count < ``threshold`` (i.e. expected
      at least ``threshold`` runs but found fewer).
    - ``"error_rate"``: compute error rate for runs matching ``filter``
      in the last ``window_minutes``. Fire if rate > ``threshold``
      (threshold is a fraction 0-1).
    """

    name: str
    filter: str
    window_minutes: int
    mode: str  # "errors", "absence", "error_rate"
    threshold: float = 1


DEFAULT_ALERT_RULES: list[AlertRule] = [
    AlertRule(
        name="Analyzer errors",
        filter='eq(name, "wfm.analysis")',
        window_minutes=60,
        mode="errors",
        threshold=1,
    ),
    AlertRule(
        name="LLM analysis errors",
        filter='eq(name, "wfm.llm_analyze")',
        window_minutes=60,
        mode="errors",
        threshold=1,
    ),
    AlertRule(
        name="Bot send errors",
        filter='eq(name, "wfm.scheduling.bot_send")',
        window_minutes=60,
        mode="errors",
        threshold=1,
    ),
    AlertRule(
        name="Ingest errors",
        filter='eq(name, "wfm.ingest.green")',
        window_minutes=60,
        mode="errors",
        threshold=1,
    ),
    AlertRule(
        name="No analyzer runs (worker may be stuck)",
        filter='eq(name, "wfm.analysis")',
        window_minutes=30,
        mode="absence",
        threshold=1,
    ),
    AlertRule(
        name="High error rate",
        filter="",  # all runs
        window_minutes=60,
        mode="error_rate",
        threshold=0.2,
    ),
]


class AlertChecker:
    """Polls LangSmith and sends WhatsApp alert summaries.

    Args:
        bot: A ``send_text(recipient, text)`` capable client (Dialog360Client).
        owner_phone: Phone number to send alerts to.
        project_id: LangSmith tracing project UUID.
        api_key: LangSmith API key.
        rules: Alert rules to check. Defaults to :data:`DEFAULT_ALERT_RULES`.
        poll_interval_seconds: How often to run checks. Default 300 (5 min).
        cooldown_seconds: Min seconds between alert messages. Default 3600 (1h).
    """

    def __init__(
        self,
        *,
        bot: AlertSender,
        owner_phone: str,
        project_id: str,
        api_key: str,
        rules: list[AlertRule] | None = None,
        poll_interval_seconds: float = 300.0,
        cooldown_seconds: int = COOLDOWN_SECONDS,
        api_base: str = API_BASE,
    ) -> None:
        self._bot = bot
        self._owner_phone = owner_phone
        self._project_id = project_id
        self._api_key = api_key
        self._api_base = api_base
        self._rules = rules or DEFAULT_ALERT_RULES
        self._poll_interval = poll_interval_seconds
        self._cooldown = cooldown_seconds
        self._last_sent: datetime | None = None
        self._headers = {"x-api-key": api_key, "Content-Type": "application/json"}

    async def check_once(self) -> list[str]:
        """Run all alert rules once. Returns list of triggered alert messages.

        Does NOT send a WhatsApp message — caller decides what to do with
        the results. :meth:`run_loop` uses this internally and sends summaries.
        """
        now = datetime.now(timezone.utc)
        triggered: list[str] = []

        for rule in self._rules:
            try:
                msg = await self._check_rule(rule, now)
                if msg:
                    triggered.append(msg)
            except Exception:
                _logger.exception("error checking alert rule: %s", rule.name)

        return triggered

    async def _check_rule(self, rule: AlertRule, now: datetime) -> str | None:
        """Check a single rule. Returns a message if triggered, None otherwise."""
        since = now - timedelta(minutes=rule.window_minutes)
        # Query runs matching the filter in the window
        run_filter = rule.filter
        if rule.mode == "errors":
            # Add status=error to the filter
            if run_filter:
                run_filter = f'and({run_filter}, eq(status, "error"))'
            else:
                run_filter = 'eq(status, "error")'

        runs = await self._query_runs(run_filter, since, now, limit=100)
        count = len(runs)

        if rule.mode == "errors":
            if count >= rule.threshold:
                return f"🔴 {rule.name} ({count} in last {rule.window_minutes}min)"
        elif rule.mode == "absence":
            if count < rule.threshold:
                return f"🔴 {rule.name} (only {count} in last {rule.window_minutes}min)"
        elif rule.mode == "error_rate":
            # Query all runs (no status filter) for the denominator
            all_runs = await self._query_runs(rule.filter, since, now, limit=500)
            total = len(all_runs)
            if total == 0:
                return None
            error_count = sum(1 for r in all_runs if r.get("status") == "error")
            rate = error_count / total
            if rate > rule.threshold:
                pct = int(rate * 100)
                return f"🔴 {rule.name} ({pct}% in last {rule.window_minutes}min, threshold {int(rule.threshold * 100)}%)"

        return None

    async def _query_runs(
        self,
        run_filter: str,
        since: datetime,
        until: datetime,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query LangSmith runs API for runs matching the filter."""
        payload: dict[str, Any] = {
            "session": [self._project_id],
            "limit": limit,
            "order": "desc",
            "start_time": since.isoformat(),
            "end_time": until.isoformat(),
        }
        if run_filter:
            payload["filter"] = run_filter

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._api_base}/api/v1/runs/query",
                headers=self._headers,
                json=payload,
            )
            resp.raise_for_status()
            return list(resp.json().get("runs", []))

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
