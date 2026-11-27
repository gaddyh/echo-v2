"""Persistence port for the Echo Operations Center metrics table.

``ops_metrics`` holds time-bucketed, low-cardinality aggregate counters and
sampled gauges (one row per bucket per metric per provider/worker — never
per request or per message). ``flush`` upsert-increments a batch of points
atomically so concurrent writers to the same bucket are merged, not lost.

Semantics:
* ``count`` / ``sum_ms`` / ``max_ms`` — cumulative within the bucket
  (counter-style; the recorder flushes deltas).
* ``value`` — a sampled gauge; last write wins within a bucket.

Writes are best-effort: the recorder logs and swallows failures so
telemetry can never fail the ingestion critical path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

__all__ = ["OpsMetricPoint", "OpsMetricsRepository"]


@dataclass(frozen=True)
class OpsMetricPoint:
    """One bucketed datapoint to merge into ``ops_metrics``."""

    bucket_start: datetime
    metric: str
    provider: str = ""
    worker_id: str = ""
    count: int = 0
    value: float | None = None
    sum_ms: float | None = None
    max_ms: float | None = None

    def merge(self, other: OpsMetricPoint) -> OpsMetricPoint:
        """Combine two points for the same bucket key (for in-memory coalescing)."""
        sum_ms = None
        if self.sum_ms is not None or other.sum_ms is not None:
            sum_ms = (self.sum_ms or 0.0) + (other.sum_ms or 0.0)
        max_values = [m for m in (self.max_ms, other.max_ms) if m is not None]
        max_ms = max(max_values) if max_values else None
        value = other.value if other.value is not None else self.value
        return OpsMetricPoint(
            bucket_start=self.bucket_start,
            metric=self.metric,
            provider=self.provider,
            worker_id=self.worker_id,
            count=self.count + other.count,
            value=value,
            sum_ms=sum_ms,
            max_ms=max_ms,
        )


@runtime_checkable
class OpsMetricsRepository(Protocol):
    async def flush(self, points: list[OpsMetricPoint]) -> None:
        """Merge ``points`` into their buckets (upsert-increment).

        ``value`` uses last-write-wins within an UPDATE; ``count``/``sum_ms``
        are incremented and ``max_ms`` is raised. Idempotent at the bucket
        level only in aggregate (the recorder coalesces before flushing).
        """
        ...

    async def series(
        self,
        *,
        metric: str,
        since: datetime,
        provider: str = "",
        worker_id: str = "",
    ) -> list[OpsMetricPoint]:
        """Return points for one metric ordered by bucket, oldest first."""
        ...

    async def delete_before(self, cutoff: datetime) -> int:
        """Retention: delete buckets strictly older than ``cutoff``."""
        ...
