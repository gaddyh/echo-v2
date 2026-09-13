"""Tests for the 360dialog webhook route (auth, inbox, dispatch)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from echo_v2.app.webhooks.dialog360 import build_router
from echo_v2.app.webhooks.inbox import InMemoryWebhookInbox
from echo_v2.ports.bot import BotEvent, BotEventType

WEBHOOK_SECRET = "test-secret-123"
AUTH_HEADER = {"Authorization": f"Bearer {WEBHOOK_SECRET}"}


class RecordingFlowService:
    """Fake flow service that records handled events."""

    def __init__(self) -> None:
        self.handled: list[BotEvent] = []

    async def handle(self, event: BotEvent) -> None:
        self.handled.append(event)


def _text_payload(
    msg_id: str = "wamid.TEST1",
    body: str = "hello",
    from_phone: str = "972500000001",
) -> dict:
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": msg_id,
                        "from": from_phone,
                        "type": "text",
                        "text": {"body": body},
                        "timestamp": "1700000000",
                    }],
                    "contacts": [
                        {"profile": {"name": "Test"}, "wa_id": from_phone}
                    ],
                }
            }]
        }]
    }


def _contact_payload(msg_id: str = "wamid.CONTACT1") -> dict:
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": msg_id,
                        "from": "972500000001",
                        "type": "contacts",
                        "contacts": [{
                            "name": {"formatted_name": "Dana"},
                            "phones": [{"wa_id": "972526610653"}],
                        }],
                    }],
                    "contacts": [{"wa_id": "972500000001"}],
                }
            }]
        }]
    }


@pytest.fixture
def client():
    flow = RecordingFlowService()
    app = FastAPI()
    app.include_router(build_router(flow_service=flow, webhook_secret=WEBHOOK_SECRET))
    with TestClient(app) as c:
        yield c, flow


def test_missing_auth_returns_401(client):
    c, _ = client
    resp = c.post("/webhooks/bot/dialog360", json=_text_payload())
    assert resp.status_code == 401


def test_wrong_secret_returns_401(client):
    c, _ = client
    resp = c.post(
        "/webhooks/bot/dialog360",
        json=_text_payload(),
        headers={"Authorization": "Bearer wrong-secret"},
    )
    assert resp.status_code == 401


def test_bare_token_accepted(client):
    """360dialog sends the header value as-is — no Bearer prefix."""
    c, flow = client
    resp = c.post(
        "/webhooks/bot/dialog360",
        json=_text_payload(msg_id="wamid.BARE1"),
        headers={"Authorization": WEBHOOK_SECRET},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "received"
    assert len(flow.handled) == 1


def test_text_message_dispatched(client):
    c, flow = client
    resp = c.post("/webhooks/bot/dialog360", json=_text_payload(), headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["status"] == "received"
    assert len(flow.handled) == 1
    assert flow.handled[0].type is BotEventType.TEXT
    assert flow.handled[0].text == "hello"


def test_contact_message_dispatched(client):
    c, flow = client
    resp = c.post("/webhooks/bot/dialog360", json=_contact_payload(), headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["status"] == "received"
    assert len(flow.handled) == 1
    assert flow.handled[0].type is BotEventType.CONTACT
    assert flow.handled[0].contact.phone == "972526610653"


def test_duplicate_message_deduplicated(client):
    c, flow = client
    payload = _text_payload(msg_id="wamid.DEDUP1")
    first = c.post("/webhooks/bot/dialog360", json=payload, headers=AUTH_HEADER)
    assert first.status_code == 200
    assert first.json()["status"] == "received"

    second = c.post("/webhooks/bot/dialog360", json=payload, headers=AUTH_HEADER)
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert len(flow.handled) == 1  # only dispatched once


def test_status_update_ignored(client):
    c, flow = client
    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "statuses": [{"id": "wamid.S1", "status": "delivered"}],
                }
            }]
        }]
    }
    resp = c.post("/webhooks/bot/dialog360", json=payload, headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert len(flow.handled) == 0


def test_empty_body_ignored(client):
    c, _flow = client
    resp = c.post("/webhooks/bot/dialog360", json={}, headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


def test_invalid_json_returns_400(client):
    c, _ = client
    resp = c.post(
        "/webhooks/bot/dialog360",
        content=b"not json",
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 400


# --- no-secret mode — build_router rejects empty secret ------------------


def test_empty_secret_raises_at_build_time():
    """Without a secret the router refuses to build — the server must not
    start with an unauthenticated webhook endpoint."""
    flow = RecordingFlowService()
    with pytest.raises(ValueError, match="D360_WEBHOOK_SECRET"):
        build_router(flow_service=flow, webhook_secret="")


# --- inbox lifecycle (processing → processed/failed → re-claim) ----------


def test_failed_event_is_reclaimed_on_retry():
    """A failed event should be re-claimable on the next provider retry."""
    flow = RecordingFlowService()
    inbox = InMemoryWebhookInbox()
    app = FastAPI()
    app.include_router(
        build_router(flow_service=flow, webhook_secret=WEBHOOK_SECRET, inbox=inbox)
    )
    with TestClient(app, raise_server_exceptions=False) as c:
        # First attempt: dispatch fails.
        call_count = 0

        original_handle = flow.handle

        async def failing_handle(event):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("processing boom")

        flow.handle = failing_handle
        resp = c.post(
            "/webhooks/bot/dialog360",
            json=_text_payload(msg_id="wamid.FAIL1"),
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 500
        assert call_count == 1

        # Second attempt (provider retry): should re-claim and re-dispatch.
        flow.handle = original_handle
        resp = c.post(
            "/webhooks/bot/dialog360",
            json=_text_payload(msg_id="wamid.FAIL1"),
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "received"
        assert call_count == 1  # failing_handle was called once, original once
        assert len(flow.handled) == 1


def test_processed_event_is_not_reclaimed():
    """A processed event should be a permanent duplicate."""
    flow = RecordingFlowService()
    inbox = InMemoryWebhookInbox()
    app = FastAPI()
    app.include_router(
        build_router(flow_service=flow, webhook_secret=WEBHOOK_SECRET, inbox=inbox)
    )
    with TestClient(app) as c:
        payload = _text_payload(msg_id="wamid.PROC1")
        first = c.post("/webhooks/bot/dialog360", json=payload, headers=AUTH_HEADER)
        assert first.status_code == 200
        assert first.json()["status"] == "received"

        second = c.post("/webhooks/bot/dialog360", json=payload, headers=AUTH_HEADER)
        assert second.status_code == 200
        assert second.json()["status"] == "duplicate"
        assert len(flow.handled) == 1


# --- pre-handler dispatch paths ------------------------------------------


class StubHandler:
    """Generic pre-handler stub that records calls and optionally handles."""

    def __init__(self, *, handles: bool = True) -> None:
        self.handled: list[BotEvent] = []
        self._handles = handles

    async def handle(self, event: BotEvent) -> bool:
        self.handled.append(event)
        return self._handles


class StubOnboarding:
    """Stub onboarding service for dispatch tests."""

    def __init__(self, *, is_onboarding: bool = True, user_exists: bool = True) -> None:
        self._is_onboarding = is_onboarding
        self._user_exists = user_exists
        self.unknown_users: list[str] = []
        self.name_responses: list[tuple[str, str]] = []
        self.resend_requests: list[str] = []

    async def is_onboarding(self, phone: str) -> bool:
        return self._is_onboarding

    async def handle_name_response(self, phone: str, text: str) -> bool:
        self.name_responses.append((phone, text))
        return True

    async def handle_resend_request(self, phone: str) -> None:
        self.resend_requests.append(phone)

    async def handle_unknown_user(self, phone: str) -> None:
        self.unknown_users.append(phone)


class StubUserResolver:
    """Stub resolver that returns None for unknown users."""

    def __init__(self, *, user_exists: bool = True) -> None:
        self._user_exists = user_exists

    async def resolve(self, phone: str):
        return "user-123" if self._user_exists else None


def test_feedback_handler_intercepts_dispatch():
    """When feedback_handler handles the event, flow service is skipped."""
    flow = RecordingFlowService()
    feedback = StubHandler(handles=True)
    app = FastAPI()
    app.include_router(
        build_router(
            flow_service=flow,
            webhook_secret=WEBHOOK_SECRET,
            feedback_handler=feedback,
        )
    )
    with TestClient(app) as c:
        resp = c.post("/webhooks/bot/dialog360", json=_text_payload(), headers=AUTH_HEADER)
        assert resp.status_code == 200
        assert resp.json()["status"] == "received"
        assert len(feedback.handled) == 1
        assert len(flow.handled) == 0


def test_feedback_handler_does_not_intercept_when_not_handled():
    """When feedback_handler returns False, the event flows through."""
    flow = RecordingFlowService()
    feedback = StubHandler(handles=False)
    app = FastAPI()
    app.include_router(
        build_router(
            flow_service=flow,
            webhook_secret=WEBHOOK_SECRET,
            feedback_handler=feedback,
        )
    )
    with TestClient(app) as c:
        resp = c.post("/webhooks/bot/dialog360", json=_text_payload(), headers=AUTH_HEADER)
        assert resp.status_code == 200
        assert resp.json()["status"] == "received"
        assert len(feedback.handled) == 1
        assert len(flow.handled) == 1


def test_digest_reply_handler_intercepts_dispatch():
    """When digest_reply_service handles the event, flow service is skipped."""
    flow = RecordingFlowService()
    digest_reply = StubHandler(handles=True)
    app = FastAPI()
    app.include_router(
        build_router(
            flow_service=flow,
            webhook_secret=WEBHOOK_SECRET,
            digest_reply_service=digest_reply,
        )
    )
    with TestClient(app) as c:
        resp = c.post("/webhooks/bot/dialog360", json=_text_payload(), headers=AUTH_HEADER)
        assert resp.status_code == 200
        assert resp.json()["status"] == "received"
        assert len(digest_reply.handled) == 1
        assert len(flow.handled) == 0


def test_onboarding_intercepts_known_onboarding_user():
    """When onboarding_service.is_onboarding returns True, flow is skipped."""
    flow = RecordingFlowService()
    flow._user_resolver = StubUserResolver(user_exists=True)
    onboarding = StubOnboarding(is_onboarding=True)
    app = FastAPI()
    app.include_router(
        build_router(
            flow_service=flow,
            webhook_secret=WEBHOOK_SECRET,
            onboarding_service=onboarding,
        )
    )
    with TestClient(app) as c:
        resp = c.post(
            "/webhooks/bot/dialog360",
            json=_text_payload(msg_id="wamid.ONB1", body="Dana"),
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "received"
        assert len(onboarding.name_responses) == 1
        assert len(flow.handled) == 0


def test_onboarding_routes_unknown_user():
    """When the user is unknown, onboarding.handle_unknown_user is called."""
    flow = RecordingFlowService()
    flow._user_resolver = StubUserResolver(user_exists=False)
    onboarding = StubOnboarding(is_onboarding=False)
    app = FastAPI()
    app.include_router(
        build_router(
            flow_service=flow,
            webhook_secret=WEBHOOK_SECRET,
            onboarding_service=onboarding,
        )
    )
    with TestClient(app) as c:
        resp = c.post(
            "/webhooks/bot/dialog360",
            json=_text_payload(msg_id="wamid.UNK1"),
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "received"
        assert len(onboarding.unknown_users) == 1
        assert len(flow.handled) == 0


def test_onboarding_resend_code_request():
    """When an onboarding user sends 'קוד', the resend path is triggered."""
    flow = RecordingFlowService()
    flow._user_resolver = StubUserResolver(user_exists=True)
    onboarding = StubOnboarding(is_onboarding=True)
    # Make handle_name_response return False so the 'קוד' path is reached.
    onboarding.handle_name_response = lambda phone, text: False  # type: ignore
    # Need async wrapper.
    async def _false_name_response(phone, text):
        onboarding.name_responses.append((phone, text))
        return False
    onboarding.handle_name_response = _false_name_response  # type: ignore

    app = FastAPI()
    app.include_router(
        build_router(
            flow_service=flow,
            webhook_secret=WEBHOOK_SECRET,
            onboarding_service=onboarding,
        )
    )
    with TestClient(app) as c:
        resp = c.post(
            "/webhooks/bot/dialog360",
            json=_text_payload(msg_id="wamid.RESEND1", body="קוד"),
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "received"
        assert len(onboarding.resend_requests) == 1
        assert len(flow.handled) == 0


def test_alt_path_works():
    """The /webhook/360dialog alias also dispatches."""
    flow = RecordingFlowService()
    app = FastAPI()
    app.include_router(build_router(flow_service=flow, webhook_secret=WEBHOOK_SECRET))
    with TestClient(app) as c:
        resp = c.post("/webhook/360dialog", json=_text_payload(), headers=AUTH_HEADER)
        assert resp.status_code == 200
        assert resp.json()["status"] == "received"
        assert len(flow.handled) == 1


def test_non_dict_payload_returns_400():
    """A non-dict JSON payload (e.g. a list) returns 400."""
    flow = RecordingFlowService()
    app = FastAPI()
    app.include_router(build_router(flow_service=flow, webhook_secret=WEBHOOK_SECRET))
    with TestClient(app) as c:
        resp = c.post(
            "/webhooks/bot/dialog360",
            json=["not", "a", "dict"],
            headers=AUTH_HEADER,
        )
        assert resp.status_code == 400
