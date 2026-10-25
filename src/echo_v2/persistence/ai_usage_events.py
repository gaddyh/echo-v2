"""Persistence port for billable AI usage events.

The repository records one row per actual provider call. The unique
``invocation_id`` makes persistence retries idempotent (``ON CONFLICT DO
NOTHING``); a genuinely separate billable attempt gets a fresh
``invocation_id`` and its own row.

There is intentionally **no logging fallback implementation**. For
billing-related analytics, silently substituting log-only persistence
produces misleading totals. Tests use an explicitly disabled recorder
(``recorder=None``); production either has the real recorder wired or it
doesn't — never a silent log substitute.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from echo_v2.domain.ai_usage_event import AiUsageEvent


@runtime_checkable
class AiUsageEventRepository(Protocol):
    async def record(self, event: AiUsageEvent) -> None:
        """Persist one usage event. Idempotent on ``invocation_id``."""
        ...

    async def list_for_range(
        self,
        *,
        child_user_id: str | None = None,
        date_from: datetime,
        date_to: datetime,
        limit: int = 10_000,
    ) -> list[AiUsageEvent]:
        """Return usage events in the half-open ``[date_from, date_to)`` range.

        When ``child_user_id`` is ``None``, all events (including
        unattributed) are returned.
        """
        ...


__all__ = ["AiUsageEventRepository"]
