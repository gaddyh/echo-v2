"""PostgreSQL repository for billable AI usage events.

Records one row per actual provider call. ``invocation_id`` is the
idempotency key: ``INSERT ... ON CONFLICT (invocation_id) DO NOTHING`` so
a recorder persistence retry never double-counts, while a genuinely
separate billable attempt (provider retry, reanalysis, duplicate
analysis) gets a fresh ``invocation_id`` and its own row.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from echo_v2.domain.ai_usage_event import AiUsageEvent
from echo_v2.persistence.orm import AiUsageEventRow


class PostgresAiUsageEventRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, event: AiUsageEvent) -> None:
        async with self._session_factory() as session:
            await session.execute(
                pg_insert(AiUsageEventRow)
                .values(
                    invocation_id=str(event.invocation_id),
                    logical_ref=event.logical_ref,
                    child_user_id=event.child_user_id,
                    provider=event.provider,
                    operation=event.operation,
                    model=event.model,
                    usage_type=event.usage_type,
                    prompt_tokens=event.prompt_tokens,
                    completion_tokens=event.completion_tokens,
                    cached_tokens=event.cached_tokens,
                    total_tokens=event.total_tokens,
                    usage_details=event.usage_details,
                    estimated_cost_usd=(
                        event.estimated_cost_usd
                        if event.estimated_cost_usd is not None
                        else None
                    ),
                    cost_source=event.cost_source,
                    price_version=event.price_version,
                    status=event.status,
                    provider_request_id=event.provider_request_id,
                    pipeline_event_id=event.pipeline_event_id,
                    error_type=event.error_type,
                    created_at=event.created_at,
                )
                .on_conflict_do_nothing(index_elements=["invocation_id"])
            )
            await session.commit()

    async def list_for_range(
        self,
        *,
        child_user_id: str | None = None,
        date_from: datetime,
        date_to: datetime,
        limit: int = 10_000,
    ) -> list[AiUsageEvent]:
        async with self._session_factory() as session:
            stmt = select(AiUsageEventRow).where(
                AiUsageEventRow.created_at >= date_from,
                AiUsageEventRow.created_at < date_to,
            )
            if child_user_id is not None:
                stmt = stmt.where(AiUsageEventRow.child_user_id == child_user_id)
            stmt = stmt.order_by(AiUsageEventRow.created_at.asc()).limit(limit)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [_to_domain(row) for row in rows]


def _to_domain(row: AiUsageEventRow) -> AiUsageEvent:
    from uuid import UUID

    return AiUsageEvent(
        invocation_id=UUID(str(row.invocation_id)),
        provider=row.provider,
        operation=row.operation,
        model=row.model,
        usage_type=row.usage_type,
        cost_source=row.cost_source,
        status=row.status,
        logical_ref=row.logical_ref,
        child_user_id=str(row.child_user_id) if row.child_user_id is not None else None,
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        cached_tokens=row.cached_tokens,
        total_tokens=row.total_tokens,
        usage_details=dict(row.usage_details or {}),
        estimated_cost_usd=(
            Decimal(row.estimated_cost_usd) if row.estimated_cost_usd is not None else None
        ),
        price_version=row.price_version,
        provider_request_id=row.provider_request_id,
        pipeline_event_id=(
            str(row.pipeline_event_id) if row.pipeline_event_id is not None else None
        ),
        error_type=row.error_type,
        created_at=row.created_at,
    )


__all__ = ["PostgresAiUsageEventRepository"]
