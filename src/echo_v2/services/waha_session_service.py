"""WAHA session lifecycle service (Phase 3, agent-based management).

Processes events reported by the per-VM WAHA agent through
``POST /internal/waha/events`` into Echo connection state and user-facing
onboarding side effects. Echo never talks to WAHA: commands flow out via the
``waha_commands`` queue (claimed by the agent), results and lifecycle events
flow back in here.

Event types (final contract — see AGENTS.md):

* ``command_result`` — execution outcome for one command (idempotent,
  worker-guarded), optionally carrying a ``qr`` image (``get_qr``) or a
  ``status`` string (``get_status``).
* ``session_status`` — a WAHA ``session.status`` lifecycle transition
  observed by the agent's local webhook listener.
* ``qr_ready`` — a pairing QR image the agent fetched from its local WAHA.

Design rules:

* **Worker identity**: every event is processed on behalf of the worker
  authenticated by its management token (never a body-supplied ``worker_id``)
  and may only touch sessions assigned to that worker.
* **Early events**: lifecycle state is persisted in ``waha_sessions``
  independently of the ``whatsapp_connections`` row — an event can arrive
  before the create command completes. When the connection row is created
  later, :meth:`reconcile_connection` backfills it (a WORKING event that
  arrived early still completes onboarding correctly).
* **Retry-safe side effects**: persisting a transition and executing its
  effect are separate operations. The effect stays queued
  (``pending_effect``) until it completes; a failed effect makes the events
  endpoint return 503 so the agent retries the same event, and effects
  themselves are idempotent (existing onboarding mechanisms).
* **QR concurrency**: a QR image is delivered and recorded only when the
  session is still pairing and the event is still the current generation —
  a delayed QR never overwrites a newer QR or resurrects pairing state
  after WORKING. Byte-identical QR images are not re-sent; QR images are
  kept short-lived (TTL) and never logged.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from echo_v2.domain.waha import (
    WahaCommandAction,
    WahaLifecycleEffect,
    WahaSessionAssignment,
    WahaSessionStatus,
    WahaTransitionOutcome,
    map_waha_session_status,
)
from echo_v2.integrations.waha.settings import WahaSettings
from echo_v2.persistence.waha_sessions import WahaSessionRepository

__all__ = [
    "WahaCallbackAuthError",
    "WahaCallbackBadRequestError",
    "WahaCallbackForbiddenError",
    "WahaCallbackRetryError",
    "WahaOnboardingHooks",
    "WahaSessionService",
    "event_datetime",
]

_logger = logging.getLogger("echo_v2.services.waha_session_service")

_MAX_EVENT_BODY_BYTES = 512 * 1024


class WahaCallbackAuthError(Exception):
    """Worker identity validation failed (HTTP 401)."""


class WahaCallbackForbiddenError(Exception):
    """The event targets another worker's session or command (HTTP 403)."""


class WahaCallbackBadRequestError(Exception):
    """Malformed event payload (HTTP 400 — retrying cannot help)."""


class WahaCallbackRetryError(Exception):
    """A temporary failure the agent should retry (HTTP 503).

    The lifecycle state stays committed; only the unfinished side effect
    (QR delivery, onboarding confirmation) is retried.
    """


@runtime_checkable
class WahaOnboardingHooks(Protocol):
    """User-facing side effects, implemented by the onboarding service."""

    async def handle_connection_established_by_id(self, user_id: str) -> None: ...

    async def handle_disconnect_notification(self, user_id: str) -> None: ...

    async def deliver_pairing_qr(self, user_id: str, image_base64: str) -> None: ...


def event_datetime(value: Any) -> datetime:
    """Convert an agent event ``timestamp`` (epoch seconds) to a datetime.

    Falls back to ``now()`` when absent/invalid — duplicates then re-apply
    idempotently and the QR hash fence still prevents duplicate deliveries.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            pass
    return datetime.now(timezone.utc)


class WahaSessionService:
    def __init__(
        self,
        *,
        sessions: WahaSessionRepository,
        settings: WahaSettings,
        hooks: WahaOnboardingHooks | None = None,
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        self._hooks = hooks

    def bind_onboarding_hooks(self, hooks: WahaOnboardingHooks) -> None:
        """Late-bind the onboarding side-effect hooks (app wiring resolves
        the construction cycle: onboarding needs this service as its WAHA
        reconciler while this service needs onboarding as its hooks)."""
        self._hooks = hooks

    # --- command polling -----------------------------------------------------

    async def claim_commands(self, worker_id: str) -> list[dict[str, str]]:
        """Claim runnable commands for one (authenticated) worker.

        Returns wire-shaped command objects for the agent; claiming never
        removes anything — completion is explicit and idempotent.
        """
        commands = await self._sessions.claim_for_worker(
            worker_id,
            limit=self._settings.command_poll_limit,
            lease_seconds=self._settings.command_lease_seconds,
            max_attempts=self._settings.command_max_attempts,
        )
        if commands:
            _logger.info(
                "waha commands claimed worker=%s count=%d ids=%s",
                worker_id,
                len(commands),
                [c.command_id for c in commands],
            )
        return [
            {
                "command_id": command.command_id,
                "action": command.action,
                "session": command.session_name,
            }
            for command in commands
        ]

    # --- event ingress ---------------------------------------------------------

    async def handle_agent_event(
        self, payload: dict[str, Any], *, worker_id: str
    ) -> str:
        """Process one agent event on behalf of the authenticated worker.

        Returns an outcome label (``ok``, ``ignored_event``,
        ``unknown_session``, ``unknown_status``, ``qr_superseded``,
        ``qr_duplicate``). Raises :class:`WahaCallbackAuthError` (401),
        :class:`WahaCallbackForbiddenError` (403),
        :class:`WahaCallbackBadRequestError` (400) or
        :class:`WahaCallbackRetryError` (503 — the agent retries).
        """
        if not isinstance(payload, dict):
            raise WahaCallbackBadRequestError("event must be an object")
        claimed_worker = payload.get("worker_id")
        if claimed_worker is not None and claimed_worker != worker_id:
            # The authenticated worker is authoritative; a mismatched body
            # claim is rejected outright.
            raise WahaCallbackForbiddenError(
                "event worker_id does not match the authenticated worker"
            )
        event_type = payload.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise WahaCallbackBadRequestError("invalid event type")

        if event_type == "command_result":
            return await self._handle_command_result(payload, worker_id=worker_id)
        if event_type == "session_status":
            return await self._handle_session_status(
                payload, worker_id=worker_id
            )
        if event_type == "qr_ready":
            return await self._handle_qr_ready(payload, worker_id=worker_id)
        _logger.info("waha event ignored type=%s", event_type)
        return "ignored_event"

    # --- event handlers ---------------------------------------------------------

    async def _handle_command_result(
        self, payload: dict[str, Any], *, worker_id: str
    ) -> str:
        command_id = payload.get("command_id")
        if not isinstance(command_id, str) or not command_id:
            raise WahaCallbackBadRequestError("invalid command_id")
        success = payload.get("success")
        if not isinstance(success, bool):
            raise WahaCallbackBadRequestError("invalid success")
        error = payload.get("error")
        if error is not None and (not isinstance(error, str) or len(error) > 500):
            raise WahaCallbackBadRequestError("invalid error")
        session_name = payload.get("session")
        if not isinstance(session_name, str) or not session_name:
            raise WahaCallbackBadRequestError("invalid session")

        completed = await self._sessions.complete_command(
            command_id,
            worker_id=worker_id,
            success=success,
            error=error,
        )
        if not completed:
            # Unknown or foreign command — a worker may only report its own.
            raise WahaCallbackForbiddenError("command not claimable by worker")

        _logger.info(
            "waha command result command_id=%s worker=%s session=%s success=%s",
            command_id,
            worker_id,
            session_name,
            success,
        )

        # Optional payloads ride along with specific actions: the QR image
        # for get_qr, the observed status for get_status.
        if not success:
            return "ok"
        qr = payload.get("qr")
        if isinstance(qr, dict) and isinstance(qr.get("data"), str) and qr["data"]:
            return await self._handle_qr_ready(payload, worker_id=worker_id)
        status = payload.get("status")
        if isinstance(status, str) and status:
            return await self._handle_session_status(
                payload, worker_id=worker_id
            )
        return "ok"

    async def _handle_session_status(
        self, payload: dict[str, Any], *, worker_id: str
    ) -> str:
        session_name = payload.get("session")
        if not isinstance(session_name, str) or not session_name:
            raise WahaCallbackBadRequestError("invalid session")
        raw_status = payload.get("status")
        if not isinstance(raw_status, str) or not raw_status:
            # A session_status event without a status is malformed agent
            # output (unknown *values* are handled safely below instead).
            raise WahaCallbackBadRequestError("invalid status")
        assignment = await self._resolve_session(session_name, worker_id)
        if assignment is None:
            return "unknown_session"
        return await self._apply_and_run_effects(
            assignment,
            raw_status=raw_status,
            event_at=event_datetime(payload.get("timestamp")),
        )

    async def _handle_qr_ready(
        self, payload: dict[str, Any], *, worker_id: str
    ) -> str:
        session_name = payload.get("session")
        if not isinstance(session_name, str) or not session_name:
            raise WahaCallbackBadRequestError("invalid session")
        qr = payload.get("qr")
        if not isinstance(qr, dict) or not isinstance(qr.get("data"), str):
            raise WahaCallbackBadRequestError("invalid qr payload")
        image_base64 = qr["data"]
        if not image_base64:
            raise WahaCallbackBadRequestError("empty qr payload")
        assignment = await self._resolve_session(session_name, worker_id)
        if assignment is None:
            return "unknown_session"
        event_at = event_datetime(payload.get("timestamp"))
        raw_status = payload.get("status", WahaSessionStatus.SCAN_QR_CODE)
        if not isinstance(raw_status, str) or not raw_status:
            raw_status = WahaSessionStatus.SCAN_QR_CODE
        mapped = map_waha_session_status(raw_status)
        if mapped is None:
            return "unknown_status"
        outcome = await self._sessions.apply_lifecycle(
            session_name,
            raw_status=raw_status,
            mapped_status=mapped.value,
            event_at=event_at,
        )
        if outcome is None:
            return "unknown_session"
        _logger.info(
            "waha qr event session=%s worker=%s applied=%s seq=%s",
            session_name,
            worker_id,
            outcome.applied,
            outcome.event_seq,
        )
        return await self._deliver_qr(assignment, outcome, event_at, image_base64)

    async def _resolve_session(
        self, session_name: str, worker_id: str
    ) -> WahaSessionAssignment | None:
        """Resolve the session, enforcing worker ownership.

        Returns ``None`` for unknown sessions (safely ignored — retrying
        cannot help). A worker publishing events for a session assigned to a
        different worker is rejected (403), never silently ignored.
        """
        assignment = await self._sessions.get(session_name)
        if assignment is None:
            return None
        if assignment.worker_id != worker_id:
            raise WahaCallbackForbiddenError(
                "session is assigned to a different worker"
            )
        return assignment

    # --- reconciliation -----------------------------------------------------------

    async def reconcile_session(self, session_name: str) -> str:
        """Request a fresh ``get_status`` from the assigned agent.

        Recovery for missed events (never a polling loop): the command
        travels the normal queue; the returned status re-enters the state
        machine as a ``command_result`` event.
        """
        assignment = await self._sessions.get(session_name)
        if assignment is None:
            return "unknown_session"
        enqueued = await self._sessions.enqueue(
            session_name,
            worker_id=assignment.worker_id,
            action=WahaCommandAction.GET_STATUS,
        )
        return "ok" if enqueued else "already_pending"

    async def reconcile_connection(self, user_id: str) -> str:
        """Backfill a freshly created connection row from persisted state.

        Called by onboarding right after it saves the ``whatsapp_connections``
        row: lifecycle events that arrived before the row existed (i.e.
        before the create command completed) are applied to it now, and any
        still-pending effects run — if WORKING already arrived, onboarding
        completes correctly.
        """
        assignment = await self._sessions.get_by_user(user_id)
        if assignment is None:
            return "no_assignment"
        if (
            assignment.raw_status is None
            or assignment.last_status_at is None
            or assignment.status == "provisioning"
        ):
            return "no_lifecycle_state"
        return await self._apply_and_run_effects(
            assignment,
            raw_status=assignment.raw_status,
            event_at=assignment.last_status_at,
        )

    # --- shared state machine -------------------------------------------------------

    async def _apply_and_run_effects(
        self,
        assignment: WahaSessionAssignment,
        *,
        raw_status: str,
        event_at: datetime,
    ) -> str:
        mapped = map_waha_session_status(raw_status)
        if mapped is None:
            _logger.info(
                "waha event unknown status session=%s raw_status=%s",
                assignment.session_name,
                raw_status,
            )
            return "unknown_status"

        outcome = await self._sessions.apply_lifecycle(
            assignment.session_name,
            raw_status=raw_status,
            mapped_status=mapped.value,
            event_at=event_at,
        )
        if outcome is None:
            return "unknown_session"

        _logger.info(
            "waha lifecycle session=%s worker=%s raw_status=%s status=%s "
            "applied=%s previous=%s connection_updated=%s seq=%s",
            assignment.session_name,
            assignment.worker_id,
            raw_status,
            outcome.status,
            outcome.applied,
            outcome.previous_status,
            outcome.connection_updated,
            outcome.event_seq,
        )

        if outcome.pending_effect is None:
            return "ok"
        return await self._execute_pending_effect(assignment, outcome)

    async def _execute_pending_effect(
        self,
        assignment: WahaSessionAssignment,
        outcome: WahaTransitionOutcome,
    ) -> str:
        effect = outcome.pending_effect
        effect_at = outcome.pending_effect_at
        if effect is None or effect_at is None:
            return "ok"

        if self._hooks is None:
            # No onboarding hooks wired (e.g. a worker-only deployment):
            # nothing can execute user-facing effects — clear them so they
            # cannot block the state machine or poison QR bookkeeping.
            await self._sessions.clear_pending_effect(
                assignment.session_name, effect=effect, effect_at=effect_at
            )
            return "ok"

        hooks = self._hooks
        if effect == WahaLifecycleEffect.CONFIRM_WORKING:
            await self._run_effect(
                lambda: hooks.handle_connection_established_by_id(
                    assignment.user_id
                ),
                assignment, outcome, effect, effect_at,
            )
            return "ok"

        if effect == WahaLifecycleEffect.NOTIFY_DISCONNECTED:
            await self._run_effect(
                lambda: hooks.handle_disconnect_notification(
                    assignment.user_id
                ),
                assignment, outcome, effect, effect_at,
            )
            return "ok"

        if effect == WahaLifecycleEffect.DELIVER_QR:
            # A session_status event has no image to deliver — the QR
            # arrives as its own qr_ready event, which calls _deliver_qr
            # directly. This branch is reached only on its retries.
            return "ok"

        _logger.warning(
            "waha unknown pending effect session=%s effect=%s — clearing",
            assignment.session_name,
            effect,
        )
        # An effect value this version does not know (e.g. written by a
        # newer deployment) must not wedge the row forever: clear it.
        await self._sessions.clear_pending_effect(
            assignment.session_name, effect=effect, effect_at=effect_at
        )
        return "ok"

    async def _run_effect(
        self,
        action: Any,
        assignment: WahaSessionAssignment,
        outcome: WahaTransitionOutcome,
        effect: str,
        effect_at: datetime,
    ) -> None:
        """Execute a user-facing effect and clear it once it succeeds.

        A failure raises :class:`WahaCallbackRetryError` — the effect stays
        queued and is re-run when the agent retries the event, while the
        already-committed lifecycle state is untouched.
        """
        try:
            await action()
        except Exception as exc:
            raise WahaCallbackRetryError(
                f"lifecycle effect {effect} failed for session "
                f"{assignment.session_name}"
            ) from exc
        await self._sessions.clear_pending_effect(
            assignment.session_name, effect=effect, effect_at=effect_at
        )

    async def _deliver_qr(
        self,
        assignment: WahaSessionAssignment,
        outcome: WahaTransitionOutcome,
        event_at: datetime,
        image_base64: str,
    ) -> str:
        """Guard-check and deliver one agent-provided QR image.

        Guards (QR concurrency): the QR is delivered and recorded only when
        the session is still pairing, the image was not already delivered,
        and the event is still the current generation — a delayed QR never
        overwrites a newer QR or resurrects pairing state after WORKING.
        """
        qr_hash = hashlib.sha256(image_base64.encode("ascii")).hexdigest()
        current = await self._sessions.get(assignment.session_name)
        if current is None:
            return "unknown_session"
        if current.status != "pairing_required" or current.raw_status != (
            WahaSessionStatus.SCAN_QR_CODE
        ):
            _logger.info(
                "waha QR discarded: state advanced session=%s status=%s",
                assignment.session_name,
                current.status,
            )
            return "qr_superseded"
        if current.last_qr_hash == qr_hash:
            # Same image already delivered — repeated event / idempotent
            # refresh: refresh the stored TTL and clear any re-queued effect.
            await self._sessions.record_qr(
                assignment.session_name,
                qr_hash=qr_hash,
                qr_image=image_base64,
                effect_at=event_at,
            )
            return "qr_duplicate"
        if not (
            outcome.applied
            or (
                current.pending_effect == WahaLifecycleEffect.DELIVER_QR
                and current.pending_effect_at == event_at
            )
        ):
            # A newer lifecycle event owns the state (and the newer QR).
            _logger.info(
                "waha QR discarded: newer event session=%s seq=%s",
                assignment.session_name,
                current.event_seq,
            )
            return "qr_superseded"

        hooks = self._hooks
        if hooks is not None:
            try:
                await hooks.deliver_pairing_qr(assignment.user_id, image_base64)
            except Exception as exc:
                raise WahaCallbackRetryError(
                    f"QR delivery failed for session {assignment.session_name}"
                ) from exc
        recorded = await self._sessions.record_qr(
            assignment.session_name,
            qr_hash=qr_hash,
            qr_image=image_base64,
            effect_at=event_at,
        )
        if not recorded:
            # Superseded between the re-check and the record — the QR was
            # delivered but a newer event now owns the state. Harmless.
            _logger.info(
                "waha QR record superseded session=%s", assignment.session_name
            )
        return "ok"
