"""Domain models for the WAHA batch-agent integration (Phase 1).

The WAHA batch agent runs on each WAHA GOWS VM: it reads messages from the
local GOWS SQLite databases, POSTs them in batches to Echo, and deletes its
local rows only after Echo returns an ACK with a matching
``(session, message_id, sha256)`` triple.

Phase 1 only provides durable ingestion + the worker registry. The raw
Whatsmeow payload (``data``) is preserved verbatim — normalization, Guard
analysis, and media transfer are later phases.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

__all__ = [
    "WahaBatchIngestResult",
    "WahaInboxMessage",
    "WahaWorkerRegistration",
    "WahaWorkerStatus",
]


@dataclass(frozen=True)
class WahaInboxMessage:
    """One message as sent by the batch agent (wire fields, typed).

    ``data`` is the complete JSON serialization of the Whatsmeow
    ``events.Message`` — kept as the exact string the agent sent; Echo never
    re-serializes it. ``from_me`` / ``is_real`` keep the wire's 0/1 integers.
    """

    session: str
    message_id: str
    chat_jid: str
    whatsapp_timestamp: datetime
    from_me: int
    is_real: int
    data: str
    sha256: str
    seq: int
    agent_first_seen_at: datetime
    batch_id: str


@dataclass(frozen=True)
class WahaBatchIngestResult:
    """Classification of one batch after the ingest transaction committed.

    ``inserted`` — new ``(session, message_id)`` rows created.
    ``updated`` — existing rows whose ``sha256`` changed (new revision).
    ``duplicated`` — messages identical to what is already stored (no state
    change). Within-batch repeats of the same key+hash are counted by the
    route on top of this.
    """

    inserted: int
    updated: int
    duplicated: int


@dataclass(frozen=True)
class WahaWorkerRegistration:
    """One registration / heartbeat POST from a WAHA batch agent."""

    worker_id: str
    region: str
    endpoint: str
    max_sessions: int
    active_sessions: int
    version: str
    health: str
    reported_heartbeat: datetime
    pending: int


@dataclass(frozen=True)
class WahaWorkerStatus:
    """Registry read model: reported state + Echo-derived availability.

    ``available`` combines a fresh Echo-side heartbeat with
    ``health == "ok"``. ``has_capacity`` is deliberately separate: capacity
    eligibility is a placement decision, not part of availability.
    """

    worker_id: str
    region: str
    endpoint: str
    max_sessions: int
    active_sessions: int
    agent_version: str
    health: str
    reported_heartbeat: datetime
    last_seen_at: datetime
    first_registered_at: datetime
    pending: int
    available: bool
    has_capacity: bool
