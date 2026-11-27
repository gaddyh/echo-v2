"""Settings for WAHA session management (Phase 3, agent-based).

Echo is the control plane: it never talks to WAHA directly and holds no
WAHA API credentials — the per-VM agent owns the local WAHA REST API
(127.0.0.1:3000) and polls Echo for management commands. Echo-side
configuration is therefore limited to the onboarding switch, the command
queue's leasing/retry behaviour, and how long a delivered QR image may be
kept for on-demand re-send.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["WahaSettings", "load_waha_settings"]


@dataclass(frozen=True)
class WahaSettings:
    """Echo-side WAHA management configuration (no secrets)."""

    onboarding_enabled: bool
    heartbeat_timeout_seconds: float
    command_lease_seconds: float
    command_max_attempts: int
    command_poll_limit: int
    qr_ttl_seconds: float


def load_waha_settings(
    *,
    env: Mapping[str, str] | None = None,
) -> WahaSettings:
    """Build WAHA settings from the environment (or an injected mapping)."""
    source = env if env is not None else os.environ
    return WahaSettings(
        onboarding_enabled=source.get("WAHA_ONBOARDING_ENABLED", "false").lower()
        in ("1", "true", "yes"),
        heartbeat_timeout_seconds=float(
            source.get("WAHA_WORKER_HEARTBEAT_TIMEOUT_SECONDS", "90")
        ),
        command_lease_seconds=float(
            source.get("WAHA_COMMAND_LEASE_SECONDS", "120")
        ),
        command_max_attempts=int(source.get("WAHA_COMMAND_MAX_ATTEMPTS", "5")),
        command_poll_limit=int(source.get("WAHA_COMMAND_POLL_LIMIT", "10")),
        qr_ttl_seconds=float(source.get("WAHA_QR_TTL_SECONDS", "120")),
    )
