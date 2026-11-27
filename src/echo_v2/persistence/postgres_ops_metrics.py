"""PostgreSQL repository for the ``ops_metrics`` aggregate table.

``flush`` performs ``INSERT ... ON CONFLICT DO UPDATE`` statements in a
single transaction. The UPDATE arm increments ``count``/``sum_ms``, raises
``max_ms``, and overwrites the gauge ``value`` only when the incoming
point carries one — so concurrent recorders merge into the same bucket.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.persistence.ops_metrics import OpsMetricPoint
from echo_v2.persistence.orm import OpsMetricRow

__all__ = ["PostgresOpsMetricsRepository"]


class PostgresOpsMetricsRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def flush(self, points: list[OpsMetricPoint]) -> None:
        if not points:
            return
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            for point in points:
                set_: dict[str, object] = {
                    "count": OpsMetricRow.count + point.count,
                    "updated_at": now,
                }
                if point.sum_ms is not None:
                    set_["sum_ms"] = func.coalesce(OpsMetricRow.sum_ms, 0) + point.sum_ms
                if point.max_ms is not None:
                    set_["max_ms"] = func.greatest(
                        func.coalesce(OpsMetricRow.max_ms, 0), point.max_ms
                    )
                if point.value is not None:
                    set_["value"] = literal(point.value)
                await session.execute(
                    pg_insert(OpsMetricRow)
                    .values(
                        bucket_start=point.bucket_start,
                        metric=point.metric,
                        provider=point.provider,
                        worker_id=point.worker_id,
                        value=point.value or 0.0,
                        count=point.count,
                        sum_ms=point.sum_ms,
                        max_ms=point.max_ms,
                    )
                    .on_conflict_do_update(
                        index_elements=[
                            "bucket_start",
                            "metric",
                            "provider",
                            "worker_id",
                        ],
                        set_=set_,
                    )
                )
            await session.commit()

    async def series(
        self,
        *,
        metric: str,
        since: datetime,
        provider: str = "",
        worker_id: str = "",
    ) -> list[OpsMetricPoint]:
        async with self._session_factory() as session:
            rows = await session.execute(
                select(
                    OpsMetricRow.bucket_start.label("bucket_start"),
                    OpsMetricRow.count.label("count"),
                    OpsMetricRow.value.label("value"),
                    OpsMetricRow.sum_ms.label("sum_ms"),
                    OpsMetricRow.max_ms.label("max_ms"),
                )
                .where(
                    OpsMetricRow.metric == metric,
                    OpsMetricRow.bucket_start >= since,
                    OpsMetricRow.provider == provider,
                    OpsMetricRow.worker_id == worker_id,
                )
                .order_by(OpsMetricRow.bucket_start.asc())
            )
            return [
                OpsMetricPoint(
                    bucket_start=row.bucket_start,
                    metric=metric,
                    provider=provider,
                    worker_id=worker_id,
                    count=int(row._mapping["count"]),
                    value=row.value,
                    sum_ms=row.sum_ms,
                    max_ms=row.max_ms,
                )
                for row in rows
            ]

    async def delete_before(self, cutoff: datetime) -> int:
        from typing import cast

        from sqlalchemy import CursorResult

        async with self._session_factory() as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    delete(OpsMetricRow).where(OpsMetricRow.bucket_start < cutoff)
                ),
            )
            await session.commit()
            return result.rowcount or 0
