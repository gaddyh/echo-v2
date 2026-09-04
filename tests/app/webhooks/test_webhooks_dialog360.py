"""Tests for the 360dialog webhook route (auth, dedup, dispatch)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from echo_v2.app.webhooks.dialog360 import build_router
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
