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


async def test_send_template_success():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        body = json.loads(request.content)
        captured["type"] = body["type"]
        captured["template"] = body["template"]
        return httpx.Response(200, json={"messages": [{"id": "wamid.TPL123"}]})

    client = _make_client(handler)
    msg_id = await client.send_template(
        "972500000001",
        "morning_waiting_digest",
        "he",
        ["גדי", "2", "דנה: \"יש מצב?\""],
    )
    assert msg_id == "wamid.TPL123"
    assert captured["type"] == "template"
    assert captured["template"]["name"] == "morning_waiting_digest"
    assert captured["template"]["language"]["code"] == "he"
    params = captured["template"]["components"][0]["parameters"]
    assert params[0]["text"] == "גדי"
    assert params[1]["text"] == "2"
    assert params[2]["text"] == "דנה: \"יש מצב?\""
    await client.aclose()


async def test_send_template_5xx_is_indeterminate():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    client = _make_client(handler)
    with pytest.raises(IndeterminateError):
        await client.send_template("972500000001", "tpl", "he", ["a"])
    await client.aclose()


# --- Edge cases ---


async def test_send_template_with_url_suffix():
    """send_template with url_suffix adds a button component."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        body = json.loads(request.content)
        captured["components"] = body["template"]["components"]
        return httpx.Response(200, json={"messages": [{"id": "m1"}]})

    client = _make_client(handler)
    msg_id = await client.send_template(
        "972500000001",
        "morning_waiting_digest6",
        "he",
        ["גדי", "3"],
        url_suffix="abc-token-123",
    )
    assert msg_id == "m1"
    # Should have body + button components.
    assert len(captured["components"]) == 2
    assert captured["components"][0]["type"] == "body"
    assert captured["components"][1]["type"] == "button"
    assert captured["components"][1]["sub_type"] == "url"
    assert captured["components"][1]["parameters"][0]["text"] == "abc-token-123"
    await client.aclose()


async def test_send_text_message_id_from_top_level():
    """send_text extracts message_id from top-level field."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message_id": "wamid.TOP"})

    client = _make_client(handler)
    msg_id = await client.send_text("972500000001", "hello")
    assert msg_id == "wamid.TOP"
    await client.aclose()


async def test_send_text_missing_message_id_raises_permanent():
    """send_text with no message_id in response raises PermanentError."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = _make_client(handler)
    with pytest.raises(PermanentError):
        await client.send_text("972500000001", "hello")
    await client.aclose()


async def test_send_template_missing_message_id_raises_permanent():
    """send_template with no message_id raises PermanentError."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = _make_client(handler)
    with pytest.raises(PermanentError):
        await client.send_template("972500000001", "tpl", "he", ["a"])
    await client.aclose()


async def test_send_text_http_error_is_indeterminate():
    """Generic httpx.HTTPError is classified as IndeterminateError."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.HTTPError("generic")

    client = _make_client(handler)
    with pytest.raises(IndeterminateError):
        await client.send_text("972500000001", "hello")
    await client.aclose()


async def test_send_interactive_list_success():
    """send_interactive_list sends and returns message id."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": [{"id": "list-1"}]})

    client = _make_client(handler)
    msg_id = await client.send_interactive_list(
        "972500000001",
        body_text="Choose:",
        button_text="Options",
        sections=[{"title": "Section", "rows": [{"id": "r1", "title": "Row 1"}]}],
    )
    assert msg_id == "list-1"
    await client.aclose()


async def test_send_buttons_success():
    """send_buttons sends and returns message id."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": [{"id": "btn-1"}]})

    client = _make_client(handler)
    msg_id = await client.send_buttons(
        "972500000001",
        body_text="Choose:",
        buttons=[{"id": "b1", "title": "Yes"}, {"id": "b2", "title": "No"}],
    )
    assert msg_id == "btn-1"
    await client.aclose()


async def test_aclose_owned_client():
    """aclose closes the client when it was created internally."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": [{"id": "m1"}]})

    client = _make_client(handler)
    await client.aclose()
    # Calling aclose again should not raise (client already closed).


async def test_safe_json_invalid_response():
    """_safe_json returns None for invalid JSON."""
    from echo_v2.integrations.dialog360.client import _safe_json

    class FakeResponse:
        def json(self):
            raise ValueError("not JSON")

    result = _safe_json(FakeResponse())  # type: ignore[arg-type]
    assert result is None


async def test_extract_msg_id_from_messages_list():
    """_extract_msg_id extracts from messages[0].id."""
    from echo_v2.integrations.dialog360.client import _extract_msg_id

    data = {"messages": [{"id": "wamid.LIST"}]}
    assert _extract_msg_id(data, "test") == "wamid.LIST"


async def test_extract_msg_id_from_top_level():
    """_extract_msg_id extracts from top-level message_id."""
    from echo_v2.integrations.dialog360.client import _extract_msg_id

    data = {"message_id": "wamid.TOP"}
    assert _extract_msg_id(data, "test") == "wamid.TOP"


async def test_extract_msg_id_missing_raises():
    """_extract_msg_id raises PermanentError when no id found."""
    from echo_v2.integrations.dialog360.client import _extract_msg_id

    with pytest.raises(PermanentError):
        _extract_msg_id({}, "test")


async def test_extract_msg_id_empty_messages_list_raises():
    """_extract_msg_id raises when messages list is empty."""
    from echo_v2.integrations.dialog360.client import _extract_msg_id

    with pytest.raises(PermanentError):
        _extract_msg_id({"messages": []}, "test")


async def test_normalize_phone():
    """_normalize_phone strips formatting."""
    from echo_v2.integrations.dialog360.client import _normalize_phone

    assert _normalize_phone("+972 50-000-0001") == "972500000001"
    assert _normalize_phone("972500000001@c.us") == "972500000001"
    assert _normalize_phone("  972500000001  ") == "972500000001"
