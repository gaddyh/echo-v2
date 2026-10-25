from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from echo_v2.app.guard_health_routes import build_guard_health_router
from echo_v2.persistence.waiting_list_tokens import InMemoryWaitingListSessionRepository
from echo_v2.services.guard_health_service import HealthSnapshot
from echo_v2.services.waiting_list_token_service import WaitingListTokenService


def _snapshot() -> HealthSnapshot:
    now = datetime.now(timezone.utc)
    return HealthSnapshot(
        generated_at=now,
        connections=[
            {
                "user_id": "child-1",
                "status": "connected",
                "provider": "baileys",
                "updated_at": now,
                "state_version": 42,
            }
        ],
        connection_summary={"connected": 1},
        messages_last_1h=56,
        messages_last_24h=968,
        latest_message_at=now,
        security_assessments_last_1h=16,
        security_assessments_last_24h=310,
        exposure_assessments_last_1h=4,
        exposure_assessments_last_24h=89,
        psk_none_last_24h=103,
        psk_true_last_24h=1,
        enrichment_status_24h={"completed": 98, "terminal": 38, "failed": 2},
        terminal_errors_24h=[
            {"error": "media download URL is unavailable", "count": 35},
        ],
        stuck_enrichments=[
            {
                "id": "cf581a38-aa35-4237-8132-62cc19a93254",
                "message_id": "8a4dd8bc-7286-405a-b334-5240a528090e",
                "created_at": datetime(2026, 10, 17, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 10, 17, tzinfo=timezone.utc),
                "lease_expires_at": None,
            }
        ],
        failed_enrichments_24h=2,
        pipeline_events_1h={"media.enrichment.completed": 4, "media.enrichment.failed": 4},
        ai_usage_events_1h=34,
        ai_usage_cost_1h=0.0791,
        ai_usage_events_24h=36,
        ai_usage_cost_24h=0.0826,
        guard_alerts_24h=0,
        delivery_outcomes_24h={"sent": 12, "not_sent": 690},
        daily_stats_rows=45,
        daily_stats_last_updated=now,
        connector_events_24h={"processed": 1050},
    )


class FakeHealthService:
    def __init__(self, snap: HealthSnapshot | None = None) -> None:
        self._snap = snap or _snapshot()
        self.calls = 0

    async def snapshot(self) -> HealthSnapshot:
        self.calls += 1
        return self._snap


def _client(
    service: FakeHealthService,
    tokens: WaitingListTokenService,
    admin_user_ids: set[str],
) -> AsyncClient:
    app = FastAPI()
    app.include_router(
        build_guard_health_router(
            health_service=service,  # type: ignore[arg-type]
            token_service=tokens,
            admin_user_ids=admin_user_ids,
        )
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://test")


async def test_health_page_serves_html() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    service = FakeHealthService()
    async with _client(service, tokens, set()) as client:
        r = await client.get("/api/admin/guard/health")
        assert r.status_code == 200
        assert "Guard Health" in r.text


async def test_snapshot_requires_admin_cookie() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    service = FakeHealthService()
    async with _client(service, tokens, {"admin"}) as client:
        r = await client.get("/api/admin/guard/health/snapshot")
        assert r.status_code == 401


async def test_snapshot_returns_health_data() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    session_id, _ = await tokens.issue(user_id="admin")
    service = FakeHealthService()
    async with _client(service, tokens, {"admin"}) as client:
        r = await client.get(
            "/api/admin/guard/health/snapshot",
            cookies={"wls": session_id},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["connection_summary"]["connected"] == 1
        assert data["messages_last_1h"] == 56
        assert data["security_assessments_last_24h"] == 310
        assert data["psk_none_last_24h"] == 103
        assert data["ai_usage_cost_1h"] == 0.0791
        assert data["stuck_enrichments"][0]["id"].startswith("cf581a38")
        assert data["terminal_errors_24h"][0]["count"] == 35
        assert data["daily_stats_rows"] == 45


async def test_non_admin_user_rejected() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    session_id, _ = await tokens.issue(user_id="regular-user")
    service = FakeHealthService()
    async with _client(service, tokens, {"admin"}) as client:
        r = await client.get(
            "/api/admin/guard/health/snapshot",
            cookies={"wls": session_id},
        )
        assert r.status_code == 401


async def test_expired_session_rejected() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    service = FakeHealthService()
    async with _client(service, tokens, {"admin"}) as client:
        r = await client.get(
            "/api/admin/guard/health/snapshot",
            cookies={"wls": "invalid-session"},
        )
        assert r.status_code == 401


# --- Direct service tests using mock session ---


class _MockRow:
    """Mock row that supports attribute access by name."""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    def __iter__(self):
        return iter(self.__dict__.values())

    def __getitem__(self, idx):
        return list(self.__dict__.values())[idx]


class _MockResult:
    def __init__(self, rows=None, scalar=None, one=None):
        self._rows = rows
        self._scalar = scalar
        self._one = one

    def __iter__(self):
        return iter(self._rows or [])

    def scalar(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar

    def one(self):
        return self._one


class _MockSession:
    def __init__(self, results: list):
        self._results = list(results)
        self._idx = 0
        self.executed: list = []

    async def execute(self, stmt, *args, **kwargs):
        self.executed.append(stmt)
        if self._idx < len(self._results):
            r = self._results[self._idx]
            self._idx += 1
            return r
        return _MockResult(rows=[], scalar=0, one=(0, None))


async def test_health_service_snapshot_from_mock_session() -> None:
    """Test GuardHealthService.snapshot with a mock session factory."""
    from echo_v2.services.guard_health_service import GuardHealthService

    now = datetime.now(timezone.utc)

    # Build mock results in the order snapshot() calls execute():
    # 1. connections
    # 2. messages 1h (count + max)
    # 3. messages 24h
    # 4. security 1h
    # 5. security 24h
    # 6. exposure 1h
    # 7. exposure 24h
    # 8. assessment quality (parent_should_know rows)
    # 9. enrichment status
    # 10. terminal errors
    # 11. stuck enrichments
    # 12. failed enrichments count
    # 13. pipeline events
    # 14. ai usage 1h
    # 15. ai usage 24h
    # 16. guard alerts count
    # 17. delivery outcomes
    # 18. daily stats info
    # 19. connector events
    results = [
        # 1. connections - accessed by attribute (row.user_id, etc.)
        _MockResult(rows=[
            _MockRow(user_id="child-1", connection_status="connected", provider="baileys", updated_at=now, baileys_state_version=42),
            _MockRow(user_id="child-2", connection_status="provisioning", provider="baileys", updated_at=now, baileys_state_version=None),
        ]),
        # 2. messages 1h
        _MockResult(one=(56, now)),
        # 3. messages 24h
        _MockResult(one=(968, now)),
        # 4. security 1h
        _MockResult(scalar=16),
        # 5. security 24h
        _MockResult(scalar=310),
        # 6. exposure 1h
        _MockResult(scalar=4),
        # 7. exposure 24h
        _MockResult(scalar=89),
        # 8. assessment quality (iterates parent_should_know values)
        _MockResult(rows=[(None,), (True,), (False,), (None,)]),
        # 9. enrichment status
        _MockResult(rows=[("completed", 98), ("terminal", 38), ("failed", 2)]),
        # 10. terminal errors
        _MockResult(rows=[("media download URL is unavailable", 35)]),
        # 11. stuck enrichments - accessed by attribute
        _MockResult(rows=[
            _MockRow(id="cf581a38-aa35-4237-8132-62cc19a93254",
                     message_id="8a4dd8bc-7286-405a-b334-5240a528090e",
                     created_at=now, updated_at=now, lease_expires_at=None),
        ]),
        # 12. failed enrichments count
        _MockResult(scalar=2),
        # 13. pipeline events
        _MockResult(rows=[("media.enrichment.completed", 4)]),
        # 14. ai usage 1h
        _MockResult(one=(34, 0.0791)),
        # 15. ai usage 24h
        _MockResult(one=(36, 0.0826)),
        # 16. guard alerts count
        _MockResult(scalar=0),
        # 17. delivery outcomes
        _MockResult(rows=[("sent", 12), ("not_sent", 690)]),
        # 18. daily stats info
        _MockResult(one=(45, now)),
        # 19. connector events
        _MockResult(rows=[("processed", 1050)]),
    ]

    session = _MockSession(results)

    class MockSessionFactory:
        def __call__(self):
            class _Ctx:
                async def __aenter__(self):
                    return session

                async def __aexit__(self, *args):
                    pass

            return _Ctx()

    service = GuardHealthService(session_factory=MockSessionFactory())  # type: ignore[arg-type]
    snap = await service.snapshot()

    assert snap.connection_summary == {"connected": 1, "provisioning": 1}
    assert snap.messages_last_1h == 56
    assert snap.messages_last_24h == 968
    assert snap.security_assessments_last_1h == 16
    assert snap.security_assessments_last_24h == 310
    assert snap.exposure_assessments_last_1h == 4
    assert snap.exposure_assessments_last_24h == 89
    assert snap.psk_none_last_24h == 2  # two None values in the mock data
    assert snap.psk_true_last_24h == 1
    assert snap.enrichment_status_24h == {"completed": 98, "terminal": 38, "failed": 2}
    assert snap.terminal_errors_24h[0]["count"] == 35
    assert len(snap.stuck_enrichments) == 1
    assert snap.failed_enrichments_24h == 2
    assert snap.pipeline_events_1h == {"media.enrichment.completed": 4}
    assert snap.ai_usage_events_1h == 34
    assert snap.ai_usage_cost_1h == 0.0791
    assert snap.guard_alerts_24h == 0
    assert snap.delivery_outcomes_24h == {"sent": 12, "not_sent": 690}
    assert snap.daily_stats_rows == 45
    assert snap.connector_events_24h == {"processed": 1050}


async def test_health_service_summarize_connections() -> None:
    """Test the _summarize_connections helper directly."""
    from echo_v2.services.guard_health_service import GuardHealthService

    class NullSessionFactory:
        async def __call__(self):
            pass

    service = GuardHealthService(session_factory=NullSessionFactory())  # type: ignore[arg-type]
    connections = [
        {"user_id": "a", "status": "connected"},
        {"user_id": "b", "status": "connected"},
        {"user_id": "c", "status": "provisioning"},
    ]
    summary = service._summarize_connections(connections)
    assert summary == {"connected": 2, "provisioning": 1}


async def test_health_service_empty_snapshot() -> None:
    """Test snapshot with all empty/zero results."""
    from echo_v2.services.guard_health_service import GuardHealthService

    results = [
        _MockResult(rows=[]),  # connections
        _MockResult(one=(0, None)),  # messages 1h
        _MockResult(one=(0, None)),  # messages 24h
        _MockResult(scalar=0),  # security 1h
        _MockResult(scalar=0),  # security 24h
        _MockResult(scalar=0),  # exposure 1h
        _MockResult(scalar=0),  # exposure 24h
        _MockResult(rows=[]),  # assessment quality
        _MockResult(rows=[]),  # enrichment status
        _MockResult(rows=[]),  # terminal errors
        _MockResult(rows=[]),  # stuck enrichments
        _MockResult(scalar=0),  # failed enrichments
        _MockResult(rows=[]),  # pipeline events
        _MockResult(one=(0, 0)),  # ai usage 1h
        _MockResult(one=(0, 0)),  # ai usage 24h
        _MockResult(scalar=0),  # guard alerts
        _MockResult(rows=[]),  # delivery outcomes
        _MockResult(one=(0, None)),  # daily stats
        _MockResult(rows=[]),  # connector events
    ]

    session = _MockSession(results)

    class MockSessionFactory:
        def __call__(self):
            class _Ctx:
                async def __aenter__(self):
                    return session

                async def __aexit__(self, *args):
                    pass

            return _Ctx()

    service = GuardHealthService(session_factory=MockSessionFactory())  # type: ignore[arg-type]
    snap = await service.snapshot()

    assert snap.connection_summary == {}
    assert snap.messages_last_1h == 0
    assert snap.latest_message_at is None
    assert snap.security_assessments_last_1h == 0
    assert snap.psk_none_last_24h == 0
    assert snap.psk_true_last_24h == 0
    assert snap.enrichment_status_24h == {}
    assert snap.terminal_errors_24h == []
    assert snap.stuck_enrichments == []
    assert snap.failed_enrichments_24h == 0
    assert snap.pipeline_events_1h == {}
    assert snap.ai_usage_cost_1h == 0.0
    assert snap.guard_alerts_24h == 0
    assert snap.delivery_outcomes_24h == {}
    assert snap.daily_stats_rows == 0
    assert snap.daily_stats_last_updated is None
    assert snap.connector_events_24h == {}
