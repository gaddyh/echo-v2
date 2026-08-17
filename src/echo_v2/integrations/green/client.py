"""Raw async HTTP/WebSocket client for Green API.

This is the **only** module that knows Green URL shapes and embeds Green
credentials (partner token, per-instance ``apiTokenInstance``) in URL paths.
Because of that:

* Full request URLs are **never** logged (plan guardrail G4). Log lines carry
  only ``provider=green operation=<name> connection_id=<id> status_code=<code>``.
* Credentials are never put in ``RuntimeEvent`` attributes or exception
  messages.

Error classification is operation-aware (plan guardrail G5): a transport
failure on an irreversible write (``create_instance``/``delete_instance``/
``logout``/``set_settings``) is :class:`IndeterminateError`, while the same
failure on a read is :class:`RetryableError`. This is what lets the runtime
honor "unknown external write outcome -> never blindly retry" for provisioning
as well as for sends.

The client takes a shared/injected ``httpx.AsyncClient`` (plan guardrail G6)
for connection pooling and trivial ``MockTransport``-based testing.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import websockets

from echo_v2.integrations.green.settings import GreenSettings
from echo_v2.runtime.errors import (
    IndeterminateError,
    PermanentError,
    RetryableError,
)

__all__ = [
    "GreenApiError",
    "GreenApiIndeterminateError",
    "GreenApiTransientError",
    "GreenClient",
]

# Type alias for the WebSocket connector factory, injectable for tests.
WSConnector = Callable[..., Awaitable[Any]]

_logger = logging.getLogger("echo_v2.green.client")


# --- Errors ---------------------------------------------------------------


class GreenApiError(PermanentError):
    """Permanent Green API failure (documented non-transient error code)."""


class GreenApiTransientError(RetryableError):
    """Transient Green API failure safe to retry (on reads)."""


class GreenApiIndeterminateError(IndeterminateError):
    """Irreversible write whose outcome is unknown (timeout/5xx/disconnect)."""


# --- Client ---------------------------------------------------------------


class GreenClient:
    """Raw Green API HTTP + WebSocket client.

    All methods take explicit ``id_instance`` and ``api_token`` strings --
    this is the one place those Green-native names live. Callers (the
    provisioner) decode them from :class:`ConnectionRef` and
    :class:`ProviderCredentials`.
    """

    def __init__(
        self,
        settings: GreenSettings,
        http_client: httpx.AsyncClient | None = None,
        ws_connector: WSConnector | None = None,
    ) -> None:
        self._settings = settings
        # Default client owns its lifecycle; an injected one is closed by the
        # caller. Tests inject a client with MockTransport.
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
        )
        # WebSocket connector factory; defaults to ``websockets.connect``.
        # Injectable so tests can fake the WS without monkeypatching.
        self._ws_connector = ws_connector or websockets.connect

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- partner endpoints -------------------------------------------------

    async def create_instance(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Partner ``createInstance``. Irreversible write."""
        url = f"{self._settings.partner_api_url}/partner/createInstance/{self._settings.partner_token}"
        return await self._request_json(
            "POST",
            url,
            operation="create_instance",
            connection_id=None,
            is_write=True,
            json_body=payload,
        )

    async def get_instances(self) -> list[dict[str, Any]]:
        """Partner ``getInstances``. Read."""
        url = f"{self._settings.partner_api_url}/partner/getInstances/{self._settings.partner_token}"
        data = await self._request_json(
            "GET",
            url,
            operation="get_instances",
            connection_id=None,
            is_write=False,
        )
        if isinstance(data, list):
            return data
        # Green sometimes returns an error object instead of a list.
        raise GreenApiError("getInstances returned non-list response")

    async def delete_instance(self, instance_id: str) -> None:
        """Partner ``deleteInstance``. Irreversible write."""
        url = (
            f"{self._settings.partner_api_url}/partner/deleteInstance/"
            f"{self._settings.partner_token}/{instance_id}"
        )
        await self._request_json(
            "POST",
            url,
            operation="delete_instance",
            connection_id=instance_id,
            is_write=True,
        )

    # -- per-instance endpoints -------------------------------------------

    async def set_settings(
        self,
        id_instance: str,
        api_token: str,
        settings: dict[str, Any],
    ) -> None:
        """Per-instance ``SetSettings``. Write."""
        url = f"{self._settings.partner_api_url}/waInstance{id_instance}/SetSettings/{api_token}"
        await self._request_json(
            "POST",
            url,
            operation="set_settings",
            connection_id=id_instance,
            is_write=True,
            json_body=settings,
        )

    async def get_state_instance(
        self,
        id_instance: str,
        api_token: str,
    ) -> str | None:
        """Per-instance ``getStateInstance``. Read. Returns the raw
        ``stateInstance`` string (or ``None``)."""
        url = f"{self._settings.partner_api_url}/waInstance{id_instance}/getStateInstance/{api_token}"
        data = await self._request_json(
            "GET",
            url,
            operation="get_state",
            connection_id=id_instance,
            is_write=False,
        )
        return data.get("stateInstance") if isinstance(data, dict) else None

    async def logout(self, id_instance: str, api_token: str) -> None:
        """Per-instance ``Logout``. Irreversible write (unpairs phone)."""
        url = f"{self._settings.partner_api_url}/waInstance{id_instance}/Logout/{api_token}"
        await self._request_json(
            "POST",
            url,
            operation="logout",
            connection_id=id_instance,
            is_write=True,
        )

    # -- QR (WebSocket) ---------------------------------------------------

    async def get_qr_ws(
        self,
        id_instance: str,
        api_token: str,
        *,
        timeout: float = 100.0,
    ) -> dict[str, Any]:
        """Connect to Green's WebSocket QR endpoint and return the first
        meaningful event as ``{"type": ..., "message": ...}``.

        Recognized ``type`` values: ``qrCode``, ``alreadyLogged``,
        ``passkeyRequired``, ``timeout``, ``error``. The WS URL is built here
        and never logged.

        Implementation note: this method imports ``websockets`` lazily so the
        rest of the client remains importable without it (e.g. for HTTP-only
        tests). The actual WS transport is injected via ``ws_factory`` for
        testability.
        """
        ws_url = self._ws_qr_url(id_instance, api_token)
        return await self._read_qr_ws(
            ws_url, timeout=timeout, connection_id=id_instance
        )

    def _ws_qr_url(self, id_instance: str, api_token: str) -> str:
        base = self._settings.partner_api_url.replace("https://", "wss://").replace(
            "http://", "ws://"
        )
        return f"{base}/waInstance{id_instance}/scanqrcode/{api_token}"

    async def _read_qr_ws(
        self,
        ws_url: str,
        *,
        timeout: float,
        connection_id: str | None,
    ) -> dict[str, Any]:
        import asyncio

        try:
            async with self._ws_connector(ws_url, open_timeout=10) as ws:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except TimeoutError:
                    return {"type": "timeout", "message": "QR scan timed out"}
                try:
                    evt = json.loads(raw)
                except (TypeError, ValueError):
                    return {"type": "error", "message": "malformed QR event"}
                etype = evt.get("type")
                if etype in (
                    "qrCode",
                    "alreadyLogged",
                    "passkeyRequired",
                    "timeout",
                    "error",
                ):
                    return {"type": etype, "message": evt.get("message")}
                return {"type": "error", "message": f"unknown QR event type {etype!r}"}
        except TimeoutError:
            return {"type": "timeout", "message": "QR connect timed out"}
        except Exception as exc:  # noqa: BLE001 - WS failures are surfaced as outcomes
            _logger.info(
                "provider=green operation=get_qr connection_id=%s status=ws_error error_type=%s",
                connection_id,
                type(exc).__name__,
            )
            return {"type": "error", "message": "websocket connection failed"}

    # -- shared request helper --------------------------------------------

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        connection_id: str | None,
        is_write: bool,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Execute an HTTP request and return parsed JSON.

        Classifies transport/HTTP errors per plan guardrail G5:
        * connect errors and 429 -> RetryableError (read or write)
        * 5xx and post-transmission timeouts/disconnects ->
          RetryableError on reads, IndeterminateError on writes
        * 4xx (non-429) -> PermanentError
        * Green error body ``{"code":..., "description":...}`` -> PermanentError
        """
        try:
            response = await self._client.request(method, url, json=json_body)
        except httpx.ConnectError as exc:
            _logger.info(
                "provider=green operation=%s connection_id=%s status=connect_error",
                operation,
                connection_id,
            )
            raise GreenApiTransientError(f"connect error during {operation}") from exc
        except httpx.ReadTimeout as exc:
            kind = _classify_timeout(is_write)
            _logger.info(
                "provider=green operation=%s connection_id=%s status=read_timeout",
                operation,
                connection_id,
            )
            raise kind(f"read timeout during {operation}") from exc
        except httpx.HTTPError as exc:
            # Other transport errors (write timeout, remote protocol, etc.).
            kind = _classify_transport(is_write)
            _logger.info(
                "provider=green operation=%s connection_id=%s status=transport_error error_type=%s",
                operation,
                connection_id,
                type(exc).__name__,
            )
            raise kind(
                f"transport error during {operation}: {type(exc).__name__}"
            ) from exc

        status = response.status_code
        _logger.info(
            "provider=green operation=%s connection_id=%s status_code=%s",
            operation,
            connection_id,
            status,
        )

        if status == 429:
            raise GreenApiTransientError(f"rate limited during {operation}")
        if 500 <= status < 600:
            kind = _classify_http_5xx(is_write)
            raise kind(f"green {operation} returned HTTP {status}")
        if 400 <= status < 500:
            # 4xx other than 429 is permanent. Green error body, if present,
            # is surfaced via the message but never includes the URL/token.
            body = _safe_json(response)
            if isinstance(body, dict) and body.get("code"):
                raise GreenApiError(
                    f"green {operation} error {body.get('code')}: {body.get('description')}"
                )
            raise GreenApiError(f"green {operation} returned HTTP {status}")

        body = _safe_json(response)
        if isinstance(body, dict) and body.get("code") and not body.get("idInstance"):
            # Some Green error envelopes arrive on 200 with a code field.
            raise GreenApiError(
                f"green {operation} error {body.get('code')}: {body.get('description')}"
            )
        return body if body is not None else {}


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError):
        return None


def _classify_timeout(is_write: bool) -> type[RetryableError | IndeterminateError]:
    return GreenApiIndeterminateError if is_write else GreenApiTransientError


def _classify_transport(is_write: bool) -> type[RetryableError | IndeterminateError]:
    return GreenApiIndeterminateError if is_write else GreenApiTransientError


def _classify_http_5xx(is_write: bool) -> type[RetryableError | IndeterminateError]:
    return GreenApiIndeterminateError if is_write else GreenApiTransientError
