"""Settings for the local Baileys connector HTTP API."""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["BaileysSettings", "load_settings"]


@dataclass(frozen=True)
class BaileysSettings:
    """Configuration needed to call the connector service."""

    connector_url: str
    internal_api_token: str


def load_settings(
    *,
    connector_url: str | None = None,
    internal_api_token: str | None = None,
) -> BaileysSettings:
    """Build connector settings from explicit values or environment."""
    url = connector_url or os.getenv("BAILEYS_CONNECTOR_URL") or "http://localhost:8080"
    token = internal_api_token or os.getenv("BAILEYS_CONNECTOR_TOKEN")
    if not token:
        raise ValueError(
            "BAILEYS_CONNECTOR_TOKEN is required. Set it before constructing "
            "Baileys components."
        )
    return BaileysSettings(connector_url=url.rstrip("/"), internal_api_token=token)
