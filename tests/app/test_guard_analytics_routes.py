from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from echo_v2.app.guard_analytics_routes import build_guard_analytics_router
from echo_v2.domain.ai_usage_event import AiUsageEvent
from echo_v2.domain.guard_daily_stats import GuardDailyStats
from echo_v2.persistence.waiting_list_tokens import InMemoryWaitingListSessionRepository
from echo_v2.services.reporting.cost_analytics_service import CostAnalyticsService
from echo_v2.services.reporting.guard_reporting_service import (
    AnalyticsOverview,
    ChildSummary,
    DailySeriesPoint,
    GuardReportingService,
)
from echo_v2.services.waiting_list_token_service import WaitingListTokenService


class FakeDailyStatsRepo:
    def __init__(self, rows: list[GuardDailyStats] | None = None) -> None:
        self._rows = rows or []
        self.upserted: list[GuardDailyStats] = []

    async def upsert(self, stats: GuardDailyStats) -> None:
        self.upserted.append(stats)
        self._rows.append(stats)

    async def get(self, child_user_id: str, local_date: date) -> GuardDailyStats | None:
        for row in self._rows:
            if row.child_user_id == child_user_id and row.local_date == local_date:
                return row
        return None

    async def list_for_range(
        self,
        *,
        child_user_id: str | None = None,
        date_from: date,
        date_to: date,
    ) -> list[GuardDailyStats]:
        return [
            row
            for row in self._rows
            if date_from <= row.local_date <= date_to
            and (child_user_id is None or row.child_user_id == child_user_id)
        ]


class FakeUsageRepo:
    def __init__(self, events: list[AiUsageEvent] | None = None) -> None:
        self._events = events or []

    async def record(self, event: AiUsageEvent) -> None:
        self._events.append(event)

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
        ][:limit]


class FakeReportingService:
    def __init__(self, rows: list[GuardDailyStats] | None = None) -> None:
        self._rows = rows or []

    async def overview(
        self,
        *,
        date_from: date,
        date_to: date,
        child_user_id: str | None = None,
    ) -> AnalyticsOverview:
        rows = [r for r in self._rows if date_from <= r.local_date <= date_to]
        children = {r.child_user_id for r in rows}
        return AnalyticsOverview(
            children_monitored=len(children),
            total_messages=sum(r.messages_received for r in rows),
            total_assessments=sum(r.guard_assessments for r in rows),
            total_security_detections=sum(r.security_detections for r in rows),
            total_exposure_detections=sum(r.exposure_detections for r in rows),
            total_security_alerts_sent=sum(r.security_alerts_sent for r in rows),
            total_exposure_alerts_sent=sum(r.exposure_alerts_sent for r in rows),
            protected_days=sum(1 for r in rows if r.coverage_status == "protected"),
            attention_days=sum(1 for r in rows if r.coverage_status == "attention"),
            incomplete_days=sum(1 for r in rows if r.coverage_status == "incomplete"),
            coverage_health_pct=100.0 if rows else 0.0,
        )

    async def children_table(
        self,
        *,
        date_from: date,
        date_to: date,
        child_user_id: str | None = None,
    ) -> list[ChildSummary]:
        rows = [r for r in self._rows if date_from <= r.local_date <= date_to]
        return [
            ChildSummary(
                child_user_id=r.child_user_id,
                child_name=None,
                local_date=r.local_date,
                coverage_status=r.coverage_status,
                coverage_health=r.coverage_health,
                messages_received=r.messages_received,
                guard_assessments=r.guard_assessments,
                security_detections=r.security_detections,
                exposure_detections=r.exposure_detections,
                security_alerts_sent=r.security_alerts_sent,
                exposure_alerts_sent=r.exposure_alerts_sent,
                connection_seconds=r.connection_seconds,
            )
            for r in rows
        ]

    async def daily_series(
        self,
        *,
        date_from: date,
        date_to: date,
        child_user_id: str | None = None,
    ) -> list[DailySeriesPoint]:
        rows = [r for r in self._rows if date_from <= r.local_date <= date_to]
        by_date: dict[date, DailySeriesPoint] = {}
        for r in rows:
            d = r.local_date
            if d not in by_date:
                by_date[d] = DailySeriesPoint(
                    local_date=d,
                    messages_received=0,
                    guard_assessments=0,
                    security_detections=0,
                    exposure_detections=0,
                    security_alerts_sent=0,
                    exposure_alerts_sent=0,
                )
            p = by_date[d]
            by_date[d] = DailySeriesPoint(
                local_date=d,
                messages_received=p.messages_received + r.messages_received,
                guard_assessments=p.guard_assessments + r.guard_assessments,
                security_detections=p.security_detections + r.security_detections,
                exposure_detections=p.exposure_detections + r.exposure_detections,
                security_alerts_sent=p.security_alerts_sent + r.security_alerts_sent,
                exposure_alerts_sent=p.exposure_alerts_sent + r.exposure_alerts_sent,
            )
        return [by_date[d] for d in sorted(by_date)]

    async def rebuild_range(
        self,
        *,
        child_user_id: str,
        date_from: date,
        date_to: date,
        timezone_name: str | None = None,
    ) -> list[GuardDailyStats]:
        return []


def _stats(
    child: str = "child-1",
    d: date = date(2026, 10, 25),
    msgs: int = 10,
    status: str = "protected",
) -> GuardDailyStats:
    return GuardDailyStats(
        child_user_id=child,
        local_date=d,
        timezone="Asia/Jerusalem",
        messages_received=msgs,
        guard_assessments=5,
        security_detections=1 if status == "attention" else 0,
        coverage_status=status,
    )


def _usage_event(
    operation: str = "guard_security",
    model: str = "gpt-5.6-luna",
    cost: str = "0.01",
    child: str | None = "child-1",
    idx: int = 0,
) -> AiUsageEvent:
    return AiUsageEvent(
        invocation_id=UUID(int=idx),
        provider="openai",
        operation=operation,
        model=model,
        usage_type="tokens",
        cost_source="measured_usage_priced",
        status="success",
        child_user_id=child,
        prompt_tokens=100,
        completion_tokens=20,
        estimated_cost_usd=Decimal(cost),
        created_at=datetime(2026, 10, 25, 12, tzinfo=timezone.utc),
    )


def _client(
    reporting: FakeReportingService,
    cost: CostAnalyticsService,
    tokens: WaitingListTokenService,
    admin_user_ids: set[str],
) -> AsyncClient:
    app = FastAPI()
    app.include_router(
        build_guard_analytics_router(
            reporting_service=reporting,
            cost_service=cost,
            token_service=tokens,
            admin_user_ids=admin_user_ids,
        )
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://test")


async def test_analytics_page_serves_html() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    reporting = FakeReportingService()
    cost = CostAnalyticsService(usage_repo=FakeUsageRepo())
    async with _client(reporting, cost, tokens, set()) as client:
        r = await client.get("/api/admin/guard/analytics")
        assert r.status_code == 200
        assert "Guard Analytics" in r.text


async def test_overview_requires_admin_cookie() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    reporting = FakeReportingService()
    cost = CostAnalyticsService(usage_repo=FakeUsageRepo())
    async with _client(reporting, cost, tokens, {"admin"}) as client:
        r = await client.get("/api/admin/guard/analytics/overview?period=7d")
        assert r.status_code == 401


async def test_overview_returns_aggregated_data() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    session_id, _ = await tokens.issue(user_id="admin")
    reporting = FakeReportingService(rows=[
        _stats(child="child-1", d=date(2026, 10, 24), msgs=10),
        _stats(child="child-1", d=date(2026, 10, 25), msgs=20, status="attention"),
    ])
    cost = CostAnalyticsService(usage_repo=FakeUsageRepo())
    async with _client(reporting, cost, tokens, {"admin"}) as client:
        r = await client.get(
            "/api/admin/guard/analytics/overview?period=7d",
            cookies={"wls": session_id},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["children_monitored"] == 1
        assert data["total_messages"] == 30
        assert data["protected_days"] == 1
        assert data["attention_days"] == 1


async def test_children_table_returns_rows() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    session_id, _ = await tokens.issue(user_id="admin")
    reporting = FakeReportingService(rows=[
        _stats(child="child-1", d=date(2026, 10, 25), msgs=15),
    ])
    cost = CostAnalyticsService(usage_repo=FakeUsageRepo())
    async with _client(reporting, cost, tokens, {"admin"}) as client:
        r = await client.get(
            "/api/admin/guard/analytics/children?period=7d",
            cookies={"wls": session_id},
        )
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["messages_received"] == 15
        assert rows[0]["child_name"] is None


async def test_daily_series_aggregates_by_date() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    session_id, _ = await tokens.issue(user_id="admin")
    reporting = FakeReportingService(rows=[
        _stats(child="child-1", d=date(2026, 10, 24), msgs=10),
        _stats(child="child-2", d=date(2026, 10, 24), msgs=5),
        _stats(child="child-1", d=date(2026, 10, 25), msgs=20),
    ])
    cost = CostAnalyticsService(usage_repo=FakeUsageRepo())
    async with _client(reporting, cost, tokens, {"admin"}) as client:
        r = await client.get(
            "/api/admin/guard/analytics/daily?period=7d",
            cookies={"wls": session_id},
        )
        assert r.status_code == 200
        series = r.json()
        assert len(series) == 2
        assert series[0]["messages_received"] == 15  # 10+5
        assert series[1]["messages_received"] == 20


async def test_costs_breakdown_by_model() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    session_id, _ = await tokens.issue(user_id="admin")
    events = [
        _usage_event(operation="guard_security", model="gpt-5.6-luna", cost="0.02", idx=1),
        _usage_event(operation="guard_exposure", model="gpt-5.6-luna", cost="0.01", idx=2),
        _usage_event(operation="media_image", model="gpt-4o", cost="0.005", idx=3),
    ]
    cost = CostAnalyticsService(usage_repo=FakeUsageRepo(events=events))
    reporting = FakeReportingService()
    async with _client(reporting, cost, tokens, {"admin"}) as client:
        r = await client.get(
            "/api/admin/guard/analytics/costs?period=7d&group_by=model",
            cookies={"wls": session_id},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["overview"]["total_events"] == 3
        assert float(data["overview"]["total_cost_usd"]) == 0.035
        groups = data["groups"]
        assert len(groups) == 2
        assert groups[0]["key"] == "gpt-5.6-luna"
        assert float(groups[0]["cost_usd"]) == 0.030


async def test_costs_breakdown_by_operation() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    session_id, _ = await tokens.issue(user_id="admin")
    events = [
        _usage_event(operation="guard_security", cost="0.02", idx=1),
        _usage_event(operation="guard_exposure", cost="0.01", idx=2),
    ]
    cost = CostAnalyticsService(usage_repo=FakeUsageRepo(events=events))
    reporting = FakeReportingService()
    async with _client(reporting, cost, tokens, {"admin"}) as client:
        r = await client.get(
            "/api/admin/guard/analytics/costs?period=7d&group_by=operation",
            cookies={"wls": session_id},
        )
        assert r.status_code == 200
        groups = r.json()["groups"]
        assert len(groups) == 2
        assert groups[0]["key"] == "guard_security"


async def test_rebuild_requires_dates_and_child() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    session_id, _ = await tokens.issue(user_id="admin")
    reporting = FakeReportingService()
    cost = CostAnalyticsService(usage_repo=FakeUsageRepo())
    async with _client(reporting, cost, tokens, {"admin"}) as client:
        r = await client.post(
            "/api/admin/guard/analytics/rebuild",
            cookies={"wls": session_id},
        )
        assert r.status_code == 400


async def test_non_admin_user_rejected() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    session_id, _ = await tokens.issue(user_id="regular-user")
    reporting = FakeReportingService()
    cost = CostAnalyticsService(usage_repo=FakeUsageRepo())
    async with _client(reporting, cost, tokens, {"admin"}) as client:
        r = await client.get(
            "/api/admin/guard/analytics/overview?period=7d",
            cookies={"wls": session_id},
        )
        assert r.status_code == 401


async def test_cost_service_aggregates_events() -> None:
    """Direct unit test for CostAnalyticsService."""
    events = [
        _usage_event(operation="guard_security", model="gpt-5.6-luna", cost="0.02", idx=1),
        _usage_event(operation="media_audio", model="whisper", cost="0.01", child=None, idx=2),
    ]
    service = CostAnalyticsService(usage_repo=FakeUsageRepo(events=events))
    result = await service.costs(
        date_from=datetime(2026, 10, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 10, 31, tzinfo=timezone.utc),
    )
    assert result.overview.total_events == 2
    assert result.overview.total_cost_usd == Decimal("0.03")
    assert len(result.by_model) == 2
    assert len(result.by_operation) == 2
    assert len(result.by_child) == 2  # child-1 + (unattributed)


async def test_cost_service_handles_fallback_unknown_and_error_events() -> None:
    """Cover fallback_estimate, unknown cost_source, and error status paths."""
    events = [
        AiUsageEvent(
            invocation_id=UUID(int=1),
            provider="modal",
            operation="media_audio",
            model="whisper",
            usage_type="audio_seconds",
            cost_source="fallback_estimate",
            status="success",
            child_user_id="child-1",
            estimated_cost_usd=Decimal("0.005"),
            created_at=datetime(2026, 10, 25, 12, tzinfo=timezone.utc),
        ),
        AiUsageEvent(
            invocation_id=UUID(int=2),
            provider="openai",
            operation="guard_security",
            model="gpt-5.6-luna",
            usage_type="tokens",
            cost_source="unknown",
            status="error",
            child_user_id="child-1",
            error_type="TimeoutError",
            created_at=datetime(2026, 10, 25, 12, tzinfo=timezone.utc),
        ),
    ]
    service = CostAnalyticsService(usage_repo=FakeUsageRepo(events=events))
    result = await service.costs(
        date_from=datetime(2026, 10, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 10, 31, tzinfo=timezone.utc),
    )
    assert result.overview.total_events == 2
    assert result.overview.fallback_events == 1
    assert result.overview.unknown_events == 1
    assert result.overview.error_events == 1
    assert result.overview.measured_events == 0
    # Coverage = (total - unknown) / total = 50%
    assert result.overview.instrumentation_coverage_pct == 50.0


async def test_cost_service_empty_events() -> None:
    """Empty events list should produce zero totals and 100% coverage."""
    service = CostAnalyticsService(usage_repo=FakeUsageRepo(events=[]))
    result = await service.costs(
        date_from=datetime(2026, 10, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 10, 31, tzinfo=timezone.utc),
    )
    assert result.overview.total_events == 0
    assert result.overview.total_cost_usd == Decimal(0)
    assert result.overview.instrumentation_coverage_pct == 0.0
    assert result.by_model == []


async def test_reporting_service_children_table_from_repo() -> None:
    """Direct unit test for GuardReportingService.children_table."""
    repo = FakeDailyStatsRepo(rows=[
        _stats(child="child-1", d=date(2026, 10, 24), msgs=10),
        _stats(child="child-2", d=date(2026, 10, 25), msgs=20, status="attention"),
    ])

    class NullSessionFactory:
        async def __call__(self) -> None:
            pass

    from echo_v2.services.reporting.guard_reporting_service import GuardReportingService
    service = GuardReportingService(
        session_factory=NullSessionFactory(),  # type: ignore[arg-type]
        daily_stats_repo=repo,  # type: ignore[arg-type]
    )
    result = await service.children_table(
        date_from=date(2026, 10, 24),
        date_to=date(2026, 10, 25),
    )
    assert len(result) == 2
    # Sorted by date desc — child-2 (Oct 25) first.
    assert result[0].child_user_id == "child-2"
    assert result[0].coverage_status == "attention"
    assert result[1].child_user_id == "child-1"
    assert result[1].messages_received == 10


async def test_reporting_service_daily_series_from_repo() -> None:
    """Direct unit test for GuardReportingService.daily_series."""
    repo = FakeDailyStatsRepo(rows=[
        _stats(child="child-1", d=date(2026, 10, 24), msgs=10),
        _stats(child="child-2", d=date(2026, 10, 24), msgs=5),
        _stats(child="child-1", d=date(2026, 10, 25), msgs=20),
    ])

    class NullSessionFactory:
        async def __call__(self) -> None:
            pass

    from echo_v2.services.reporting.guard_reporting_service import GuardReportingService
    service = GuardReportingService(
        session_factory=NullSessionFactory(),  # type: ignore[arg-type]
        daily_stats_repo=repo,  # type: ignore[arg-type]
    )
    result = await service.daily_series(
        date_from=date(2026, 10, 24),
        date_to=date(2026, 10, 25),
    )
    assert len(result) == 2
    assert result[0].local_date == date(2026, 10, 24)
    assert result[0].messages_received == 15  # 10 + 5
    assert result[1].local_date == date(2026, 10, 25)
    assert result[1].messages_received == 20


async def test_reporting_service_overview_empty() -> None:
    """Overview with no data should return zeros."""
    repo = FakeDailyStatsRepo(rows=[])

    class NullSessionFactory:
        async def __call__(self) -> None:
            pass

    from echo_v2.services.reporting.guard_reporting_service import GuardReportingService
    service = GuardReportingService(
        session_factory=NullSessionFactory(),  # type: ignore[arg-type]
        daily_stats_repo=repo,  # type: ignore[arg-type]
    )
    result = await service.overview(
        date_from=date(2026, 10, 24),
        date_to=date(2026, 10, 25),
    )
    assert result.children_monitored == 0
    assert result.total_messages == 0
    assert result.coverage_health_pct == 0.0


async def test_reporting_service_overview_from_repo() -> None:
    """Direct unit test for GuardReportingService.overview using fake repo."""
    repo = FakeDailyStatsRepo(rows=[
        _stats(child="child-1", d=date(2026, 10, 24), msgs=10),
        _stats(child="child-1", d=date(2026, 10, 25), msgs=20, status="attention"),
    ])

    class NullSessionFactory:
        async def __call__(self) -> None:
            pass

    service = GuardReportingService(
        session_factory=NullSessionFactory(),  # type: ignore[arg-type]
        daily_stats_repo=repo,  # type: ignore[arg-type]
    )
    result = await service.overview(
        date_from=date(2026, 10, 24),
        date_to=date(2026, 10, 25),
    )
    assert result.children_monitored == 1
    assert result.total_messages == 30
    assert result.attention_days == 1
