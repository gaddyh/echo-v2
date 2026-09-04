"""Raw async HTTP client for 360dialog / WhatsApp Cloud API.

This is the only module that knows 360dialog URL shapes and embeds the
D360 API key. It sends messages **from the Echo Business Bot** to users.
The bot is a single shared number — there is no per-user instance.

Error classification follows the same taxonomy as :class:`GreenClient`:
* 5xx / timeout on a send → :class:`IndeterminateError` (message may have
  been delivered; never blindly retry).
* 4xx (non-429) → :class:`PermanentError`.
* 429 / connect error → :class:`RetryableError`.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from echo_v2.integrations.dialog360.settings import Dialog360Settings
from echo_v2.runtime.errors import (
    IndeterminateError,
    PermanentError,
    RetryableError,
)

__all__ = ["Dialog360Client"]

_logger = logging.getLogger("echo_v2.dialog360.client")


class Dialog360Client:
    """HTTP client for the 360dialog WhatsApp Business API."""

    def __init__(
        self,
        settings: Dialog360Settings,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send_text(self, recipient: str, text: str) -> str:
        """Send a text message from the bot to ``recipient``.

        ``recipient`` is a phone number (E.164 or raw). Returns the
        message ID assigned by 360dialog. A timeout or 5xx is
        :class:`IndeterminateError` — the message may have been sent.
        """
        phone = _normalize_phone(recipient)
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": phone,
            "type": "text",
            "text": {"body": text},
        }
        data = await self._post_json(
            f"{self._settings.api_base_url}/messages",
            payload,
            operation="send_text",
        )
        msg_id = data.get("message_id") if isinstance(data, dict) else None
        if not msg_id:
            # 360dialog may return messages[0].id on some API versions.
            messages = data.get("messages") if isinstance(data, dict) else None
            if isinstance(messages, list) and messages:
                msg_id = messages[0].get("id")
        if not msg_id:
            raise PermanentError("360dialog send_text response missing message id")
        return str(msg_id)

    async def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        operation: str,
    ) -> dict[str, Any]:
        headers = {
            "D360-API-KEY": self._settings.api_key,
            "Content-Type": "application/json",
        }
        try:
            response = await self._client.post(url, json=payload, headers=headers)
        except httpx.ConnectError as exc:
            _logger.info("provider=dialog360 operation=%s status=connect_error", operation)
            raise RetryableError(f"connect error during {operation}") from exc
        except httpx.ReadTimeout as exc:
            _logger.info("provider=dialog360 operation=%s status=read_timeout", operation)
            raise IndeterminateError(f"read timeout during {operation}") from exc
        except httpx.HTTPError as exc:
            _logger.info(
                "provider=dialog360 operation=%s status=transport_error error_type=%s",
                operation,
                type(exc).__name__,
            )
            raise IndeterminateError(
                f"transport error during {operation}: {type(exc).__name__}"
            ) from exc

        status = response.status_code
        _logger.info("provider=dialog360 operation=%s status_code=%s", operation, status)

        if status == 429:
            raise RetryableError(f"rate limited during {operation}")
        if 500 <= status < 600:
            raise IndeterminateError(f"dialog360 {operation} returned HTTP {status}")
        if 400 <= status < 500:
            raise PermanentError(f"dialog360 {operation} returned HTTP {status}")

        body = _safe_json(response)
        return body if isinstance(body, dict) else {}


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _normalize_phone(recipient: str) -> str:
    """Strip formatting to get a bare phone number for the API."""
    return (
        recipient
        .replace("@c.us", "")
        .replace("+", "")
        .replace(" ", "")
        .replace("-", "")
        .strip()
    )
