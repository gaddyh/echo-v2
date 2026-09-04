"""Tests for the 360dialog client (send_text + error classification)."""

from __future__ import annotations

import httpx
import pytest

from echo_v2.integrations.dialog360.client import Dialog360Client
from echo_v2.integrations.dialog360.settings import Dialog360Settings
from echo_v2.runtime.errors import (
    IndeterminateError,
    PermanentError,
    RetryableError,
)


def _make_client(
    handler: httpx.MockTransport,
) -> Dialog360Client:
    settings = Dialog360Settings()
    settings.api_key = "test-key"
    settings.api_base_url = "https://waba-v2.360dialog.io"
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    return Dialog360Client(settings=settings, http_client=http_client)


async def test_send_text_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/messages"
        assert request.headers["D360-API-KEY"] == "test-key"
        return httpx.Response(
            200,
            json={"messages": [{"id": "wamid.SENT123"}]},
        )

    client = _make_client(handler)
    msg_id = await client.send_text("972500000001", "hello")
    assert msg_id == "wamid.SENT123"
    await client.aclose()


async def test_send_text_normalizes_recipient():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        body = json.loads(request.content)
        captured["to"] = body["to"]
        return httpx.Response(200, json={"messages": [{"id": "m1"}]})

    client = _make_client(handler)
    await client.send_text("+972 50-000-0002", "hi")
    assert captured["to"] == "972500000002"
    await client.aclose()


async def test_send_text_5xx_is_indeterminate():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    client = _make_client(handler)
    with pytest.raises(IndeterminateError):
        await client.send_text("972500000001", "hello")
    await client.aclose()


async def test_send_text_4xx_is_permanent():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad request"})

    client = _make_client(handler)
    with pytest.raises(PermanentError):
        await client.send_text("972500000001", "hello")
    await client.aclose()


async def test_send_text_429_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    client = _make_client(handler)
    with pytest.raises(RetryableError):
        await client.send_text("972500000001", "hello")
    await client.aclose()


async def test_send_text_timeout_is_indeterminate():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    client = _make_client(handler)
    with pytest.raises(IndeterminateError):
        await client.send_text("972500000001", "hello")
    await client.aclose()


async def test_send_text_connect_error_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = _make_client(handler)
    with pytest.raises(RetryableError):
        await client.send_text("972500000001", "hello")
    await client.aclose()
