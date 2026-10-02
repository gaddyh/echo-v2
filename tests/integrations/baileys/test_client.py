from __future__ import annotations

import httpx
import pytest

from echo_v2.integrations.baileys.client import BaileysClient
from echo_v2.integrations.baileys.settings import BaileysSettings
from echo_v2.runtime.errors import IndeterminateError, PermanentError, RetryableError


def _client(handler):
    return BaileysClient(
        BaileysSettings("http://connector", "secret"),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_client_calls_connector_with_bearer_and_paths():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/connections":
            return httpx.Response(201, json={"connection_id": "c1", "status": "provisioning"})
        if request.url.path.endswith("/unpair"):
            return httpx.Response(204)
        return httpx.Response(200, json={"status": "connected"})

    client = _client(handler)
    assert (await client.create_connection())["connection_id"] == "c1"
    await client.get_status("c1")
    await client.unpair("c1")
    assert requests[0].headers["authorization"] == "Bearer secret"
    assert [request.url.path for request in requests] == [
        "/connections", "/connections/c1/status", "/connections/c1/unpair"
    ]
    await client.aclose()


@pytest.mark.parametrize("status", [500, 503])
async def test_read_server_error_is_retryable(status: int):
    client = _client(lambda request: httpx.Response(status))
    with pytest.raises(RetryableError):
        await client.get_status("c1")
    await client.aclose()


async def test_create_server_error_is_indeterminate():
    client = _client(lambda request: httpx.Response(500))
    with pytest.raises(IndeterminateError):
        await client.create_connection()
    await client.aclose()


async def test_not_found_is_permanent():
    client = _client(lambda request: httpx.Response(404, json={"error": "missing"}))
    with pytest.raises(PermanentError):
        await client.get_qr("missing")
    await client.aclose()


async def test_transport_read_error_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    client = _client(handler)
    with pytest.raises(RetryableError):
        await client.get_status("c1")
    await client.aclose()
