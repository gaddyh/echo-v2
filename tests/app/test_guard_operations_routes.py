from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from echo_v2.app.guard_admin_dashboard_page import ADMIN_DASHBOARD_PAGE
from echo_v2.app.guard_operations_routes import build_guard_operations_router
from echo_v2.persistence.waiting_list_tokens import InMemoryWaitingListSessionRepository
from echo_v2.services.guard_simple_telemetry_service import GuardActivityResponse
from echo_v2.services.ops_health_rules import OpsHealthRules
from echo_v2.services.waiting_list_token_service import WaitingListTokenService


class FakeOperationsService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.response = GuardActivityResponse(
            guardian_user_id="guardian",
            generated_at=datetime(2026, 10, 15, tzinfo=timezone.utc),
            summary={"analyzed": 1, "yes": 1, "no": 0, "unknown": 0, "notifications": 1},
            children=({"id": "child", "name": "Shay"},),
            analyses=(
                {
                    "id": "assessment-1",
                    "decision": "yes",
                    "child_name": "Shay",
                    "chat_name": "Class group",
                    "created_at": datetime(2026, 10, 15, tzinfo=timezone.utc),
                    "message_count": 37,
                    "summary": "Repeated humiliation.",
                    "delivery": {"status": "sent", "label": "Sent"},
                },
            ),
            next_cursor=None,
        )

    async def list_activity(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["session_id"] == "expired":
            return None
        return self.response

    async def get_analysis(self, **kwargs):
        return {"id": kwargs["assessment_id"], "decision": "yes"}

    async def get_context(self, **kwargs):
        return {"trust": "exact message selection, verified content", "messages": []}


class FakeOpsSnapshot:
    """Static ops snapshot for route-level tests (service tested separately)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.payload = {
            "generated_at": "2026-11-27T12:00:00+00:00",
            "period": "24h",
            "period_seconds": 86400,
            "filters": {"provider": "all", "worker_id": None, "user_id": None},
            "settings": {},
            "sections": {
                "providers": {"available": True, "providers": [], "connection_summary": {}},
                "waha_infrastructure": {"available": True, "worker_summary": {}, "workers": []},
                "users": {"available": True, "users": []},
                "ingestion": {"available": True, "waha_inbox": {"current": {}}},
                "media": {"available": True},
                "guard_analysis": {"available": True},
                "notifications": {"available": True},
                "management": {"available": True},
                "ai_costs": {"available": True},
                "overview": {"available": True},
            },
        }

    async def snapshot(self, **kwargs):
        self.calls.append(kwargs)
        return {**self.payload, "period": kwargs.get("period", "24h")}

    async def user_detail(self, user_id: str):
        return {"available": True, "user": {"user_id": user_id, "first_name": "Shay"}}


class FakeErrorView:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def list_errors(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "available": True,
            "errors": [
                {
                    "timestamp": "2026-11-27T11:00:00+00:00",
                    "subsystem": "waha_message_consumer",
                    "provider": "waha",
                    "user_id": None,
                    "worker_id": "session-1",
                    "category": "inbox_retryable",
                    "description": "boom",
                    "state": "retryable",
                    "attempt_count": 2,
                    "recovered": None,
                }
            ],
            "total_in_window": 1,
            "offset": 0,
            "limit": 50,
            "aggregates": [{"subsystem": "waha_message_consumer", "category": "inbox_retryable", "count": 1}],
            "notes": "",
        }


def _client(
    service: FakeOperationsService,
    tokens: WaitingListTokenService,
    admin_user_ids: set[str] | None = None,
    ops_snapshot: FakeOpsSnapshot | None = None,
    error_view: FakeErrorView | None = None,
) -> AsyncClient:
    app = FastAPI()
    app.include_router(
        build_guard_operations_router(
            service=service,
            token_service=tokens,
            admin_user_ids=admin_user_ids or set(),
            ops_center_service=ops_snapshot,
            ops_health_rules=OpsHealthRules() if ops_snapshot else None,
            ops_error_view=error_view,
        )
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://test")


async def test_operations_route_requires_session_and_serializes_activity() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    session_id, _ = await tokens.issue(user_id="guardian")
    service = FakeOperationsService()
    async with _client(service, tokens) as client:
        unauthorized = await client.get("/api/debug/guard/operations")
        response = await client.get(
            "/api/debug/guard/operations", cookies={"wls": session_id}
        )
        detail = await client.get(
            "/api/debug/guard/operations/assessment-1", cookies={"wls": session_id}
        )
        context = await client.get(
            "/api/debug/guard/operations/assessment-1/context", cookies={"wls": session_id}
        )
        page = await client.get("/debug/guard/operations")

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    assert response.json()["analyses"][0]["decision"] == "yes"
    assert response.json()["summary"]["notifications"] == 1
    assert detail.status_code == 200
    assert context.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert page.status_code == 200
    assert "Echo Guard — Activity" in page.text
    assert "Confirmed notifications" in page.text
    assert 'placeholder="Chat name"' in page.text


async def test_admin_page_is_echo_operations_center() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    service = FakeOperationsService()
    async with _client(service, tokens) as client:
        page = await client.get("/debug/guard/admin")

    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "Echo Operations Center" in page.text
    assert "Provider health" in page.text
    assert "WAHA infrastructure" in page.text
    assert "Per-user monitoring health" in page.text
    assert "Recent errors and operational incidents" in page.text
    # No fake metrics in the shipped page.
    assert "lorem" not in page.text.lower()


async def test_admin_session_forwards_admin_scope() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    session_id, _ = await tokens.issue(user_id="admin")
    service = FakeOperationsService()
    async with _client(service, tokens, {"admin"}) as client:
        response = await client.get(
            "/api/debug/guard/operations", cookies={"wls": session_id}
        )

    assert response.status_code == 200
    assert service.calls[0]["admin"] is True


async def test_operations_route_forwards_all_filters_and_pagination() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    session_id, _ = await tokens.issue(user_id="guardian")
    service = FakeOperationsService()
    async with _client(service, tokens) as client:
        response = await client.get(
            "/api/debug/guard/operations",
            params={
                "child_id": "child-2",
                "chat_id": "chat-2",
                "chat_name": "Class",
                "decision": "yes",
                "date_from": "2026-10-01",
                "date_to": "2026-10-15",
                "limit": "10",
                "cursor": "next-page",
            },
            cookies={"wls": session_id},
        )

    assert response.status_code == 200
    assert service.calls == [
        {
            "session_id": session_id,
            "child_user_id": "child-2",
            "chat_id": "chat-2",
            "chat_name": "Class",
            "decision": "yes",
            "date_from": datetime(2026, 10, 1, tzinfo=timezone.utc),
            "date_to": datetime(2026, 10, 16, tzinfo=timezone.utc),
            "limit": 10,
            "cursor": "next-page",
            "admin": False,
        }
    ]


async def test_operations_token_exchange_sets_session_cookie_for_valid_token() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    _, token = await tokens.issue(user_id="guardian")
    service = FakeOperationsService()
    async with _client(service, tokens) as client:
        response = await client.get(f"/q/guard/operations/{token}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/debug/guard/operations"
    assert "wls=" in response.headers["set-cookie"]


async def test_operations_detail_and_context_reject_expired_session() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    service = FakeOperationsService()
    async with _client(service, tokens) as client:
        detail = await client.get(
            "/api/debug/guard/operations/assessment-1", cookies={"wls": "expired"}
        )
        context = await client.get(
            "/api/debug/guard/operations/assessment-1/context", cookies={"wls": "expired"}
        )
    assert detail.status_code == 401
    assert context.status_code == 401


async def test_admin_route_requires_admin_session_and_returns_snapshot() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    session_id, _ = await tokens.issue(user_id="admin")
    service = FakeOperationsService()
    ops = FakeOpsSnapshot()
    async with _client(service, tokens, {"admin"}, ops) as client:
        no_cookie = await client.get("/api/debug/guard/admin")
        response = await client.get(
            "/api/debug/guard/admin",
            params={"period": "1h", "provider": "waha", "worker_id": "w1"},
            cookies={"wls": session_id},
        )

    assert no_cookie.status_code == 401
    assert response.status_code == 200
    body = response.json()
    assert body["period"] == "1h"
    assert body["sections"]["providers"]["available"] is True
    assert "health" in body
    assert ops.calls == [
        {"period": "1h", "provider": "waha", "worker_id": "w1", "user_id": None}
    ]


async def test_admin_route_rejects_non_admin_and_missing_service() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    session_id, _ = await tokens.issue(user_id="guardian")
    service = FakeOperationsService()
    async with _client(service, tokens, {"admin"}, FakeOpsSnapshot()) as client:
        response = await client.get(
            "/api/debug/guard/admin", cookies={"wls": session_id}
        )
    async with _client(service, tokens, {"admin"}) as client:
        missing = await client.get(
            "/api/debug/guard/admin", cookies={"wls": session_id}
        )

    assert response.status_code == 401
    assert missing.status_code == 401


async def test_admin_user_detail_and_errors_require_admin() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    session_id, _ = await tokens.issue(user_id="admin")
    guardian_session, _ = await tokens.issue(user_id="guardian")
    service = FakeOperationsService()
    ops = FakeOpsSnapshot()
    errors = FakeErrorView()
    async with _client(service, tokens, {"admin"}, ops, errors) as client:
        detail = await client.get(
            "/api/debug/guard/admin/users/1234",
            cookies={"wls": session_id},
        )
        error_list = await client.get(
            "/api/debug/guard/admin/errors",
            params={"subsystem": "waha_message_consumer", "severity": "retryable"},
            cookies={"wls": session_id},
        )
    async with _client(service, tokens, {"admin"}, FakeOpsSnapshot(), FakeErrorView()) as client:
        non_admin = await client.get(
            "/api/debug/guard/admin/users/1234", cookies={"wls": guardian_session}
        )
        no_service = _client(service, tokens, {"admin"})
        missing = await no_service.get(
            "/api/debug/guard/admin/errors", cookies={"wls": session_id}
        )

    assert detail.status_code == 200
    assert detail.json()["user"]["first_name"] == "Shay"
    assert error_list.status_code == 200
    assert error_list.json()["errors"][0]["subsystem"] == "waha_message_consumer"
    assert error_list.json()["aggregates"][0]["count"] == 1
    assert non_admin.status_code == 401
    assert missing.status_code == 401


async def test_operations_token_exchange_rejects_expired_token() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    _, _ = await tokens.issue(user_id="guardian")
    service = FakeOperationsService()
    async with _client(service, tokens) as client:
        response = await client.get("/q/guard/operations/not-a-token")

    assert response.status_code == 200
    assert "expired" in response.text


def test_admin_page_has_no_sensitive_fields() -> None:
    lowered = ADMIN_DASHBOARD_PAGE.lower()
    for forbidden in (
        "management_token",
        "qr_image",
        "api_key",
        "webhook_token",
        "credentials",
        "last_qr_image",
    ):
        assert forbidden not in lowered
