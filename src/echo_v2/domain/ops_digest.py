"""Ops digest — one hourly connector log digest, persisted for browsing.

An ``OpsDigest`` is produced by the echo-whatsapp-connector's ops log
monitor: an LLM-written summary of one hour of connector logs (what
happened, what resolved from the previous hour, what is still open),
together with the redacted counters that fed it. The connector POSTs it
to echo-v2's internal ops-alert endpoint; echo-v2 persists it here for
the admin health dashboard. Nothing pushes it to users — Option B:
table and history only.

``digest_key`` is the idempotency key (``worker_id:window_to``): a
connector delivery retry re-POSTs the same digest and must not create
a duplicate row (``ON CONFLICT DO NOTHING``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class OpsDigest:
    digest_key: str
    worker_id: str
    source: str
    severity: str
    attention: bool
    one_liner: str
    digest: dict[str, Any]
    metrics: dict[str, Any] | None = None
    window_from: datetime | None = None
    window_to: datetime | None = None
    received_at: datetime | None = None


__all__ = ["OpsDigest"]
