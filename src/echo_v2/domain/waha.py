"""Domain models for the WAHA batch-agent integration (Phase 1).

The WAHA batch agent runs on each WAHA GOWS VM: it reads messages from the
local GOWS SQLite databases, POSTs them in batches to Echo, and deletes its
local rows only after Echo returns an ACK with a matching
``(session, message_id, sha256)`` triple.

Phase 1 only provides durable ingestion + the worker registry. The raw
Whatsmeow payload (``data``) is preserved verbatim — normalization, Guard
analysis, and media transfer are later phases.

Phase 3 adds session management: the ``waha_sessions`` assignment (one stable
WAHA session name per Echo user, pinned to one registered worker) and the
lifecycle state machine driven by WAHA ``session.status`` webhooks.

Phase 3 revision (agent-based management): Echo never talks to WAHA directly.
The per-VM agent polls ``POST /internal/waha/commands`` for management
commands (create/start/get_qr/get_status), executes them against the local
WAHA REST API on 127.0.0.1:3000, and reports results + lifecycle events +
QR images through ``POST /internal/waha/events``. All cross-boundary traffic
is agent-initiated outbound HTTPS. WAHA is a **receive-only** connector —
Echo never sends WhatsApp messages through it.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from echo_v2.ports.whatsapp import ConnectionStatus

__all__ = [
    "ClaimedWahaMessage",
    "WahaAllocationError",
    "WahaBatchIngestResult",
    "WahaCommand",
    "WahaCommandAction",
    "WahaCommandStatus",
    "WahaInboxMessage",
    "WahaLifecycleEffect",
    "WahaMediaPayload",
    "WahaRegistrationResult",
    "WahaSessionAssignment",
    "WahaSessionStatus",
    "WahaTransitionOutcome",
    "WahaWorkerRegistration",
    "WahaWorkerStatus",
    "effect_for_waha_status",
    "generate_worker_token",
    "hash_worker_token_secret",
    "map_waha_session_status",
    "parse_worker_token",
]


@dataclass(frozen=True)
class WahaMediaPayload:
    """Media descriptor extracted from a GOWS envelope.

    ``media_key`` / ``file_sha256`` are the base64 strings exactly as they
    appear in the envelope (WhatsApp client-side E2E encryption fields).
    ``media_type`` selects the HKDF info string ("WhatsApp <Type> Keys").
    """

    media_type: str
    kind: str
    url: str
    media_key: str
    file_length: int
    file_sha256: str
    mimetype: str | None = None
    file_name: str | None = None


@dataclass(frozen=True)
class ClaimedWahaMessage:
    """One inbox row claimed for processing by the WAHA consumer.

    ``revision`` fences the completion: if the payload is updated while the
    row is being processed (revision bumped, status reset to pending), the
    processor's completion is rejected and the new revision is re-processed.
    """

    message: WahaInboxMessage
    revision: int
    worker_id: str


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
    """One registration / heartbeat POST from a WAHA batch agent.

    ``management_protocol`` declares support for the Phase 3 agent-based
    management protocol (command polling + event reporting). ``0`` (the
    default, and the value implied by its absence in older agents) means a
    batch-only agent that must never receive onboarding assignments.

    ``resources`` is the optional agent-reported VM/container/disk telemetry
    object (Operations Center contract, migration 0067). Backward
    compatible: old agents never send it and the registry stores NULL.
    """

    worker_id: str
    region: str
    endpoint: str
    max_sessions: int
    active_sessions: int
    version: str
    health: str
    reported_heartbeat: datetime
    pending: int
    management_protocol: int = 0
    resources: dict[str, Any] | None = None
    resources_observed_at: datetime | None = None


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
    management_protocol: int = 0
    resources: dict[str, Any] | None = None
    resources_observed_at: datetime | None = None


# --- Phase 3: session management --------------------------------------------


class WahaSessionStatus:
    """Raw WAHA ``session.status`` values (wire constants).

    Kept as plain constants (not an enum) so an unknown value from a newer
    WAHA release never crashes the callback path — unknown statuses are
    logged and ignored.
    """

    STARTING = "STARTING"
    SCAN_QR_CODE = "SCAN_QR_CODE"
    WORKING = "WORKING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class WahaLifecycleEffect:
    """Side effect queued by a lifecycle transition.

    Persisting the transition and executing its effect are **separate**
    operations: the effect stays queued (``pending_effect``) until it
    completes, so a failed effect is retried on the next callback even when
    the stored status already matches the incoming one.
    """

    CONFIRM_WORKING = "confirm_working"
    NOTIFY_DISCONNECTED = "notify_disconnected"
    DELIVER_QR = "deliver_qr"


def map_waha_session_status(raw: str | None) -> ConnectionStatus | None:
    """Map a raw WAHA session status to Echo's connection status.

    Returns ``None`` for unknown statuses — callers must ignore them safely
    and never change established connection state because of one.
    """
    if raw == WahaSessionStatus.STARTING:
        return ConnectionStatus.CONNECTING
    if raw == WahaSessionStatus.SCAN_QR_CODE:
        return ConnectionStatus.PAIRING_REQUIRED
    if raw == WahaSessionStatus.WORKING:
        return ConnectionStatus.CONNECTED
    # STOPPED and FAILED both mean "user action required to reconnect" —
    # Echo has no 'stopped' status, and PAIRING_REQUIRED is exactly the
    # existing disconnected-needs-user-action state (the disconnect
    # notification flow keys off it).
    if raw in (WahaSessionStatus.STOPPED, WahaSessionStatus.FAILED):
        return ConnectionStatus.PAIRING_REQUIRED
    return None


def effect_for_waha_status(raw: str | None) -> str | None:
    """The side effect queued when a raw WAHA status is applied (or None)."""
    if raw == WahaSessionStatus.WORKING:
        return WahaLifecycleEffect.CONFIRM_WORKING
    if raw == WahaSessionStatus.SCAN_QR_CODE:
        return WahaLifecycleEffect.DELIVER_QR
    if raw in (WahaSessionStatus.STOPPED, WahaSessionStatus.FAILED):
        return WahaLifecycleEffect.NOTIFY_DISCONNECTED
    return None


@dataclass(frozen=True)
class WahaSessionAssignment:
    """Echo-owned mapping of one user to one WAHA session on one worker.

    ``session_name`` is generated once at initial allocation and is **stable**
    across subsequent onboarding attempts — retries reconcile against the same
    worker and session; Echo never re-allocates while an outcome is unresolved.
    ``status`` holds the Echo-mapped :class:`ConnectionStatus` value of the
    latest applied lifecycle event; it is persisted independently of the
    ``whatsapp_connections`` row so callbacks arriving before the connection
    record is created are not lost. ``last_qr_image`` holds the latest
    agent-delivered QR short-lived (for on-demand re-send on explicit user
    action); it is cleared on pairing and never logged.
    """

    session_name: str
    user_id: str
    worker_id: str
    status: str
    raw_status: str | None = None
    last_status_at: datetime | None = None
    event_seq: int = 0
    pending_effect: str | None = None
    pending_effect_at: datetime | None = None
    last_qr_hash: str | None = None
    last_qr_image: str | None = None
    qr_updated_at: datetime | None = None
    account_phone: str | None = None
    account_identity_at: datetime | None = None


@dataclass(frozen=True)
class WahaTransitionOutcome:
    """Result of fence-applying one lifecycle event.

    ``applied`` is ``True`` only when the event changed state (newer event
    timestamp or different status) — duplicates and stale events do not
    re-apply. ``pending_effect`` carries the queued side effect either way so
    callers retry unfinished effects regardless of duplication.
    """

    applied: bool
    session_name: str
    status: str
    raw_status: str | None
    previous_status: str | None
    event_seq: int
    event_at: datetime | None
    pending_effect: str | None
    pending_effect_at: datetime | None
    connection_updated: bool


class WahaAllocationError(Exception):
    """No eligible WAHA worker is available for a new session assignment.

    Carries diagnostic counts (structured, no secrets) so the caller can log
    *why* allocation failed: stale heartbeats, unhealthy workers, full
    capacity, or missing management configuration.
    """

    def __init__(
        self,
        reason: str,
        *,
        registered: int = 0,
        healthy: int = 0,
        configured: int = 0,
        with_capacity: int = 0,
    ) -> None:
        self.reason = reason
        self.registered = registered
        self.healthy = healthy
        self.configured = configured
        self.with_capacity = with_capacity
        super().__init__(
            f"no eligible WAHA worker (reason={reason}, registered={registered}, "
            f"healthy={healthy}, configured={configured}, "
            f"with_capacity={with_capacity})"
        )


# --- Phase 3 revision: agent command queue ------------------------------------


class WahaCommandAction:
    """Management operations Echo asks the per-VM agent to execute against
    the local WAHA REST API (127.0.0.1:3000). Echo never connects to WAHA."""

    CREATE_AND_START = "create_and_start"
    START = "start"
    GET_QR = "get_qr"
    GET_STATUS = "get_status"


class WahaCommandStatus:
    PENDING = "pending"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class WahaCommand:
    """One queued management command, assigned to a specific worker.

    Lifecycle: ``pending`` → (agent claims) ``claimed`` (lease-bounded) →
    ``completed`` / ``failed``. A claimed command whose lease expired becomes
    claimable again (bounded by ``attempts``); completion is idempotent.
    """

    command_id: str
    worker_id: str
    session_name: str
    action: str
    status: str
    attempts: int
    created_at: datetime | None = None
    claimed_at: datetime | None = None
    lease_expires_at: datetime | None = None
    completed_at: datetime | None = None
    last_error: str | None = None


# --- Phase 3 revision: per-worker management tokens -----------------------------

_WORKER_TOKEN_PREFIX = "wt_"


def generate_worker_token(worker_id: str) -> str:
    """Issue a per-worker management token.

    Format ``wt_<worker_id>_<secret>`` — the embedded worker id makes
    verification a direct row lookup plus a constant-time secret compare;
    the secret itself is never stored (only its SHA-256 hash is).
    """
    return f"{_WORKER_TOKEN_PREFIX}{worker_id}_{secrets.token_urlsafe(32)}"


def parse_worker_token(token: str) -> tuple[str, str] | None:
    """Split a worker token into ``(worker_id, secret)`` or ``None``."""
    if not token.startswith(_WORKER_TOKEN_PREFIX):
        return None
    remainder = token[len(_WORKER_TOKEN_PREFIX):]
    worker_id, separator, secret = remainder.partition("_")
    if not separator or not worker_id or not secret:
        return None
    return worker_id, secret


def hash_worker_token_secret(secret: str) -> bytes:
    """The stored form of a worker token's secret (plaintext never stored)."""
    return hashlib.sha256(secret.encode("utf-8")).digest()


@dataclass(frozen=True)
class WahaRegistrationResult:
    """Outcome of one registration / heartbeat POST.

    ``issued_worker_token`` is set exactly once — on the worker's first
    management-capable registration (``management_protocol >= 1`` with no
    existing token). The plaintext token is never persisted; only its
    SHA-256 hash is stored.
    """

    first: bool
    issued_worker_token: str | None = None
