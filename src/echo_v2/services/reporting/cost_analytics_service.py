"""Cost analytics service — queries the AI usage ledger for admin dashboards.

Reads from ``ai_usage_events`` (PR 1) to surface cost breakdowns by model,
operation, and child. Does not trigger any LLM analysis — all data comes
from already-recorded usage events.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from echo_v2.domain.ai_usage_event import AiUsageEvent
from echo_v2.persistence.ai_usage_events import AiUsageEventRepository

__all__ = [
    "CostAnalyticsService",
    "CostBreakdown",
    "CostGroup",
    "CostOverview",
]


@dataclass(frozen=True)
class CostOverview:
    """Aggregated cost totals for the selected period."""

    total_cost_usd: Decimal
    total_events: int
    total_prompt_tokens: int
    total_completion_tokens: int
    measured_events: int
    fallback_events: int
    unknown_events: int
    error_events: int
    instrumentation_coverage_pct: float


@dataclass(frozen=True)
class CostGroup:
    """One row in a cost breakdown (by model, operation, or child)."""

    key: str
    events: int
    cost_usd: Decimal
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class CostBreakdown:
    """Full cost breakdown response."""

    overview: CostOverview
    by_model: list[CostGroup]
    by_operation: list[CostGroup]
    by_child: list[CostGroup]


class CostAnalyticsService:
    """Query AI usage events for cost analytics. No LLM calls."""

    def __init__(
        self,
        *,
        usage_repo: AiUsageEventRepository,
    ) -> None:
        self._usage_repo = usage_repo

    async def costs(
        self,
        *,
        date_from: datetime,
        date_to: datetime,
        child_user_id: str | None = None,
    ) -> CostBreakdown:
        events = await self._usage_repo.list_for_range(
            child_user_id=child_user_id,
            date_from=date_from,
            date_to=date_to,
            limit=100_000,
        )
        return self._aggregate(events)

    def _aggregate(self, events: list[AiUsageEvent]) -> CostBreakdown:
        total_cost = Decimal(0)
        total_prompt = 0
        total_completion = 0
        measured = 0
        fallback = 0
        unknown = 0
        errors = 0

        by_model: dict[str, list[AiUsageEvent]] = defaultdict(list)
        by_operation: dict[str, list[AiUsageEvent]] = defaultdict(list)
        by_child: dict[str, list[AiUsageEvent]] = defaultdict(list)

        for event in events:
            cost = event.estimated_cost_usd or Decimal(0)
            total_cost += cost
            total_prompt += event.prompt_tokens or 0
            total_completion += event.completion_tokens or 0
            if event.cost_source == "measured_usage_priced":
                measured += 1
            elif event.cost_source == "fallback_estimate":
                fallback += 1
            elif event.cost_source == "unknown":
                unknown += 1
            if event.status == "error":
                errors += 1

            by_model[event.model].append(event)
            by_operation[event.operation].append(event)
            child_key = event.child_user_id or "(unattributed)"
            by_child[child_key].append(event)

        total = len(events)
        coverage = (total - unknown) / total * 100.0 if total else 0.0

        return CostBreakdown(
            overview=CostOverview(
                total_cost_usd=total_cost,
                total_events=total,
                total_prompt_tokens=total_prompt,
                total_completion_tokens=total_completion,
                measured_events=measured,
                fallback_events=fallback,
                unknown_events=unknown,
                error_events=errors,
                instrumentation_coverage_pct=coverage,
            ),
            by_model=self._group(by_model),
            by_operation=self._group(by_operation),
            by_child=self._group(by_child),
        )

    def _group(
        self, groups: dict[str, list[AiUsageEvent]]
    ) -> list[CostGroup]:
        result: list[CostGroup] = []
        for key, events in groups.items():
            cost = sum((e.estimated_cost_usd or Decimal(0)) for e in events) or Decimal(0)
            prompt = sum(e.prompt_tokens or 0 for e in events)
            completion = sum(e.completion_tokens or 0 for e in events)
            result.append(
                CostGroup(
                    key=key,
                    events=len(events),
                    cost_usd=cost,
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                )
            )
        result.sort(key=lambda g: g.cost_usd, reverse=True)
        return result
