"""Green API integration settings.

Loaded from environment variables (no ``python-dotenv`` in v2 -- rely on real
env). Required for any Green operation; the pure runtime does not import this
module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["GreenSettings", "load_settings"]


_DEFAULT_PARTNER_URL = "https://api.green-api.com"


@dataclass(frozen=True)
class GreenSettings:
    """Configuration needed to talk to Green's partner API."""

    partner_api_url: str
    partner_token: str


def load_settings(
    *,
    partner_api_url: str | None = None,
    partner_token: str | None = None,
) -> GreenSettings:
    """Build :class:`GreenSettings` from arguments or environment.

    ``GREEN_API_PARTNER_URL`` defaults to the public Green API endpoint.
    ``GREEN_API_PARTNER_TOKEN`` is required -- a missing partner token is a
    deployment/configuration error, not a runtime error.
    """

    url = partner_api_url or os.getenv("GREEN_API_PARTNER_URL", _DEFAULT_PARTNER_URL)
    token = partner_token or os.getenv("GREEN_API_PARTNER_TOKEN")
    if not token:
        raise ValueError(
            "GREEN_API_PARTNER_TOKEN is required. "
            "Set it in the environment before constructing Green components."
        )
    return GreenSettings(partner_api_url=url.rstrip("/"), partner_token=token)
