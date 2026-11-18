"""CostAnalyticsService tier-usage tests — daily tokens vs shared quotas."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from echo_v2.domain.ai_usage_event import AiUsageEvent
from echo_v2.services.reporting.cost_analytics_service import (
    CostAnalyticsService,
)
from echo_v2.services.reporting.pricing_service import IncentivizedTierPool

_TIER_POOLS = {
    "mini": IncentivizedTierPool(
        daily_quota_tokens=10_000_000,
        models=("gpt-5.6-luna",),
    ),
    "large": IncentivizedTierPool(
        daily_quota_tokens=1_000_000,
        models=("gpt-5.4",),
    ),
}


class _FakeUsageRepo:
    def __init__(self, events: list[AiUsageEvent]) -> None:
        self._events = events

    async def record(self, event: AiUsageEvent) -> None: ...

    async def list_for_range(
        self,
        *,
        child_user_id: str | None = None,
        date_from: datetime,
        date_to: datetime,
        limit: int = 10_000,
    ) -> list[AiUsageEvent]:
        return [
            e
            for e in self._events
            if date_from <= e.created_at < date_to
            and (child_user_id is None or e.child_user_id == child_user_id)
        ]


def _event(
    *,
    model: str,
    prompt: int,
    completion: int = 0,
    child: str | None = "child-1",
    day: str = "2026-11-10",
    provider: str = "openai",
) -> AiUsageEvent:
    return AiUsageEvent(
        invocation_id=uuid4(),
        provider=provider,
        operation="guard_security",
        model=model,
        usage_type="tokens",
        cost_source="measured_usage_priced",
        status="success",
        child_user_id=child,
        prompt_tokens=prompt,
        completion_tokens=completion,
        estimated_cost_usd=Decimal("0.001"),
        created_at=datetime.fromisoformat(day + "T12:00:00+00:00"),
    )


def _service(events: list[AiUsageEvent]) -> CostAnalyticsService:
    return CostAnalyticsService(
        usage_repo=_FakeUsageRepo(events),
        tier_pools=_TIER_POOLS,
    )


async def test_tier_usage_aggregates_per_pool_per_day() -> None:
    service = _service([
        _event(model="gpt-5.6-luna", prompt=1_000_000),
        _event(model="gpt-5.4", prompt=200_000, child="child-2"),
    ])
    report = await service.tier_usage(
        date_from=datetime(2026, 11, 10, tzinfo=timezone.utc),
        date_to=datetime(2026, 11, 11, tzinfo=timezone.utc),
    )
    by_pool = {p.pool: p for p in report.pools}
    assert by_pool["mini"].days[0].tokens == 1_000_000
    assert by_pool["mini"].days[0].pct_of_quota == 10.0
    assert by_pool["large"].days[0].tokens == 200_000
    assert by_pool["large"].days[0].pct_of_quota == 20.0


async def test_tier_usage_break_even_users() -> None:
    """2 users, 100K tokens/day each → mini breaks even at 10M/100K=100."""
    service = _service([
        _event(model="gpt-5.6-luna", prompt=100_000, child="child-1"),
        _event(model="gpt-5.6-luna", prompt=100_000, child="child-2"),
    ])
    report = await service.tier_usage(
        date_from=datetime(2026, 11, 10, tzinfo=timezone.utc),
        date_to=datetime(2026, 11, 11, tzinfo=timezone.utc),
    )
    by_pool = {p.pool: p for p in report.pools}
    assert report.active_users == 2
    assert by_pool["mini"].per_user_daily_tokens == 100_000
    assert by_pool["mini"].break_even_users == 100
    # No large usage → no per-user average → no break-even projection.
    assert by_pool["large"].break_even_users is None


async def test_tier_usage_ignores_non_openai_and_zero_tokens() -> None:
    service = _service([
        _event(model="gemini-3.5-flash-lite", prompt=999_999, provider="google"),
        _event(model="gpt-5.6-luna", prompt=0, completion=0),
    ])
    report = await service.tier_usage(
        date_from=datetime(2026, 11, 10, tzinfo=timezone.utc),
        date_to=datetime(2026, 11, 11, tzinfo=timezone.utc),
    )
    assert report.active_users == 0
    assert all(p.avg_daily_tokens == 0 for p in report.pools)


async def test_tier_usage_reports_unmapped_models() -> None:
    service = _service([
        _event(model="gpt-9-future", prompt=5_000),
    ])
    report = await service.tier_usage(
        date_from=datetime(2026, 11, 10, tzinfo=timezone.utc),
        date_to=datetime(2026, 11, 11, tzinfo=timezone.utc),
    )
    assert report.unmapped_models == {"gpt-9-future": 5_000}


async def test_tier_usage_fills_missing_days_with_zero() -> None:
    """A day with only large-pool events still shows 0 for the mini pool."""
    service = _service([
        _event(model="gpt-5.4", prompt=10_000, day="2026-11-10"),
        _event(model="gpt-5.6-luna", prompt=10_000, day="2026-11-11"),
    ])
    report = await service.tier_usage(
        date_from=datetime(2026, 11, 10, tzinfo=timezone.utc),
        date_to=datetime(2026, 11, 12, tzinfo=timezone.utc),
    )
    by_pool = {p.pool: p for p in report.pools}
    assert [d.tokens for d in by_pool["mini"].days] == [0, 10_000]
    assert [d.tokens for d in by_pool["large"].days] == [10_000, 0]


@pytest.mark.parametrize(
    ("prompt", "completion", "expected_tokens"),
    [(100, 50, 150), (0, 10, 10), (10, 0, 10)],
)
async def test_tier_usage_counts_prompt_plus_completion(
    prompt: int, completion: int, expected_tokens: int
) -> None:
    service = _service([
        _event(model="gpt-5.6-luna", prompt=prompt, completion=completion),
    ])
    report = await service.tier_usage(
        date_from=datetime(2026, 11, 10, tzinfo=timezone.utc),
        date_to=datetime(2026, 11, 11, tzinfo=timezone.utc),
    )
    by_pool = {p.pool: p for p in report.pools}
    assert by_pool["mini"].days[0].tokens == expected_tokens
