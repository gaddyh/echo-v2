"""Async HTTP client for the Echo Baileys connector."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from echo_v2.domain.chat import Message
from echo_v2.integrations.baileys.settings import BaileysSettings
from echo_v2.runtime.errors import IndeterminateError, PermanentError, RetryableError

__all__ = ["BaileysClient", "BaileysConnectorError", "BaileysMediaUrlResolver"]

_logger = logging.getLogger("echo_v2.baileys.client")


class BaileysConnectorError(PermanentError):
    """A connector response that cannot be recovered by retrying."""


class BaileysMediaUrlResolver:
    def __init__(self, client: BaileysClient) -> None:
        self._client = client

    async def resolve(self, message: Message) -> str | None:
        reference = message.media_reference
        if reference is None:
            return None
        parts = reference.split(":", 2)
        if len(parts) != 3 or parts[0] != "baileys":
            raise BaileysConnectorError("invalid Baileys media reference")
        return await self._client.get_media_url(parts[1], reference)


class BaileysClient:
    """Raw HTTP client for the connector's provider-neutral API."""

    def __init__(
        self,
        settings: BaileysSettings,
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

    async def create_connection(self, phone_number: str | None = None) -> dict[str, Any]:
        body: dict[str, str] = {}
        if phone_number is not None:
            body["phone_number"] = phone_number
        return await self._request_json(
            "POST", "/connections", operation="create_connection", json_body=body,
            write=True,
        )

    async def get_status(self, connection_id: str) -> dict[str, Any]:
        return await self._request_json(
            "GET", f"/connections/{connection_id}/status", operation="get_status",
        )

    async def get_qr(self, connection_id: str) -> dict[str, Any]:
        return await self._request_json(
            "GET", f"/connections/{connection_id}/qr", operation="get_qr",
        )

    async def get_media_url(self, connection_id: str, media_reference: str) -> str:
        data = await self._request_json(
            "GET",
            f"/connections/{connection_id}/media-url",
            operation="get_media_url",
            query_params={"reference": media_reference},
        )
        url = data.get("media_download_url")
        if not isinstance(url, str) or not url:
            raise BaileysConnectorError("Baileys connector returned an empty media URL")
        return url

    async def unpair(self, connection_id: str) -> None:
        await self._request_json(
            "POST", f"/connections/{connection_id}/unpair", operation="unpair",
            write=True, allow_empty=True,
        )

    async def delete_connection(self, connection_id: str) -> None:
        await self._request_json(
            "DELETE", f"/connections/{connection_id}", operation="delete_connection",
            write=True, allow_empty=True,
        )

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        json_body: dict[str, Any] | None = None,
        query_params: dict[str, str] | None = None,
        write: bool = False,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        url = f"{self._settings.connector_url}{path}"
        try:
            response = await self._client.request(
                method,
                url,
                headers={"Authorization": f"Bearer {self._settings.internal_api_token}"},
                params=query_params,
                json=json_body,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            if write:
                raise IndeterminateError(
                    f"Baileys connector {operation} outcome is unknown"
                ) from exc
            raise RetryableError(
                f"Baileys connector {operation} transport failure"
            ) from exc
        except httpx.HTTPError as exc:
            raise RetryableError(
                f"Baileys connector {operation} HTTP failure"
            ) from exc

        if response.status_code >= 500:
            if write:
                raise IndeterminateError(
                    f"Baileys connector {operation} outcome is unknown"
                )
            raise RetryableError(
                f"Baileys connector {operation} returned server error"
            )
        if response.status_code >= 400:
            raise BaileysConnectorError(
                f"Baileys connector {operation} returned HTTP {response.status_code}"
            )
        if allow_empty and response.status_code == 204:
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            raise BaileysConnectorError(
                f"Baileys connector {operation} returned invalid JSON"
            ) from exc
        if not isinstance(data, dict):
            raise BaileysConnectorError(
                f"Baileys connector {operation} returned a non-object response"
            )
        _logger.info(
            "provider=baileys operation=%s status_code=%s",
            operation,
            response.status_code,
        )
        return data
