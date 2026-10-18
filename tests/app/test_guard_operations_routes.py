from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from echo_v2.app.guard_operations_routes import build_guard_operations_router
from echo_v2.persistence.waiting_list_tokens import InMemoryWaitingListSessionRepository
from echo_v2.services.guard_simple_telemetry_service import GuardActivityResponse
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


def _client(service: FakeOperationsService, tokens: WaitingListTokenService) -> AsyncClient:
    app = FastAPI()
    app.include_router(build_guard_operations_router(service=service, token_service=tokens))
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://test")


async def test_operations_route_requires_session_and_serializes_activity() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    session_id, _ = await tokens.issue(user_id="guardian")
    service = FakeOperationsService()
    async with _client(service, tokens) as client:
        unauthorized = await client.get("/api/debug/guard/operations")
        response = await client.get(
            "/api/debug/guard/operations", cookies={"wls": "valid"}
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
    assert "chat_name" in page.text
    assert "Conversation snapshot" in page.text
    assert "Decision transitions" not in page.text
    assert "Relevant messages for this transition" not in page.text
    assert "function resetFilters" in page.text
    assert "renderChildren(data.children)" in page.text
    assert "cursor=null;" in page.text
    assert "addEventListener('change',resetFilters)" in page.text


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


async def test_operations_token_exchange_rejects_expired_token() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    _, _ = await tokens.issue(user_id="guardian")
    service = FakeOperationsService()
    async with _client(service, tokens) as client:
        response = await client.get("/q/guard/operations/not-a-token")

    assert response.status_code == 200
    assert "expired" in response.text
