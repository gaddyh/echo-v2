from __future__ import annotations

import httpx
import pytest

from echo_v2.domain.chat import Message
from echo_v2.integrations.baileys.client import BaileysClient, BaileysMediaUrlResolver
from echo_v2.integrations.baileys.settings import BaileysSettings
from echo_v2.ports.whatsapp import MessageDirection
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


async def test_send_message_posts_text_and_returns_provider_id() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"provider_message_id": "baileys-msg-1"})

    client = _client(handler)
    assert (
        await client.send_message("c1", "15551234567@s.whatsapp.net", "hello")
        == "baileys-msg-1"
    )
    assert requests[0].url.path == "/connections/c1/messages"
    assert requests[0].read() == (
        b'{"chat_id":"15551234567@s.whatsapp.net","message":"hello"}'
    )
    await client.aclose()


async def test_send_message_rejects_missing_provider_id() -> None:
    client = _client(lambda request: httpx.Response(200, json={}))
    with pytest.raises(PermanentError, match="provider_message_id"):
        await client.send_message("c1", "chat", "hello")
    await client.aclose()


async def test_send_message_server_error_is_indeterminate() -> None:
    client = _client(lambda request: httpx.Response(500))
    with pytest.raises(IndeterminateError):
        await client.send_message("c1", "chat", "hello")
    await client.aclose()


async def test_get_media_url_and_resolver_use_reference() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"media_download_url": "https://media.test/signed"})

    client = _client(handler)
    reference = "baileys:c1:message-1"
    assert await client.get_media_url("c1", reference) == "https://media.test/signed"
    message = Message(
        id="id",
        user_id="user",
        connection_id="db-connection",
        chat_id="chat",
        provider_message_id="message-1",
        direction=MessageDirection.INBOUND,
        sender_id=None,
        media_reference=reference,
    )
    assert await BaileysMediaUrlResolver(client).resolve(message) == "https://media.test/signed"
    assert requests[0].url.params["reference"] == reference
    await client.aclose()


async def test_media_url_resolver_returns_none_without_reference() -> None:
    client = _client(lambda request: httpx.Response(200, json={}))
    message = Message(
        id="id",
        user_id="user",
        connection_id="db-connection",
        chat_id="chat",
        provider_message_id="message-1",
        direction=MessageDirection.INBOUND,
        sender_id=None,
    )
    assert await BaileysMediaUrlResolver(client).resolve(message) is None
    await client.aclose()


async def test_media_url_resolver_rejects_missing_url() -> None:
    client = _client(lambda request: httpx.Response(200, json={}))
    message = Message(
        id="id",
        user_id="user",
        connection_id="db-connection",
        chat_id="chat",
        provider_message_id="message-1",
        direction=MessageDirection.INBOUND,
        sender_id=None,
        media_reference="baileys:c1:message-1",
    )
    with pytest.raises(PermanentError, match="empty media URL"):
        await BaileysMediaUrlResolver(client).resolve(message)
    await client.aclose()


async def test_media_url_resolver_rejects_invalid_reference() -> None:
    client = _client(lambda request: httpx.Response(200, json={}))
    message = Message(
        id="id",
        user_id="user",
        connection_id="db-connection",
        chat_id="chat",
        provider_message_id="message-1",
        direction=MessageDirection.INBOUND,
        sender_id=None,
        media_reference="invalid",
    )
    with pytest.raises(PermanentError, match="media reference"):
        await BaileysMediaUrlResolver(client).resolve(message)
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
