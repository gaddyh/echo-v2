from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from echo_v2.app.guard_operations_routes import build_guard_operations_router
from echo_v2.persistence.waiting_list_tokens import InMemoryWaitingListSessionRepository
from echo_v2.services.guard_operations_service import GuardOperationsResponse
from echo_v2.services.waiting_list_token_service import WaitingListTokenService


class FakeOperationsService:
    def __init__(self) -> None:
        self.response = GuardOperationsResponse(
            guardian_user_id="guardian",
            generated_at=datetime(2026, 10, 15, tzinfo=timezone.utc),
            summary={"monitored_chats": 1},
            chats=(
                {
                    "chat_id": "chat@c.us",
                    "last_message_at": datetime(2026, 10, 15, tzinfo=timezone.utc),
                    "latest_delivery": None,
                },
            ),
        )

    async def list_dashboard(self, **kwargs):
        if kwargs["session_id"] == "expired":
            return None
        return self.response


def _client(service: FakeOperationsService, tokens: WaitingListTokenService) -> AsyncClient:
    app = FastAPI()
    app.include_router(build_guard_operations_router(service=service, token_service=tokens))
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://test")


async def test_operations_route_requires_session_and_serializes_dashboard() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    service = FakeOperationsService()
    async with _client(service, tokens) as client:
        unauthorized = await client.get("/api/debug/guard/operations")
        response = await client.get(
            "/api/debug/guard/operations", cookies={"wls": "valid"}
        )
        page = await client.get("/debug/guard/operations")

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    assert response.json()["chats"][0]["chat_id"] == "chat@c.us"
    assert response.headers["cache-control"] == "no-store"
    assert page.status_code == 200
    assert "Guard operations" in page.text
    assert "Current state" in page.text
    assert "Decision transitions" in page.text
    assert "Relevant messages for this transition" in page.text
    assert "Complete analysis log" in page.text


async def test_operations_token_exchange_rejects_expired_token() -> None:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    service = FakeOperationsService()
    async with _client(service, tokens) as client:
        response = await client.get("/q/guard/operations/not-a-token")

    assert response.status_code == 200
    assert "expired" in response.text
