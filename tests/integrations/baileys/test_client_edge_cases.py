from __future__ import annotations

import httpx
import pytest

from echo_v2.integrations.baileys.client import BaileysClient, BaileysConnectorError
from echo_v2.integrations.baileys.settings import BaileysSettings
from echo_v2.runtime.errors import IndeterminateError, RetryableError


async def test_client_optional_paths_and_owned_close() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"connection_id": "c1"})

    client = BaileysClient(BaileysSettings("http://connector", "secret"), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await client.create_connection("+972500000001")
    await client.delete_connection("c1")
    await client.aclose()
    assert requests[0].read() == b'{"phone_number":"+972500000001"}'

    owned = BaileysClient(BaileysSettings("http://connector", "secret"))
    await owned.aclose()


@pytest.mark.asyncio
async def test_write_transport_error_is_indeterminate() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    client = BaileysClient(BaileysSettings("http://connector", "secret"), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(IndeterminateError):
        await client.create_connection()
    await client.aclose()


@pytest.mark.asyncio
async def test_http_error_invalid_json_and_non_object_are_rejected() -> None:
    responses = iter([
        httpx.Response(400),
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json=["not-object"]),
    ])
    client = BaileysClient(
        BaileysSettings("http://connector", "secret"),
        httpx.AsyncClient(transport=httpx.MockTransport(lambda request: next(responses))),
    )
    with pytest.raises(BaileysConnectorError):
        await client.get_status("c1")
    with pytest.raises(BaileysConnectorError):
        await client.get_status("c1")
    with pytest.raises(BaileysConnectorError):
        await client.get_status("c1")
    await client.aclose()


@pytest.mark.asyncio
async def test_generic_http_error_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ProtocolError("protocol", request=request)

    client = BaileysClient(BaileysSettings("http://connector", "secret"), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(RetryableError):
        await client.get_status("c1")
    await client.aclose()
