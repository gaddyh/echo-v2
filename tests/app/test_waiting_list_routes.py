"""Tests for the waiting-list mini web app routes.

Covers:
* Token → cookie exchange on GET /q/{token}.
* Expired/invalid token → expired-link page.
* GET /api/waiting with cookie → JSON list.
* POST /api/waiting/items/{active_id}/actions → action response.
* Security headers on all responses.
* Cookie attributes (Secure, HttpOnly, SameSite).
* Rate limiting by session_id.
* No raw token in API paths.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from echo_v2.app.waiting_list_routes import build_waiting_list_router
from echo_v2.persistence.chat_repositories import (
    InMemoryChatStateRepository,
    InMemoryMessageRepository,
    InMemoryWaitingForMeActiveRepository,
    InMemoryWaitingForMeResultRepository,
)
from echo_v2.persistence.contacts import InMemoryContactRepository
from echo_v2.persistence.feedback_repositories import (
    InMemoryChatMuteRepository,
    InMemoryWaitingForMeActionRepository,
    InMemoryWaitingForMeFeedbackRepository,
)
from echo_v2.persistence.waiting_list_tokens import (
    InMemoryWaitingListSessionRepository,
)
from echo_v2.services.feedback_service import WaitingForMeActionService
from echo_v2.services.waiting_list_query import WaitingListQueryService
from echo_v2.services.waiting_list_service import WaitingListService
from echo_v2.services.waiting_list_token_service import WaitingListTokenService

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 12, 6, 0, 0, tzinfo=timezone.utc)
USER_ID = "user-1"
CHAT_ID = "972508765432@c.us"
RESULT_ID = "result-1"
BOT_PHONE = "972500000000"


def _make_app() -> tuple[
    FastAPI,
    WaitingListTokenService,
    WaitingListService,
    InMemoryWaitingForMeActiveRepository,
]:
    active_repo = InMemoryWaitingForMeActiveRepository()
    action_repo = InMemoryWaitingForMeActionRepository()
    feedback_repo = InMemoryWaitingForMeFeedbackRepository()
    chat_state_repo = InMemoryChatStateRepository()
    message_repo = InMemoryMessageRepository()
    contact_repo = InMemoryContactRepository()
    mute_repo = InMemoryChatMuteRepository()
    result_repo = InMemoryWaitingForMeResultRepository()
    session_repo = InMemoryWaitingListSessionRepository()

    token_service = WaitingListTokenService(session_repo)
    query_service = WaitingListQueryService(
        active_repo=active_repo,
        chat_state_repo=chat_state_repo,
        mute_repo=mute_repo,
    )
    action_service = WaitingForMeActionService(
        active_repo=active_repo,
        action_repo=action_repo,
        mute_repo=mute_repo,
        feedback_repo=feedback_repo,
        result_repo=result_repo,
    )
    # Scheduling infra for the send endpoint.
    from echo_v2.observability import InMemoryEventSink
    from echo_v2.persistence.scheduled_actions import InMemoryScheduledActionRepository
    from echo_v2.persistence.whatsapp_connections import (
        InMemoryWhatsAppConnectionRepository,
        StoredConnection,
    )
    from echo_v2.ports.whatsapp import (
        ConnectionRef,
        ConnectionStatus,
        ProviderCredentials,
    )
    from echo_v2.runtime.idempotency import InMemoryIdempotencyStore
    from echo_v2.services.scheduling import SchedulingService

    scheduled_action_repo = InMemoryScheduledActionRepository()
    conn_repo = InMemoryWhatsAppConnectionRepository()
    conn_repo._by_ref[("green", "123")] = StoredConnection(
        user_id=USER_ID,
        ref=ConnectionRef("green", "123"),
        credentials=ProviderCredentials(b"api-tok"),
        webhook_token_hash=b"\x00" * 32,
        status=ConnectionStatus.CONNECTED,
    )
    conn_repo._by_user[USER_ID] = ("green", "123")

    class _FakeMessaging:
        async def send_message(self, connection, chat_id, message) -> str:
            return "MSG_1"

    scheduling_service = SchedulingService(
        action_repo=scheduled_action_repo,
        connection_repo=conn_repo,
        messaging=_FakeMessaging(),
        idempotency_store=InMemoryIdempotencyStore(),
        event_sink=InMemoryEventSink(),
    )
    service = WaitingListService(
        token_service=token_service,
        query_service=query_service,
        action_service=action_service,
        action_repo=action_repo,
        chat_state_repo=chat_state_repo,
        message_repo=message_repo,
        contact_repo=contact_repo,
        result_repo=result_repo,
        scheduling_service=scheduling_service,
        active_repo=active_repo,
    )
    router = build_waiting_list_router(
        token_service=token_service,
        waiting_list_service=service,
        bot_phone=BOT_PHONE,
    )
    app = FastAPI()
    app.include_router(router)
    return app, token_service, service, active_repo


async def _setup_active(
    active_repo: InMemoryWaitingForMeActiveRepository,
    chat_state_repo: InMemoryChatStateRepository,
):
    from echo_v2.ports.whatsapp import MessageDirection

    await chat_state_repo.upsert_on_message(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        direction=MessageDirection.INBOUND,
        observed_at=NOW,
        next_analysis_at=None,
    )
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        target_version=1,
        result_id=RESULT_ID,
        waiting_since=NOW,
    )
    active = await active_repo.get(user_id=USER_ID, chat_id=CHAT_ID)
    return active.id


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://test")


async def test_valid_token_sets_cookie_and_serves_page():
    """GET /q/{token} with a valid token sets a cookie and serves the HTML page."""
    app, token_service, _, _ = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    async with _client(app) as client:
        resp = await client.get(f"/q/{raw_token}")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    # Cookie set.
    set_cookie = resp.headers.get("set-cookie", "")
    assert "wls=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    # Security headers.
    assert resp.headers.get("cache-control") == "no-store"
    assert resp.headers.get("referrer-policy") == "no-referrer"
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert "default-src 'self'" in resp.headers.get("content-security-policy", "")


async def test_invalid_token_serves_expired_page():
    app, _, _, _ = _make_app()
    async with _client(app) as client:
        resp = await client.get("/q/invalid-token")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert "הקישור פג" in resp.text
    assert "בקש סיכום חדש" in resp.text


async def test_api_waiting_without_cookie_returns_401():
    app, _, _, _ = _make_app()
    async with _client(app) as client:
        resp = await client.get("/api/waiting")
    assert resp.status_code == 401


async def test_api_waiting_with_cookie_returns_items():
    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    await _setup_active(active_repo, service._chat_state_repo)
    async with _client(app) as client:
        # Exchange token for cookie.
        await client.get(f"/q/{raw_token}")
        resp = await client.get("/api/waiting")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "summary" in data
    assert len(data["items"]) == 1
    assert data["summary"]["waiting"] == 1


async def test_api_waiting_includes_situation_summary():
    """The API response includes situation_summary when the result has one."""
    from echo_v2.domain.waiting_for_me import (
        WaitingForMeDecision,
        WaitingForMeResult,
    )

    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)

    # Save a result with a summary.
    result_id = await service._result_repo.save(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        result=WaitingForMeResult(
            decision=WaitingForMeDecision.WAITING_FOR_ME,
            confidence=0.9,
            reason="Direct question.",
            summary="רוצה לתאם פגישה ומחכה לאישור.",
            target_version=1,
        ),
    )
    # Set up active pointing to this result.
    from echo_v2.ports.whatsapp import MessageDirection

    await service._chat_state_repo.upsert_on_message(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        direction=MessageDirection.INBOUND,
        observed_at=NOW,
        next_analysis_at=None,
    )
    await active_repo.upsert(
        user_id=USER_ID,
        chat_id=CHAT_ID,
        target_version=1,
        result_id=result_id,
        waiting_since=NOW,
    )

    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.get("/api/waiting")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["situation_summary"] == "רוצה לתאם פגישה ומחכה לאישור."
    assert "message_preview" in item


async def test_api_action_done():
    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    active_id = await _setup_active(active_repo, service._chat_state_repo)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.post(
            f"/api/waiting/items/{active_id}/actions",
            json={
                "action_id": "action-1",
                "expected_version": 1,
                "action": "done",
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["outcome"] == "applied"
    assert data["summary"]["completed"] == 1


async def test_api_action_duplicate():
    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    active_id = await _setup_active(active_repo, service._chat_state_repo)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        # First call.
        resp1 = await client.post(
            f"/api/waiting/items/{active_id}/actions",
            json={"action_id": "action-1", "expected_version": 1, "action": "done"},
        )
        assert resp1.status_code == 200
        assert resp1.json()["outcome"] == "applied"
        # Retry with same action_id.
        resp2 = await client.post(
            f"/api/waiting/items/{active_id}/actions",
            json={"action_id": "action-1", "expected_version": 1, "action": "done"},
        )
    assert resp2.status_code == 200
    assert resp2.json()["outcome"] == "duplicate"


async def test_api_action_stale():
    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    active_id = await _setup_active(active_repo, service._chat_state_repo)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.post(
            f"/api/waiting/items/{active_id}/actions",
            json={"action_id": "action-1", "expected_version": 99, "action": "done"},
        )
    assert resp.status_code == 409
    assert resp.json()["outcome"] == "stale"


async def test_api_action_not_found():
    app, token_service, _, _ = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.post(
            "/api/waiting/items/nonexistent/actions",
            json={"action_id": "action-1", "expected_version": 1, "action": "done"},
        )
    assert resp.status_code == 404
    assert resp.json()["outcome"] == "not_found"


async def test_api_action_snooze_preset():
    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    active_id = await _setup_active(active_repo, service._chat_state_repo)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.post(
            f"/api/waiting/items/{active_id}/actions",
            json={
                "action_id": "action-1",
                "expected_version": 1,
                "action": "snooze",
                "snooze_preset": "tomorrow",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "applied"


async def test_api_action_dismiss_with_reason():
    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    active_id = await _setup_active(active_repo, service._chat_state_repo)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.post(
            f"/api/waiting/items/{active_id}/actions",
            json={
                "action_id": "action-1",
                "expected_version": 1,
                "action": "dismiss",
                "dismiss_reason": "detected_incorrectly",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "applied"


async def test_security_headers_on_api_response():
    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    await _setup_active(active_repo, service._chat_state_repo)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.get("/api/waiting")
    assert resp.headers.get("cache-control") == "no-store"
    assert resp.headers.get("referrer-policy") == "no-referrer"
    assert resp.headers.get("x-content-type-options") == "nosniff"


async def test_expired_page_route():
    """GET /q/expired serves the expired-link page."""
    app, _, _, _ = _make_app()
    async with _client(app) as client:
        resp = await client.get("/q/expired")
    assert resp.status_code == 200
    assert "הקישור פג" in resp.text


async def test_api_waiting_invalid_session_cookie_returns_401():
    """A cookie with an invalid session id returns 401."""
    app, _, _, _ = _make_app()
    async with _client(app) as client:
        resp = await client.get(
            "/api/waiting",
            cookies={"wls": "invalid-session-id"},
        )
    assert resp.status_code == 401


async def test_api_action_invalid_action_returns_invalid():
    """An unknown action returns outcome 'invalid'."""
    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    active_id = await _setup_active(active_repo, service._chat_state_repo)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.post(
            f"/api/waiting/items/{active_id}/actions",
            json={
                "action_id": "action-1",
                "expected_version": 1,
                "action": "unknown_action",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "invalid"


async def test_api_action_snooze_invalid_preset_returns_422():
    """An invalid snooze preset returns 422."""
    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    active_id = await _setup_active(active_repo, service._chat_state_repo)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.post(
            f"/api/waiting/items/{active_id}/actions",
            json={
                "action_id": "action-1",
                "expected_version": 1,
                "action": "snooze",
                "snooze_preset": "invalid_preset",
            },
        )
    assert resp.status_code == 422
    assert resp.json()["outcome"] == "invalid_snooze"


async def test_api_action_dismiss_already_handled():
    """Dismiss with 'already_handled' reason succeeds with no feedback."""
    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    active_id = await _setup_active(active_repo, service._chat_state_repo)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.post(
            f"/api/waiting/items/{active_id}/actions",
            json={
                "action_id": "action-1",
                "expected_version": 1,
                "action": "dismiss",
                "dismiss_reason": "already_handled",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "applied"


async def test_api_action_snooze_custom_datetime():
    """Snooze with a custom ISO datetime succeeds."""
    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    active_id = await _setup_active(active_repo, service._chat_state_repo)
    future = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.post(
            f"/api/waiting/items/{active_id}/actions",
            json={
                "action_id": "action-1",
                "expected_version": 1,
                "action": "snooze",
                "snooze_until": future,
            },
        )
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "applied"


async def test_api_action_snooze_invalid_datetime_returns_422():
    """Snooze with an invalid datetime format returns 422."""
    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    active_id = await _setup_active(active_repo, service._chat_state_repo)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.post(
            f"/api/waiting/items/{active_id}/actions",
            json={
                "action_id": "action-1",
                "expected_version": 1,
                "action": "snooze",
                "snooze_until": "not-a-date",
            },
        )
    assert resp.status_code == 422


async def test_api_action_without_cookie_returns_401():
    """POST without a cookie returns 401."""
    app, _, _, _ = _make_app()
    async with _client(app) as client:
        resp = await client.post(
            "/api/waiting/items/any/actions",
            json={"action_id": "a1", "expected_version": 1, "action": "done"},
        )
    assert resp.status_code == 401


# --- Edge cases ---


async def test_token_reuse_still_works():
    """Opening the same link twice: both times serve the page.

    The token remains valid until expiry/revocation. ``mark_opened``
    is for auditing only — it does not prevent reuse.
    """
    app, token_service, _, _ = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    async with _client(app) as client:
        resp1 = await client.get(f"/q/{raw_token}")
        assert resp1.status_code == 200
        assert "wls=" in resp1.headers.get("set-cookie", "")
        # Second time — token still valid.
        resp2 = await client.get(f"/q/{raw_token}")
    assert resp2.status_code == 200
    assert "wls=" in resp2.headers.get("set-cookie", "")


async def test_revoked_session_returns_401():
    """A revoked session cookie returns 401."""
    app, token_service, service, active_repo = _make_app()
    session_id, raw_token = await token_service.issue(USER_ID)
    await _setup_active(active_repo, service._chat_state_repo)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        # Revoke the session.
        await token_service.revoke(session_id)
        resp = await client.get("/api/waiting")
    assert resp.status_code == 401


async def test_invalid_uuid_active_id_returns_404():
    """An invalid UUID as active_id returns 404, not a crash."""
    app, token_service, _, _ = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.post(
            "/api/waiting/items/not-a-uuid-at-all/actions",
            json={"action_id": "a1", "expected_version": 1, "action": "done"},
        )
    assert resp.status_code == 404
    assert resp.json()["outcome"] == "not_found"


async def test_snooze_until_naive_datetime_treated_as_utc():
    """A timezone-naive snooze_until is treated as UTC."""
    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    active_id = await _setup_active(active_repo, service._chat_state_repo)
    future = (datetime.now(timezone.utc) + timedelta(hours=6)).replace(tzinfo=None).isoformat()
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.post(
            f"/api/waiting/items/{active_id}/actions",
            json={
                "action_id": "action-1",
                "expected_version": 1,
                "action": "snooze",
                "snooze_until": future,
            },
        )
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "applied"


async def test_missing_action_id_returns_422():
    """POST without action_id returns 422 (Pydantic validation)."""
    app, token_service, _, _ = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.post(
            "/api/waiting/items/some-id/actions",
            json={"expected_version": 1, "action": "done"},
        )
    assert resp.status_code == 422


async def test_missing_action_field_returns_422():
    """POST without the 'action' field returns 422."""
    app, token_service, _, _ = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.post(
            "/api/waiting/items/some-id/actions",
            json={"action_id": "a1", "expected_version": 1},
        )
    assert resp.status_code == 422


async def test_expired_page_has_back_to_whatsapp_link():
    """The expired page contains a wa.me link to the Echo bot."""
    app, _, _, _ = _make_app()
    async with _client(app) as client:
        resp = await client.get("/q/invalid-token")
    assert "wa.me/972500000000" in resp.text


async def test_valid_page_has_bot_phone():
    """The main page has the bot phone embedded for the JS back button."""
    app, token_service, _, _ = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    async with _client(app) as client:
        resp = await client.get(f"/q/{raw_token}")
    # The BOT_PHONE JS variable is replaced in the HTML source.
    assert 'BOT_PHONE = "972500000000"' in resp.text


async def test_security_headers_on_expired_page():
    """The expired page also has security headers."""
    app, _, _, _ = _make_app()
    async with _client(app) as client:
        resp = await client.get("/q/invalid-token")
    assert resp.headers.get("cache-control") == "no-store"
    assert resp.headers.get("referrer-policy") == "no-referrer"
    assert resp.headers.get("x-content-type-options") == "nosniff"


async def test_security_headers_on_post_response():
    """POST responses also have security headers."""
    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    active_id = await _setup_active(active_repo, service._chat_state_repo)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.post(
            f"/api/waiting/items/{active_id}/actions",
            json={"action_id": "a1", "expected_version": 1, "action": "done"},
        )
    assert resp.headers.get("cache-control") == "no-store"
    assert resp.headers.get("referrer-policy") == "no-referrer"
    assert resp.headers.get("x-content-type-options") == "nosniff"


async def test_snooze_until_with_z_suffix():
    """Snooze with ISO 8601 Z suffix works."""
    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    active_id = await _setup_active(active_repo, service._chat_state_repo)
    future = (datetime.now(timezone.utc) + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.post(
            f"/api/waiting/items/{active_id}/actions",
            json={
                "action_id": "action-1",
                "expected_version": 1,
                "action": "snooze",
                "snooze_until": future,
            },
        )
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "applied"


async def test_dismiss_without_reason_still_works():
    """Dismiss without a reason still resolves the item."""
    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    active_id = await _setup_active(active_repo, service._chat_state_repo)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.post(
            f"/api/waiting/items/{active_id}/actions",
            json={"action_id": "a1", "expected_version": 1, "action": "dismiss"},
        )
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "applied"


async def test_rate_limit_returns_429():
    """Exceeding the rate limit returns 429."""
    from echo_v2.app.waiting_list_routes import _RATE_LIMIT_MAX

    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    await _setup_active(active_repo, service._chat_state_repo)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        # Make RATE_LIMIT_MAX successful requests.
        for _ in range(_RATE_LIMIT_MAX):
            resp = await client.get("/api/waiting")
            assert resp.status_code == 200
        # Next request should be rate limited.
        resp = await client.get("/api/waiting")
    assert resp.status_code == 429


async def test_rate_limit_post_returns_429():
    """Rate limit on POST also returns 429."""
    from echo_v2.app.waiting_list_routes import _RATE_LIMIT_MAX

    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    active_id = await _setup_active(active_repo, service._chat_state_repo)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        # Exhaust the rate limit with GET requests.
        for _ in range(_RATE_LIMIT_MAX):
            await client.get("/api/waiting")
        # Now POST should also be rate limited.
        resp = await client.post(
            f"/api/waiting/items/{active_id}/actions",
            json={"action_id": "a1", "expected_version": 1, "action": "done"},
        )
    assert resp.status_code == 429


async def test_list_items_session_invalid_returns_401():
    """If list_items returns None (session invalid), returns 401."""
    app, token_service, _, _ = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        # Revoke the session so list_items returns None.
        sessions = token_service._repo._sessions  # type: ignore[attr-defined]
        for sid in sessions:
            await token_service.revoke(sid)
        resp = await client.get("/api/waiting")
    assert resp.status_code == 401


async def test_execute_action_session_invalid_returns_401():
    """If execute_action returns None (session invalid), returns 401."""
    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    active_id = await _setup_active(active_repo, service._chat_state_repo)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        # Revoke the session.
        sessions = token_service._repo._sessions  # type: ignore[attr-defined]
        for sid in sessions:
            await token_service.revoke(sid)
        resp = await client.post(
            f"/api/waiting/items/{active_id}/actions",
            json={"action_id": "a1", "expected_version": 1, "action": "done"},
        )
    assert resp.status_code == 401


# --- POST /api/waiting/items/{active_id}/send tests ---


async def test_api_send_with_preset_succeeds():
    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    active_id = await _setup_active(active_repo, service._chat_state_repo)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.post(
            f"/api/waiting/items/{active_id}/send",
            json={
                "request_id": "11111111-1111-1111-1111-111111111111",
                "message": "היי, אחזור אליך",
                "send_preset": "1h",
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["outcome"] == "scheduled"
    assert data["scheduled_for"] is not None
    assert data["action_id"] is not None


async def test_api_send_same_request_id_returns_duplicate():
    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    active_id = await _setup_active(active_repo, service._chat_state_repo)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        body = {
            "request_id": "22222222-2222-2222-2222-222222222222",
            "message": "היי",
            "send_preset": "1h",
        }
        resp1 = await client.post(f"/api/waiting/items/{active_id}/send", json=body)
        resp2 = await client.post(f"/api/waiting/items/{active_id}/send", json=body)
    assert resp1.status_code == 200
    assert resp1.json()["outcome"] == "scheduled"
    assert resp2.status_code == 200
    assert resp2.json()["outcome"] == "duplicate"
    assert resp2.json()["action_id"] == resp1.json()["action_id"]


async def test_api_send_invalid_active_id_returns_404():
    app, token_service, _, _ = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.post(
            "/api/waiting/items/nonexistent/send",
            json={
                "request_id": "33333333-3333-3333-3333-333333333333",
                "message": "היי",
                "send_preset": "1h",
            },
        )
    assert resp.status_code == 404
    assert resp.json()["outcome"] == "not_found"


async def test_api_send_without_cookie_returns_401():
    app, _, _, _ = _make_app()
    async with _client(app) as client:
        resp = await client.post(
            "/api/waiting/items/any/send",
            json={
                "request_id": "44444444-4444-4444-4444-444444444444",
                "message": "היי",
                "send_preset": "1h",
            },
        )
    assert resp.status_code == 401


async def test_api_send_empty_message_returns_422():
    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    active_id = await _setup_active(active_repo, service._chat_state_repo)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.post(
            f"/api/waiting/items/{active_id}/send",
            json={
                "request_id": "55555555-5555-5555-5555-555555555555",
                "message": "   ",
                "send_preset": "1h",
            },
        )
    assert resp.status_code == 422


async def test_api_send_both_preset_and_send_at_returns_422():
    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    active_id = await _setup_active(active_repo, service._chat_state_repo)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.post(
            f"/api/waiting/items/{active_id}/send",
            json={
                "request_id": "66666666-6666-6666-6666-666666666666",
                "message": "היי",
                "send_preset": "1h",
                "send_at": "2099-01-01T10:00:00+00:00",
            },
        )
    assert resp.status_code == 422


async def test_api_send_neither_preset_nor_send_at_returns_422():
    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    active_id = await _setup_active(active_repo, service._chat_state_repo)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.post(
            f"/api/waiting/items/{active_id}/send",
            json={
                "request_id": "77777777-7777-7777-7777-777777777777",
                "message": "היי",
            },
        )
    assert resp.status_code == 422


async def test_api_send_naive_datetime_returns_422():
    """A naive datetime (no offset) is rejected by Pydantic."""
    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    active_id = await _setup_active(active_repo, service._chat_state_repo)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.post(
            f"/api/waiting/items/{active_id}/send",
            json={
                "request_id": "88888888-8888-8888-8888-888888888888",
                "message": "היי",
                "send_at": "2099-01-01T10:00:00",  # no offset
            },
        )
    assert resp.status_code == 422


async def test_api_send_with_custom_datetime_succeeds():
    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    active_id = await _setup_active(active_repo, service._chat_state_repo)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.post(
            f"/api/waiting/items/{active_id}/send",
            json={
                "request_id": "99999999-9999-9999-9999-999999999999",
                "message": "הודעה עתידית",
                "send_at": "2099-01-01T10:00:00+00:00",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "scheduled"


async def test_api_send_security_headers_preserved():
    app, token_service, service, active_repo = _make_app()
    _, raw_token = await token_service.issue(USER_ID)
    active_id = await _setup_active(active_repo, service._chat_state_repo)
    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.post(
            f"/api/waiting/items/{active_id}/send",
            json={
                "request_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "message": "היי",
                "send_preset": "1h",
            },
        )
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"
    assert resp.headers.get("referrer-policy") == "no-referrer"
    assert resp.headers.get("x-content-type-options") == "nosniff"
