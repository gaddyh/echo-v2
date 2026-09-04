"""Settings for the 360dialog integration.

Read from environment variables. The API key is the per-phone-number key
from the 360dialog dashboard. The webhook secret is a bearer token Echo
generates and configures in the 360dialog dashboard — incoming webhooks
must carry it in the ``Authorization`` header.
"""

from __future__ import annotations

import os

__all__ = ["Dialog360Settings"]


class Dialog360Settings:
    """Configuration for the 360dialog bot integration."""

    def __init__(self) -> None:
        self.api_key: str = os.environ.get("D360_API_KEY", "")
        self.api_base_url: str = os.environ.get(
            "D360_API_BASE_URL", "https://waba-v2.360dialog.io"
        )
        self.webhook_secret: str = os.environ.get("D360_WEBHOOK_SECRET", "")
