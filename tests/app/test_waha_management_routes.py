"""Route tests for the WAHA agent management endpoints (commands + events)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from echo_v2.app.waha_management_routes import build_waha_management_router
from echo_v2.integrations.waha.settings import WahaSettings
from echo_v2.services.waha_session_service import (
    WahaCallbackBadRequestError,
    WahaCallbackForbiddenError,
    WahaCallbackRetryError,
    WahaSessionService,
)

_TOKEN = "wt_waha-1_secretsecretsecret"
_SETTINGS = WahaSettings(
    onboarding_enabled=True,
    heartbeat_timeout_seconds=90.0,
    command_lease_seconds=120.0,
    command_max_attempts=5,
    command_poll_limit=10,
    qr_ttl_seconds=120.0,
)


class FakeRegistry:
    """Worker registry fake: only ``wt_waha-1_secret…`` authenticates."""

    async def verify_worker_token(self, authorization: str | None) -> str | None:
        if authorization == f"Bearer {_TOKEN}":
            return "waha-1"
        return None


class FakeService(WahaSessionService):
    """Service with canned behaviours for route-level testing."""

    def __init__(
        self,
        *,
        commands: list[dict[str, str]] | None = None,
        outcome: str = "ok",
        error: Exception | None = None,
    ) -> None:
        super().__init__(sessions=_NullRepo(), settings=_SETTINGS)
        self._commands = commands or []
        self._outcome = outcome
        self._error = error
        self.events: list[dict[str, Any]] = []
        self.claimed_workers: list[str] = []

    async def claim_commands(self, worker_id: str) -> list[dict[str, str]]:
        self.claimed_workers.append(worker_id)
        return self._commands

    async def handle_agent_event(
        self, payload: dict[str, Any], *, worker_id: str
    ) -> str:
        self.events.append(payload)
        if self._error is not None:
            raise self._error
        return self._outcome


class _NullRepo:
    """Never called in these tests (all service methods are overridden)."""


def _client(service: WahaSessionService) -> TestClient:
    app = FastAPI()
    app.include_router(
        build_waha_management_router(
            workers=FakeRegistry(),  # type: ignore[arg-type]
            service=service,
        )
    )
    return TestClient(app)


def _auth(token: str | None = _TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def test_commands_poll_returns_claims_for_authenticated_worker() -> None:
    service = FakeService(
        commands=[
            {
                "command_id": "c1",
                "action": "create_and_start",
                "session": "echo-1",
            }
        ]
    )
    client = _client(service)
    response = client.post(
        "/internal/waha/commands",
        json={"worker_id": "waha-1"},
        headers=_auth(),
    )
    assert response.status_code == 200
    assert response.json()["commands"] == [
        {"command_id": "c1", "action": "create_and_start", "session": "echo-1"}
    ]
    assert service.claimed_workers == ["waha-1"]


def test_commands_poll_empty_list() -> None:
    client = _client(FakeService())
    response = client.post(
        "/internal/waha/commands", json={}, headers=_auth()
    )
    assert response.status_code == 200
    assert response.json()["commands"] == []


def test_commands_poll_rejects_bad_token() -> None:
    client = _client(FakeService())
    response = client.post(
        "/internal/waha/commands",
        json={},
        headers=_auth(token="wt_waha-1_wrong"),
    )
    assert response.status_code == 401
    response = client.post("/internal/waha/commands", json={})
    assert response.status_code == 401


def test_commands_poll_rejects_mismatched_body_worker() -> None:
    client = _client(FakeService())
    response = client.post(
        "/internal/waha/commands",
        json={"worker_id": "waha-other"},
        headers=_auth(),
    )
    assert response.status_code == 403


def test_commands_poll_rejects_malformed_body() -> None:
    client = _client(FakeService())
    response = client.post(
        "/internal/waha/commands",
        content=b"not-json",
        headers={**_auth(), "Content-Type": "application/json"},
    )
    assert response.status_code == 400


def test_commands_poll_security_headers() -> None:
    client = _client(FakeService())
    response = client.post(
        "/internal/waha/commands", json={}, headers=_auth()
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def _event(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "worker_id": "waha-1",
        "session": "echo-1",
        "type": "session_status",
        "status": "WORKING",
        "timestamp": 1795718136.42,
    }
    payload.update(overrides)
    return payload


def test_event_accepted_returns_200() -> None:
    service = FakeService(outcome="ok")
    client = _client(service)
    event = _event()
    response = client.post(
        "/internal/waha/events", json=event, headers=_auth()
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert service.events == [event]


def test_event_unknown_session_returns_202() -> None:
    client = _client(FakeService(outcome="unknown_session"))
    response = client.post(
        "/internal/waha/events", json=_event(), headers=_auth()
    )
    assert response.status_code == 202


def test_event_ignored_event_returns_202() -> None:
    client = _client(FakeService(outcome="ignored_event"))
    response = client.post(
        "/internal/waha/events", json=_event(), headers=_auth()
    )
    assert response.status_code == 202


def test_event_rejects_bad_token() -> None:
    client = _client(FakeService())
    response = client.post(
        "/internal/waha/events",
        json=_event(),
        headers=_auth(token="garbage"),
    )
    assert response.status_code == 401
    assert client.post("/internal/waha/events", json=_event()).status_code == 401


def test_event_forbidden_returns_403() -> None:
    client = _client(
        FakeService(error=WahaCallbackForbiddenError("foreign session"))
    )
    response = client.post(
        "/internal/waha/events", json=_event(), headers=_auth()
    )
    assert response.status_code == 403


def test_event_malformed_returns_400() -> None:
    client = _client(
        FakeService(error=WahaCallbackBadRequestError("invalid status"))
    )
    response = client.post(
        "/internal/waha/events", json=_event(), headers=_auth()
    )
    assert response.status_code == 400
    # Malformed JSON at the route level too.
    response = client.post(
        "/internal/waha/events",
        content=b"not json",
        headers={**_auth(), "Content-Type": "application/json"},
    )
    assert response.status_code == 400


def test_event_retryable_returns_503() -> None:
    client = _client(
        FakeService(error=WahaCallbackRetryError("QR delivery failed"))
    )
    response = client.post(
        "/internal/waha/events", json=_event(), headers=_auth()
    )
    assert response.status_code == 503


def test_event_oversized_body_rejected() -> None:
    client = _client(FakeService())
    big = _event()
    big["padding"] = "x" * (600 * 1024)
    response = client.post(
        "/internal/waha/events", json=big, headers=_auth()
    )
    assert response.status_code == 400


def test_event_non_object_payload_rejected() -> None:
    client = _client(FakeService())
    response = client.post(
        "/internal/waha/events", json=[1, 2, 3], headers=_auth()
    )
    assert response.status_code == 400


def test_commands_poll_large_body_rejected() -> None:
    client = _client(FakeService())
    response = client.post(
        "/internal/waha/commands",
        json={"worker_id": "x" * 10000},
        headers=_auth(),
    )
    assert response.status_code == 400


def test_wire_shape_matches_contract() -> None:
    """The commands response carries exactly the documented fields."""
    service = FakeService(
        commands=[{"command_id": "c1", "action": "get_qr", "session": "echo-9"}]
    )
    client = _client(service)
    body = client.post(
        "/internal/waha/commands", json={}, headers=_auth()
    ).json()
    assert set(body) == {"commands"}
    assert set(body["commands"][0]) == {"command_id", "action", "session"}


def test_commands_poll_db_error_returns_503() -> None:
    class _DbErrorService(FakeService):
        async def claim_commands(self, worker_id: str) -> list[dict[str, str]]:
            from sqlalchemy.exc import SQLAlchemyError

            raise SQLAlchemyError("db down")

    client = _client(_DbErrorService())
    response = client.post(
        "/internal/waha/commands", json={}, headers=_auth()
    )
    assert response.status_code == 503


def test_event_db_error_returns_503() -> None:
    class _DbErrorService(FakeService):
        async def handle_agent_event(self, payload, *, worker_id: str) -> str:
            from sqlalchemy.exc import SQLAlchemyError

            raise SQLAlchemyError("db down")

    client = _client(_DbErrorService())
    response = client.post(
        "/internal/waha/events", json=_event(), headers=_auth()
    )
    assert response.status_code == 503


def test_commands_poll_invalid_content_length_rejected() -> None:
    client = _client(FakeService())
    response = client.post(
        "/internal/waha/commands",
        content=b"{}",
        headers={**_auth(), "Content-Length": "not-a-number"},
    )
    assert response.status_code == 400
