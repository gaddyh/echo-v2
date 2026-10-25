"""Repository protocol for guard_daily_stats.

No logging fallback — daily stats are reporting-critical data.
Tests use in-memory or real Postgres repos; production uses the real one.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from echo_v2.domain.guard_daily_stats import GuardDailyStats

__all__ = ["GuardDailyStatsRepository"]


@runtime_checkable
class GuardDailyStatsRepository(Protocol):
    """Persistence interface for daily Guard statistics."""

    async def upsert(self, stats: GuardDailyStats) -> None:
        """Insert or update a daily stats row (idempotent by child+date)."""
        ...

    async def get(
        self, child_user_id: str, local_date: date
    ) -> GuardDailyStats | None:
        """Fetch a single day's stats for a child."""
        ...

    async def list_for_range(
        self,
        *,
        child_user_id: str | None = None,
        date_from: date,
        date_to: date,
    ) -> list[GuardDailyStats]:
        """List daily stats in a date range, optionally filtered by child."""
        ...
