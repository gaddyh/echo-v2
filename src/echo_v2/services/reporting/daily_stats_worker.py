"""DailyStatsWorker — periodic daily stats aggregation worker.

Follows the DigestWorker pattern (poll-loop + asyncio.create_task in lifespan)
with a nonblocking PG advisory lock for single-owner safety.

The worker periodically rebuilds daily stats for recent days. It uses
``pg_try_advisory_xact_lock`` (nonblocking) on a hashed key like
``guard_reporting:{local_date}`` — a worker that can't acquire the lock
skips immediately, avoiding redundant rebuilds across instances without
serializing them.

Aggregation is **reproducible**, not finalized: late messages or enrichment
results arriving after the grace window trigger a re-aggregation of that day.
Explicit rebuild is always allowed (via GuardReportingService.rebuild_day).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.persistence.guard_daily_stats import GuardDailyStatsRepository
from echo_v2.services.reporting.guard_reporting_service import GuardReportingService

__all__ = ["DailyStatsWorker"]

_logger = logging.getLogger(__name__)

# How far back to look for children needing rebuild (days).
_LOOKBACK_DAYS = 3

# Poll interval between rebuild sweeps (seconds).
_POLL_INTERVAL_SECONDS = 300  # 5 minutes


class DailyStatsWorker:
    """Periodic worker that rebuilds daily stats for monitored children.

    Uses a nonblocking advisory lock per (child, date) so concurrent
    instances don't duplicate work. Late arrivals trigger re-aggregation
    within a bounded lookback window.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        reporting_service: GuardReportingService,
        daily_stats_repo: GuardDailyStatsRepository,
        poll_interval_seconds: float = _POLL_INTERVAL_SECONDS,
        lookback_days: int = _LOOKBACK_DAYS,
        default_timezone: str = "Asia/Jerusalem",
    ) -> None:
        self._session_factory = session_factory
        self._reporting_service = reporting_service
        self._daily_stats_repo = daily_stats_repo
        self._poll_interval = poll_interval_seconds
        self._lookback_days = lookback_days
        self._default_timezone = default_timezone

    async def run_once(self, *, now_utc: datetime | None = None) -> int:
        """Run one sweep of daily stats rebuild.

        Returns the number of (child, date) rows rebuilt.
        """
        now = now_utc or datetime.now(timezone.utc)
        rebuilt = 0

        # Get all monitored children (users with guardian links).
        child_ids = await self._get_monitored_children()
        if not child_ids:
            _logger.debug("No monitored children found — skipping daily stats rebuild")
            return 0

        # Rebuild the last N days for each child.
        dates_to_rebuild = self._dates_to_rebuild(now)

        for child_id in child_ids:
            for local_date in dates_to_rebuild:
                try:
                    acquired = await self._try_lock_and_rebuild(
                        child_id=child_id,
                        local_date=local_date,
                    )
                    if acquired:
                        rebuilt += 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _logger.exception(
                        "Failed to rebuild daily stats for child=%s date=%s",
                        child_id,
                        local_date,
                    )

        _logger.info("Daily stats rebuild complete: %d rows rebuilt", rebuilt)
        return rebuilt

    async def run_loop(self) -> None:
        """Run the worker in a loop until cancelled."""
        _logger.info("DailyStatsWorker started (poll=%ds, lookback=%dd)",
                     self._poll_interval, self._lookback_days)
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                _logger.info("DailyStatsWorker loop cancelled")
                raise
            except Exception:
                _logger.exception("DailyStatsWorker sweep failed")

            await asyncio.sleep(self._poll_interval)

    async def _try_lock_and_rebuild(
        self,
        *,
        child_id: str,
        local_date: date,
    ) -> bool:
        """Try to acquire a nonblocking advisory lock and rebuild.

        Returns True if the lock was acquired and the rebuild ran,
        False if another worker holds the lock.
        """
        lock_key = self._lock_key(child_id, local_date)
        async with self._session_factory() as session:
            result = await session.execute(
                text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": lock_key},
            )
            acquired = result.scalar()
            if not acquired:
                _logger.debug(
                    "Lock not acquired for child=%s date=%s — skipping",
                    child_id,
                    local_date,
                )
                return False

            # Lock acquired — rebuild within this transaction.
            await self._reporting_service.rebuild_day(
                child_user_id=child_id,
                local_date=local_date,
                timezone_name=self._default_timezone,
            )
            await session.commit()
            return True

    def _lock_key(self, child_id: str, local_date: date) -> int:
        """Compute a stable 64-bit advisory lock key from child+date.

        Uses SHA-256 hash truncated to 15 hex digits (fits in BIGINT).
        """
        raw = f"guard_reporting:{child_id}:{local_date.isoformat()}"
        h = hashlib.sha256(raw.encode()).hexdigest()[:15]
        return int(h, 16)

    def _dates_to_rebuild(self, now_utc: datetime) -> list[date]:
        """Return the list of dates to rebuild (last N days, excluding today).

        Today is rebuilt too if it's late enough in the day (past 23:00 UTC).
        """
        today = now_utc.date()
        dates = []
        for i in range(1, self._lookback_days + 1):
            dates.append(today - timedelta(days=i))
        # Include today if it's late enough
        if now_utc.hour >= 23:
            dates.append(today)
        return dates

    async def _get_monitored_children(self) -> list[str]:
        """Get all child user IDs that have guardian links.

        These are the children for whom daily stats should be maintained.
        """

        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT DISTINCT child_user_id FROM guardian_child_links"
                )
            )
            rows = result.fetchall()
            return [str(row[0]) for row in rows]
