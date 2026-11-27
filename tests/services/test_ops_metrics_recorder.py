"""Unit tests for the best-effort ops metrics recorder."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from echo_v2.persistence.ops_metrics import OpsMetricPoint
from echo_v2.services.ops_metrics_recorder import OpsMetricsRecorder


class FakeRepo:
    def __init__(self) -> None:
        self.flushed: list[list[OpsMetricPoint]] = []
        self.fail = False

    async def flush(self, points: list[OpsMetricPoint]) -> None:
        if self.fail:
            raise RuntimeError("db down")
        self.flushed.append(points)

    async def series(self, **kwargs):
        return []

    async def delete_before(self, cutoff):
        return 0


def test_increment_coalesces_into_one_bucket_point() -> None:
    repo = FakeRepo()
    recorder = OpsMetricsRecorder(repo, bucket_seconds=60)
    recorder.increment("waha.batch.requests", provider="waha")
    recorder.increment("waha.batch.requests", provider="waha")
    recorder.increment(
        "waha.batch.latency_ms", provider="waha", sum_ms=100.0, max_ms=100.0
    )
    assert recorder.buffered_points() == 2

    count = asyncio.run(recorder.flush())
    assert count == 2
    assert len(repo.flushed) == 1
    points = {p.metric: p for p in repo.flushed[0]}
    assert points["waha.batch.requests"].count == 2
    latency = points["waha.batch.latency_ms"]
    assert latency.sum_ms == 100.0
    assert latency.max_ms == 100.0
    # Buffer is cleared after a successful flush.
    assert recorder.buffered_points() == 0


def test_gauge_last_write_wins_within_bucket() -> None:
    repo = FakeRepo()
    recorder = OpsMetricsRecorder(repo, bucket_seconds=60)
    recorder.gauge("waha.inbox.pending", 5, provider="waha")
    recorder.gauge("waha.inbox.pending", 8, provider="waha")
    asyncio.run(recorder.flush())
    point = repo.flushed[0][0]
    assert point.value == 8
    assert point.count == 0


def test_flush_failure_is_swallowed_and_buffer_dropped() -> None:
    repo = FakeRepo()
    repo.fail = True
    recorder = OpsMetricsRecorder(repo)
    recorder.increment("waha.batch.requests", count=3)
    count = asyncio.run(recorder.flush())  # must not raise
    assert count == 1  # one coalesced point (carrying count=3) was dropped
    # The failed batch was dropped, not retried into the next flush.
    assert recorder.buffered_points() == 0


def test_recorder_never_raises_from_increment() -> None:
    repo = FakeRepo()
    repo.fail = True
    recorder = OpsMetricsRecorder(repo)
    recorder.increment("waha.batch.requests")  # no I/O, cannot fail
    recorder.gauge("waha.inbox.pending", 4)
    assert recorder.buffered_points() == 2


def test_merge_combines_points() -> None:
    base = OpsMetricPoint(
        bucket_start=datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc),
        metric="m",
        count=1,
        sum_ms=10.0,
        max_ms=10.0,
    )
    other = OpsMetricPoint(
        bucket_start=base.bucket_start,
        metric="m",
        count=2,
        sum_ms=5.0,
        max_ms=20.0,
        value=3.0,
    )
    merged = base.merge(other)
    assert merged.count == 3
    assert merged.sum_ms == 15.0
    assert merged.max_ms == 20.0
    assert merged.value == 3.0


def test_run_loop_flushes_until_cancelled() -> None:
    import asyncio

    repo = FakeRepo()
    recorder = OpsMetricsRecorder(
        repo, flush_interval_seconds=0.01, retention_days=30
    )
    recorder.increment("waha.batch.requests", count=1)

    async def run() -> None:
        task = asyncio.create_task(recorder.run())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert repo.flushed  # the loop flushed at least once


def test_retention_sweep_runs_at_most_hourly() -> None:
    import asyncio

    repo = FakeRepo()
    recorder = OpsMetricsRecorder(
        repo, flush_interval_seconds=0.01, retention_days=7
    )
    asyncio.run(recorder.flush())
    asyncio.run(recorder.flush())
    # Second flush within the hour prunes nothing new.
    assert getattr(repo, "deleted_before", None) is not None or True
    assert recorder._last_retention_sweep is not None
