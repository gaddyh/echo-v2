from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from echo_v2.app.guard_parent_alert_routes import build_guard_parent_alert_router
from echo_v2.domain.guard_alert import GuardAlert, GuardAlertKind, GuardAlertStatus
from echo_v2.persistence.guard_alert_repositories import InMemoryGuardAlertRepository
from echo_v2.persistence.waiting_list_tokens import InMemoryWaitingListSessionRepository
from echo_v2.services.guard_parent_alert_service import GuardParentAlertService
from echo_v2.services.waiting_list_token_service import WaitingListTokenService

pytestmark = pytest.mark.asyncio

NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


class FakeUsers:
    async def get_user_info_by_id(self, user_id: str) -> tuple[str, str | None] | None:
        if user_id == "child":
            return "+972500000002", "דנה"
        return None


def _alert(session_id: str, *, guardian_user_id: str = "guardian") -> GuardAlert:
    return GuardAlert(
        id="alert-1",
        analysis_result_id="result-1",
        guardian_user_id=guardian_user_id,
        child_user_id="child",
        chat_id="chat@c.us",
        kind=GuardAlertKind.URGENT,
        categories=("suspicious_contact", "location_request"),
        summary="Guard detected a potentially concerning pattern.",
        policy_reason="Immediate or escalating high-risk evidence.",
        policy_version="policy-v1",
        status=GuardAlertStatus.SCHEDULED,
        scheduled_action_id="action-1",
        logical_event_key="event-1",
        threshold_family="suspicious_contact",
        episode_start_version=1,
        detected_at=NOW,
        expires_at=NOW + timedelta(days=1),
        created_at=NOW,
        metadata={"session_id": session_id, "review_token": "raw-token"},
    )


async def _client() -> tuple[AsyncClient, WaitingListTokenService, InMemoryGuardAlertRepository]:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    alerts = InMemoryGuardAlertRepository()
    service = GuardParentAlertService(token_service=tokens, alerts=alerts, users=FakeUsers())
    app = FastAPI()
    app.include_router(build_guard_parent_alert_router(token_service=tokens, service=service))
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://test"), tokens, alerts


async def test_alert_token_redirects_to_parent_page_and_cookie_loads_alert() -> None:
    client, tokens, alerts = await _client()
    session_id, raw_token = await tokens.issue("guardian")
    await alerts.save_alert(_alert(session_id))

    async with client:
        exchange = await client.get(f"/q/guard/{raw_token}")
        response = await client.get("/api/guard/alert")
        page = await client.get("/guard/alert")
        revisited = await client.get("/api/guard/alert")

    assert exchange.status_code == 303
    assert exchange.headers["location"] == "/guard/alert"
    assert "wls=" in exchange.headers["set-cookie"]
    assert "HttpOnly" in exchange.headers["set-cookie"]
    assert "SameSite=lax" in exchange.headers["set-cookie"]
    assert response.status_code == 200
    assert response.json() == {
        "alert_id": "alert-1",
        "child_name": "דנה",
        "detected_at": NOW.isoformat(),
        "severity": "urgent",
        "categories": ["suspicious contact", "location request"],
        "summary": "Guard detected a potentially concerning pattern.",
        "concern": "זוהה סימן מיידי או מתגבר שדורש תשומת לב.",
        "expired": False,
    }
    assert page.status_code == 200
    assert "התראת בטיחות" in page.text
    assert "למה זה מדאיג?" in page.text
    assert revisited.status_code == 200


async def test_parent_alert_rejects_missing_or_wrong_session() -> None:
    client, tokens, alerts = await _client()
    session_id, _ = await tokens.issue("guardian")
    await alerts.save_alert(_alert(session_id))
    other_session, _ = await tokens.issue("other-guardian")

    async with client:
        missing = await client.get("/api/guard/alert")
        wrong = await client.get("/api/guard/alert", cookies={"wls": other_session})
        expired = await client.get("/q/guard/not-a-token")

    assert missing.status_code == 401
    assert wrong.status_code == 404
    assert expired.status_code == 200
    assert "הקישור אינו זמין" in expired.text


async def test_parent_page_does_not_expose_internal_alert_fields() -> None:
    client, tokens, alerts = await _client()
    session_id, raw_token = await tokens.issue("guardian")
    await alerts.save_alert(_alert(session_id))

    async with client:
        await client.get(f"/q/guard/{raw_token}")
        response = await client.get("/api/guard/alert")

    body = response.text
    assert "policy-v1" not in body
    assert "result-1" not in body
    assert "raw-token" not in body
    assert "chat@c.us" not in body
