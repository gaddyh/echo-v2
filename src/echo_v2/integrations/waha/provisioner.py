"""WAHA implementation of the provider-neutral WhatsApp provisioner.

Agent-based (Phase 3 revision): Echo never talks to WAHA. This provisioner is
a pure control-plane adapter — it persists placement decisions and enqueues
management commands for the per-VM agent (which executes them against its
local WAHA on 127.0.0.1:3000 and reports back through
``POST /internal/waha/events``).

Echo owns placement and identity: the session name is derived from a UUID
generated at initial allocation (never the user's phone number) and is
**stable** — subsequent attempts reconcile against the same assigned worker
and session (the agent makes create-and-start idempotent by inspecting the
existing session first).

Status and QR reads are served from Echo's persisted lifecycle state (kept
fresh by agent events); on-demand paths enqueue ``get_qr`` / ``start`` /
``get_status`` commands. Echo starts a stopped session only as the direct
consequence of a user action ('qr' keyword / הצג QR click), at most once per
cooldown window — never a restart loop. WAHA owns reconnection.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from echo_v2.domain.waha import (
    WahaCommandAction,
    WahaSessionAssignment,
    WahaSessionStatus,
    map_waha_session_status,
)
from echo_v2.integrations.waha.settings import WahaSettings
from echo_v2.persistence.waha_sessions import WahaSessionRepository
from echo_v2.ports.whatsapp import (
    ConnectionConfig,
    ConnectionRef,
    ConnectionStatus,
    ConnectionStatusSnapshot,
    CreatedConnection,
    PairingOutcome,
    PairingQr,
    PairingResult,
    ProviderCredentials,
)
from echo_v2.runtime.errors import PermanentError

__all__ = ["WahaProvisioner"]

_logger = logging.getLogger("echo_v2.waha.provisioner")

_PROVIDER = "waha"

# Minimum spacing between Echo-initiated start commands for one session
# (in-process). The on-demand paths can be re-triggered by repeated user
# actions; this prevents them from becoming a restart loop.
_START_COOLDOWN_SECONDS = 60.0


class WahaProvisioner:
    """Translate Echo lifecycle calls into control-plane state + commands."""

    def __init__(
        self,
        *,
        sessions: WahaSessionRepository,
        settings: WahaSettings,
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        self._last_start_attempt: dict[str, float] = {}

    # --- WhatsAppProvisioner ------------------------------------------------

    async def create_connection(self, config: ConnectionConfig) -> CreatedConnection:
        """Allocate (or reuse) a session and enqueue create-and-start.

        The assignment **and** the ``create_and_start`` command are persisted
        atomically before this returns — the agent's lifecycle events can
        therefore resolve the user even if they beat the onboarding flow,
        and an assignment can never exist without an executable provisioning
        command. Returns immediately (no HTTP to WAHA, ever).
        """
        user_id = config.user_id
        if not user_id:
            raise PermanentError(
                "WAHA provisioning requires ConnectionConfig.user_id"
            )
        assignment = await self._sessions.allocate_and_enqueue_create(
            user_id,
            heartbeat_timeout_seconds=self._settings.heartbeat_timeout_seconds,
        )
        _logger.info(
            "waha create_and_start enqueued session=%s worker=%s",
            assignment.session_name,
            assignment.worker_id,
        )
        return self._created(assignment)

    def _created(self, assignment: WahaSessionAssignment) -> CreatedConnection:
        return CreatedConnection(
            ref=ConnectionRef(
                provider=_PROVIDER,
                provider_connection_id=assignment.session_name,
            ),
            # WAHA auth state lives on the worker's VM; Echo stores no
            # provider credential for it (same convention as Phase 2).
            credentials=ProviderCredentials(data=b""),
        )

    async def configure_connection(
        self,
        connection: ConnectionRef,
        config: ConnectionConfig,
    ) -> None:
        """No-op: the lifecycle webhook is configured by the agent at
        session creation (pointing at its own local listener)."""
        del connection, config

    async def get_status(self, connection: ConnectionRef) -> ConnectionStatusSnapshot:
        """Serve status from Echo's persisted lifecycle state.

        The state is kept fresh by agent events; this never performs I/O to
        a worker. Explicit recovery (missed events, agent restart) is
        available via ``WahaSessionService.reconcile_session``.
        """
        assignment = await self._sessions.get(connection.provider_connection_id)
        if assignment is None:
            raise PermanentError(
                "no WAHA assignment for session "
                f"{connection.provider_connection_id}"
            )
        # The stored status is exactly the mapped value of the latest
        # applied lifecycle event (kept fresh by agent events).
        return ConnectionStatusSnapshot(
            status=ConnectionStatus(assignment.status),
            provider_raw_status=assignment.raw_status,
        )

    async def get_pairing_qr(self, connection: ConnectionRef) -> PairingResult:
        """Route the on-demand QR request (user-action triggered only).

        Serves a recently delivered, still-fresh stored QR image when one
        exists; otherwise enqueues a ``get_qr`` command (or a ``start``
        command for a stopped session — at most once per cooldown window)
        and reports PREPARING. The QR itself arrives asynchronously as an
        agent ``qr_ready`` event and is delivered through the onboarding
        hooks; Echo never fetches it from WAHA.
        """
        assignment = await self._sessions.get(connection.provider_connection_id)
        if assignment is None:
            raise PermanentError(
                "no WAHA assignment for session "
                f"{connection.provider_connection_id}"
            )
        raw_status = assignment.raw_status
        mapped = map_waha_session_status(raw_status)
        if mapped is ConnectionStatus.CONNECTED:
            return PairingResult(outcome=PairingOutcome.ALREADY_AUTHORIZED)

        if raw_status in (WahaSessionStatus.STOPPED, WahaSessionStatus.FAILED):
            started = await self._start_once(
                connection.provider_connection_id, assignment.worker_id
            )
            return PairingResult(
                outcome=PairingOutcome.PREPARING,
                message=(
                    "session restart requested"
                    if started
                    else "session restart pending"
                ),
            )

        stored = self._stored_qr(assignment)
        if stored is not None:
            return PairingResult(
                outcome=PairingOutcome.QR_READY,
                qr=PairingQr(image_base64=stored),
            )

        enqueued = await self._sessions.enqueue(
            connection.provider_connection_id,
            worker_id=assignment.worker_id,
            action=WahaCommandAction.GET_QR,
        )
        return PairingResult(
            outcome=PairingOutcome.PREPARING,
            message=(
                "QR requested from the worker"
                if enqueued
                else "QR request already pending"
            ),
        )

    def _stored_qr(self, assignment: WahaSessionAssignment) -> str | None:
        """A previously delivered QR image, if still fresh.

        QR images are short-lived by design: older than the TTL they are
        considered expired (WhatsApp rotates them) and are not re-sent.
        """
        if (
            assignment.last_qr_image is None
            or assignment.qr_updated_at is None
            or assignment.status != "pairing_required"
        ):
            return None
        age = (
            datetime.now(timezone.utc) - assignment.qr_updated_at
        ).total_seconds()
        if age > self._settings.qr_ttl_seconds:
            return None
        return assignment.last_qr_image

    async def _start_once(self, session_name: str, worker_id: str) -> bool:
        """Request a stopped-session start at most once per cooldown.

        Returns ``False`` (without enqueuing) when a recent start request is
        still inside the cooldown — repeated user actions must not become a
        restart loop. WAHA owns normal automatic reconnection.
        """
        now = time.monotonic()
        last = self._last_start_attempt.get(session_name)
        if last is not None and now - last < _START_COOLDOWN_SECONDS:
            return False
        self._last_start_attempt[session_name] = now
        await self._sessions.enqueue(
            session_name,
            worker_id=worker_id,
            action=WahaCommandAction.START,
        )
        _logger.info(
            "waha start command enqueued session=%s worker=%s",
            session_name,
            worker_id,
        )
        return True

    async def unpair(self, connection: ConnectionRef) -> None:
        raise PermanentError("unpair is not supported for WAHA connections")

    async def delete_connection(self, connection: ConnectionRef) -> None:
        raise PermanentError("delete is not supported for WAHA connections")
