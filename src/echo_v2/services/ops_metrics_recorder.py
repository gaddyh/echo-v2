"""Best-effort in-process recorder for the ``ops_metrics`` aggregate table.

Design (Echo Operations Center telemetry category C):

* **Counters** — ``increment(metric, count=…, sum_ms=…, max_ms=…)`` buffers
  an in-memory delta keyed by ``(bucket, metric, provider, worker)``.
  ``sum_ms``/``max_ms`` are generic numeric sum/max columns (they hold
  milliseconds for latency metrics and bytes for size metrics).
* **Gauges** — ``gauge(metric, value)`` records a sampled value;
  last-write-wins within a bucket.
* **Flush** — ``run()`` is the background loop: every ``flush_interval``
  it coalesces the buffer into one ``flush`` (single transaction, upsert
  increment) and, once an hour, prunes buckets older than the retention
  window.

Failure semantics are strictly best-effort: any persistence error is
logged and the buffered points for that flush are dropped. Telemetry can
never fail a caller — ``increment``/``gauge`` do no I/O, and callers on
the ingestion critical path pass the optional recorder and skip it when
absent. A process restart loses at most one flush interval of counters
(documented limitation; the per-batch ``pipeline_events`` rows remain the
exact record).

No high-cardinality dimensions: provider and worker_id only — never
message ids, chat ids or user ids (per-user data lives in real tables).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from echo_v2.persistence.ops_metrics import (
    OpsMetricPoint,
    OpsMetricsRepository,
)

__all__ = ["OpsMetricsRecorder"]

_logger = logging.getLogger("echo_v2.services.ops_metrics_recorder")

_NA = ""


class OpsMetricsRecorder:
    def __init__(
        self,
        repo: OpsMetricsRepository,
        *,
        bucket_seconds: int = 60,
        flush_interval_seconds: float = 60.0,
        retention_days: int = 30,
    ) -> None:
        self._repo = repo
        self._bucket_seconds = bucket_seconds
        self._flush_interval_seconds = flush_interval_seconds
        self._retention_days = retention_days
        self._buffer: dict[tuple[datetime, str, str, str], OpsMetricPoint] = {}
        self._last_retention_sweep: datetime | None = None

    # -- write side (no I/O) ---------------------------------------------------

    def increment(
        self,
        metric: str,
        *,
        count: int = 1,
        sum_ms: float | None = None,
        max_ms: float | None = None,
        provider: str = _NA,
        worker_id: str = _NA,
    ) -> None:
        """Buffer a counter delta. Never raises, never blocks."""
        key = (_bucket_start(self._bucket_seconds), metric, provider, worker_id)
        point = OpsMetricPoint(
            bucket_start=key[0],
            metric=metric,
            provider=provider,
            worker_id=worker_id,
            count=count,
            sum_ms=sum_ms,
            max_ms=max_ms,
        )
        self._merge(key, point)

    def gauge(
        self,
        metric: str,
        value: float,
        *,
        provider: str = _NA,
        worker_id: str = _NA,
    ) -> None:
        """Buffer a sampled gauge (last write wins within a bucket)."""
        key = (_bucket_start(self._bucket_seconds), metric, provider, worker_id)
        point = OpsMetricPoint(
            bucket_start=key[0],
            metric=metric,
            provider=provider,
            worker_id=worker_id,
            value=value,
        )
        self._merge(key, point)

    def _merge(self, key: Any, point: OpsMetricPoint) -> None:
        existing = self._buffer.get(key)
        if existing is None:
            self._buffer[key] = point
        else:
            self._buffer[key] = existing.merge(point)

    # -- flush loop -------------------------------------------------------------

    async def run(self) -> None:
        """Background flush loop; runs until cancelled."""
        while True:
            await asyncio.sleep(self._flush_interval_seconds)
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("ops metrics flush failed")

    async def flush(self) -> int:
        """Coalesce the buffer into ``ops_metrics``; prune old buckets hourly.

        Best-effort: a persistence failure is logged and the buffered
        points are dropped — telemetry never breaks callers.
        """
        points = list(self._buffer.values())
        self._buffer.clear()
        try:
            if points:
                await self._repo.flush(points)
            await self._maybe_sweep_retention()
        except Exception:
            _logger.exception(
                "ops metrics flush failed dropped_points=%d", len(points)
            )
        return len(points)

    async def _maybe_sweep_retention(self) -> None:
        now = datetime.now(timezone.utc)
        if (
            self._last_retention_sweep is not None
            and (now - self._last_retention_sweep) < timedelta(hours=1)
        ):
            return
        self._last_retention_sweep = now
        deleted = await self._repo.delete_before(
            now - timedelta(days=self._retention_days)
        )
        if deleted:
            _logger.info("ops metrics retention sweep deleted_rows=%d", deleted)

    def buffered_points(self) -> int:
        """Test hook: number of coalesced points currently buffered."""
        return len(self._buffer)


def _bucket_start(bucket_seconds: int, now: datetime | None = None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    epoch = int(moment.timestamp())
    return datetime.fromtimestamp(
        (epoch // bucket_seconds) * bucket_seconds, tz=timezone.utc
    )
