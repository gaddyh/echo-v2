"""Tests for GreenClient: HTTP calls, error classification, QR WS."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from typing_extensions import Self

from echo_v2.integrations.green.client import (
    GreenApiError,
    GreenApiIndeterminateError,
    GreenApiTransientError,
    GreenClient,
)
from echo_v2.integrations.green.settings import GreenSettings
from echo_v2.runtime.errors import IndeterminateError, PermanentError, RetryableError

_SETTINGS = GreenSettings(
    partner_api_url="https://api.green-api.com", partner_token="partner-tok"
)


def _client_with_handler(handler) -> GreenClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return GreenClient(_SETTINGS, http_client=http)


def _ok(payload: Any) -> httpx.Response:
    return httpx.Response(200, json=payload)


async def test_create_instance_returns_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "createInstance" in request.url.path
        body = json.loads(request.content)
        assert body["webhookUrl"] == "https://echo/hook"
        assert body["webhookUrlToken"] == "tok"
        return _ok({"idInstance": 123, "apiTokenInstance": "api-tok"})

    client = _client_with_handler(handler)
    try:
        data = await client.create_instance(
            {"webhookUrl": "https://echo/hook", "webhookUrlToken": "tok"}
        )
        assert data == {"idInstance": 123, "apiTokenInstance": "api-tok"}
    finally:
        await client.aclose()


async def test_get_state_instance_returns_state_string():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "getStateInstance" in request.url.path
        return _ok({"stateInstance": "authorized"})

    client = _client_with_handler(handler)
    try:
        raw = await client.get_state_instance("123", "api-tok")
        assert raw == "authorized"
    finally:
        await client.aclose()


async def test_get_state_instance_returns_none_when_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok({})

    client = _client_with_handler(handler)
    try:
        assert await client.get_state_instance("123", "api-tok") is None
    finally:
        await client.aclose()


async def test_get_instances_returns_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok([{"idInstance": 1}, {"idInstance": 2}])

    client = _client_with_handler(handler)
    try:
        items = await client.get_instances()
        assert [i["idInstance"] for i in items] == [1, 2]
    finally:
        await client.aclose()


async def test_get_instances_raises_on_non_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok({"not": "a list"})

    client = _client_with_handler(handler)
    try:
        with pytest.raises(GreenApiError):
            await client.get_instances()
    finally:
        await client.aclose()


async def test_delete_instance_calls_partner_endpoint():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return _ok({"ok": True})

    client = _client_with_handler(handler)
    try:
        await client.delete_instance("123")
        assert "deleteInstance" in seen["path"]
        assert "123" in seen["path"]
    finally:
        await client.aclose()


async def test_set_settings_posts_settings():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _ok({"saveSettings": True})

    client = _client_with_handler(handler)
    try:
        await client.set_settings("123", "api-tok", {"incomingWebhook": "yes"})
        assert captured["body"] == {"incomingWebhook": "yes"}
    finally:
        await client.aclose()


async def test_logout_posts_to_logout_endpoint():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return _ok({"logout": True})

    client = _client_with_handler(handler)
    try:
        await client.logout("123", "api-tok")
        assert "Logout" in seen["path"]
    finally:
        await client.aclose()


# --- Error classification (G5) -------------------------------------------


async def test_429_is_retryable_on_read_and_write():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"code": 429})

    client = _client_with_handler(handler)
    try:
        with pytest.raises(GreenApiTransientError):
            await client.get_state_instance("123", "api-tok")
        with pytest.raises(GreenApiTransientError):
            await client.create_instance({})
    finally:
        await client.aclose()


async def test_5xx_on_read_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={})

    client = _client_with_handler(handler)
    try:
        with pytest.raises(GreenApiTransientError):
            await client.get_state_instance("123", "api-tok")
    finally:
        await client.aclose()


async def test_5xx_on_write_is_indeterminate():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={})

    client = _client_with_handler(handler)
    try:
        with pytest.raises(GreenApiIndeterminateError):
            await client.create_instance({})
    finally:
        await client.aclose()


async def test_4xx_is_permanent():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": 400, "description": "bad"})

    client = _client_with_handler(handler)
    try:
        with pytest.raises(GreenApiError):
            await client.get_state_instance("123", "api-tok")
    finally:
        await client.aclose()


async def test_green_error_envelope_on_200_is_permanent():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok({"code": 401, "description": "no auth"})

    client = _client_with_handler(handler)
    try:
        with pytest.raises(GreenApiError):
            await client.get_state_instance("123", "api-tok")
    finally:
        await client.aclose()


async def test_connect_error_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no connection")

    client = _client_with_handler(handler)
    try:
        with pytest.raises(GreenApiTransientError):
            await client.get_state_instance("123", "api-tok")
        # Connect error before write is sent is still retryable (request never left).
        with pytest.raises(GreenApiTransientError):
            await client.create_instance({})
    finally:
        await client.aclose()


async def test_read_timeout_on_read_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    client = _client_with_handler(handler)
    try:
        with pytest.raises(GreenApiTransientError):
            await client.get_state_instance("123", "api-tok")
    finally:
        await client.aclose()


async def test_read_timeout_on_write_is_indeterminate():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    client = _client_with_handler(handler)
    try:
        with pytest.raises(GreenApiIndeterminateError):
            await client.create_instance({})
    finally:
        await client.aclose()


async def test_other_http_error_on_write_is_indeterminate():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.WriteError("disconnected")

    client = _client_with_handler(handler)
    try:
        with pytest.raises(GreenApiIndeterminateError):
            await client.create_instance({})
    finally:
        await client.aclose()


async def test_other_http_error_on_read_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.WriteError("disconnected")

    client = _client_with_handler(handler)
    try:
        with pytest.raises(GreenApiTransientError):
            await client.get_state_instance("123", "api-tok")
    finally:
        await client.aclose()


def test_error_subclasses_match_runtime_taxonomy():
    assert issubclass(GreenApiError, PermanentError)
    assert issubclass(GreenApiTransientError, RetryableError)
    assert issubclass(GreenApiIndeterminateError, IndeterminateError)


# --- QR WebSocket --------------------------------------------------------


class _FakeWS:
    """Minimal async context manager + recv() fake for QR WS testing.

    Doubles as the return value of ``connect()`` and as the async context
    manager itself, so ``async with connect(url) as ws:`` works without an
    intermediate await (matching how ``websockets.connect`` is used as an
    async context manager directly).
    """

    def __init__(self, messages: list[str]) -> None:
        self._messages = list(messages)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def recv(self) -> str:
        if not self._messages:
            raise TimeoutError("no more messages")
        return self._messages.pop(0)


class _FailingWS:
    """Async context manager that raises on __aenter__."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> Self:
        raise self._exc

    async def __aexit__(self, *exc) -> None:
        return None


def _fake_ws_connector(messages: list[str]):
    def connect(url, **kwargs):
        return _FakeWS(messages)

    return connect


def _failing_ws_connector(exc: Exception):
    def connect(url, **kwargs):
        return _FailingWS(exc)

    return connect


def _qr_client(connector) -> GreenClient:
    return GreenClient(
        _SETTINGS,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: _ok({}))),
        ws_connector=connector,
    )


async def test_get_qr_ws_returns_qr_code():
    client = _qr_client(
        _fake_ws_connector([json.dumps({"type": "qrCode", "message": "b64png"})])
    )
    try:
        result = await client.get_qr_ws("123", "api-tok", timeout=5)
        assert result == {"type": "qrCode", "message": "b64png"}
    finally:
        await client.aclose()


async def test_get_qr_ws_handles_already_logged():
    client = _qr_client(_fake_ws_connector([json.dumps({"type": "alreadyLogged"})]))
    try:
        result = await client.get_qr_ws("123", "api-tok", timeout=5)
        assert result["type"] == "alreadyLogged"
    finally:
        await client.aclose()


async def test_get_qr_ws_handles_passkey_required():
    client = _qr_client(
        _fake_ws_connector(
            [json.dumps({"type": "passkeyRequired", "message": "use passkey"})]
        )
    )
    try:
        result = await client.get_qr_ws("123", "api-tok", timeout=5)
        assert result["type"] == "passkeyRequired"
        assert result["message"] == "use passkey"
    finally:
        await client.aclose()


async def test_get_qr_ws_timeout():
    client = _qr_client(_fake_ws_connector([]))  # recv raises TimeoutError
    try:
        result = await client.get_qr_ws("123", "api-tok", timeout=0.05)
        assert result["type"] == "timeout"
    finally:
        await client.aclose()


async def test_get_qr_ws_unknown_event_type_becomes_error():
    client = _qr_client(
        _fake_ws_connector([json.dumps({"type": "somethingNew", "message": "x"})])
    )
    try:
        result = await client.get_qr_ws("123", "api-tok", timeout=5)
        assert result["type"] == "error"
    finally:
        await client.aclose()


async def test_get_qr_ws_malformed_json():
    client = _qr_client(_fake_ws_connector(["not-json"]))
    try:
        result = await client.get_qr_ws("123", "api-tok", timeout=5)
        assert result["type"] == "error"
    finally:
        await client.aclose()


async def test_get_qr_ws_connection_failure_surfaces_error():
    client = _qr_client(_failing_ws_connector(OSError("connection refused")))
    try:
        result = await client.get_qr_ws("123", "api-tok", timeout=5)
        assert result["type"] == "error"
    finally:
        await client.aclose()


# --- Settings ------------------------------------------------------------


def test_load_settings_requires_partner_token(monkeypatch):
    from echo_v2.integrations.green.settings import load_settings

    monkeypatch.delenv("GREEN_API_PARTNER_TOKEN", raising=False)
    with pytest.raises(ValueError, match="GREEN_API_PARTNER_TOKEN"):
        load_settings()


def test_load_settings_uses_env(monkeypatch):
    from echo_v2.integrations.green.settings import load_settings

    monkeypatch.setenv("GREEN_API_PARTNER_TOKEN", "env-tok")
    monkeypatch.delenv("GREEN_API_PARTNER_URL", raising=False)
    settings = load_settings()
    assert settings.partner_token == "env-tok"
    assert settings.partner_api_url == "https://api.green-api.com"


def test_load_settings_strips_trailing_slash(monkeypatch):
    from echo_v2.integrations.green.settings import load_settings

    settings = load_settings(
        partner_api_url="https://api.green-api.com/", partner_token="t"
    )
    assert settings.partner_api_url == "https://api.green-api.com"


# --- Additional coverage: aclose, 4xx without code, invalid JSON, connect timeout ---


async def test_aclose_closes_owned_client():
    client = GreenClient(_SETTINGS)  # owns its client
    await client.aclose()  # should not raise


async def test_aclose_does_not_close_injected_client():
    closed = {"yes": False}

    class _TrackingClient(httpx.AsyncClient):
        async def aclose(self) -> None:
            closed["yes"] = True
            await super().aclose()

    injected = _TrackingClient(transport=httpx.MockTransport(lambda r: _ok({})))
    client = GreenClient(_SETTINGS, http_client=injected)
    await client.aclose()
    assert closed["yes"] is False  # client does not close injected client
    await injected.aclose()


async def test_4xx_without_code_body_is_permanent():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"forbidden")  # non-JSON body

    client = _client_with_handler(handler)
    try:
        with pytest.raises(GreenApiError, match="HTTP 403"):
            await client.get_state_instance("123", "api-tok")
    finally:
        await client.aclose()


async def test_invalid_json_response_body_handled():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    client = _client_with_handler(handler)
    try:
        # 200 with invalid JSON -> _safe_json returns None -> {} -> stateInstance missing -> None.
        data = await client.get_state_instance("123", "api-tok")
        assert data is None
    finally:
        await client.aclose()


async def test_get_qr_ws_connect_timeout_surfaces_timeout():
    # The outer TimeoutError path (connect timeout, distinct from recv timeout).
    class _ConnectTimeoutWS:
        async def __aenter__(self):
            raise TimeoutError("connect timed out")

        async def __aexit__(self, *exc):
            return None

    def connect(url, **kwargs):
        return _ConnectTimeoutWS()

    client = GreenClient(
        _SETTINGS,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: _ok({}))),
        ws_connector=connect,
    )
    try:
        result = await client.get_qr_ws("123", "api-tok", timeout=5)
        assert result["type"] == "timeout"
        assert "connect" in result["message"]
    finally:
        await client.aclose()


async def test_ws_url_converts_https_to_wss():
    client = GreenClient(
        _SETTINGS,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: _ok({}))),
    )
    try:
        url = client._ws_qr_url("123", "tok")
        assert url.startswith("wss://")
        assert "waInstance123" in url
        assert "scanqrcode/tok" in url
    finally:
        await client.aclose()


async def test_ws_url_converts_http_to_ws():
    http_settings = GreenSettings(
        partner_api_url="http://localhost:8080", partner_token="t"
    )
    client = GreenClient(
        http_settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: _ok({}))),
    )
    try:
        url = client._ws_qr_url("123", "tok")
        assert url.startswith("ws://")
    finally:
        await client.aclose()
