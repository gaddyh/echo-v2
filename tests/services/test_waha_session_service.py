"""Unit tests for the WAHA session lifecycle service (Phase 3, agent-based).

Covers the agent event matrix (command_result / session_status / qr_ready),
worker ownership, duplicate / out-of-order / early events, retry-safe
pending effects, QR concurrency (delayed QR vs newer events / WORKING), the
command queue surface the service uses, and the reconciliation paths. Uses
an in-memory repository that faithfully mirrors the Postgres fence and
queue semantics.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

import pytest

from echo_v2.domain.waha import (
    WahaCommand,
    WahaCommandAction,
    WahaLifecycleEffect,
    WahaSessionAssignment,
    WahaTransitionOutcome,
)
from echo_v2.integrations.waha.settings import WahaSettings
from echo_v2.services.waha_session_service import (
    WahaCallbackBadRequestError,
    WahaCallbackForbiddenError,
    WahaCallbackRetryError,
    WahaSessionService,
    event_datetime,
)

USER = "11111111-1111-1111-1111-111111111111"
WORKER = "waha-1"
OTHER_WORKER = "waha-2"

_EPOCH = 1764177600.0


# --- fakes --------------------------------------------------------------------


class FakeRepo:
    """In-memory repository mirroring the Postgres fences + command queue."""

    def __init__(self) -> None:
        self.rows: dict[str, WahaSessionAssignment] = {}
        self._by_user: dict[str, str] = {}
        self.commands: dict[str, WahaCommand] = {}
        self._lock = asyncio.Lock()

    async def allocate_and_enqueue_create(
        self, user_id: str, *, heartbeat_timeout_seconds: float = 90.0
    ) -> WahaSessionAssignment:
        async with self._lock:
            existing = self._by_user.get(user_id)
            if existing:
                assignment = self.rows[existing]
            else:
                assignment = WahaSessionAssignment(
                    session_name=f"echo-{len(self.rows) + 1:04d}",
                    user_id=user_id,
                    worker_id=WORKER,
                    status="provisioning",
                )
                self.rows[assignment.session_name] = assignment
                self._by_user[user_id] = assignment.session_name
            await self._enqueue_locked(
                assignment.session_name, WORKER, WahaCommandAction.CREATE_AND_START
            )
            return assignment

    async def get(self, session_name: str) -> WahaSessionAssignment | None:
        return self.rows.get(session_name)

    async def get_by_user(self, user_id: str) -> WahaSessionAssignment | None:
        name = self._by_user.get(user_id)
        return self.rows.get(name) if name else None

    async def _enqueue_locked(self, session_name: str, worker_id: str, action: str) -> bool:
        for command in self.commands.values():
            if (
                command.session_name == session_name
                and command.action == action
                and command.status in ("pending", "claimed")
            ):
                return False
        command = WahaCommand(
            command_id=str(uuid.uuid4()),
            worker_id=worker_id,
            session_name=session_name,
            action=action,
            status="pending",
            attempts=0,
        )
        self.commands[command.command_id] = command
        return True

    async def enqueue(
        self, session_name: str, *, worker_id: str, action: str
    ) -> bool:
        async with self._lock:
            return await self._enqueue_locked(session_name, worker_id, action)

    async def claim_for_worker(
        self,
        worker_id: str,
        *,
        limit: int = 10,
        lease_seconds: float = 120.0,
        max_attempts: int = 5,
    ) -> list[WahaCommand]:
        async with self._lock:
            claimed: list[WahaCommand] = []
            now = datetime.now(timezone.utc)
            for command in self.commands.values():
                if command.status == "failed":
                    continue
                if command.attempts >= max_attempts:
                    self.commands[command.command_id] = replace(
                        command, status="failed"
                    )
                    continue
                if command.worker_id != worker_id:
                    continue
                if command.status == "pending" or (
                    command.status == "claimed"
                    and command.lease_expires_at is not None
                    and command.lease_expires_at < now
                ):
                    updated = replace(
                        command,
                        status="claimed",
                        claimed_at=now,
                        lease_expires_at=now.replace(
                            microsecond=now.microsecond
                        ),
                        attempts=command.attempts + 1,
                    )
                    self.commands[command.command_id] = updated
                    claimed.append(updated)
                    if len(claimed) >= limit:
                        break
            return claimed

    async def complete_command(
        self,
        command_id: str,
        *,
        worker_id: str,
        success: bool,
        error: str | None = None,
    ) -> bool:
        async with self._lock:
            command = self.commands.get(command_id)
            if command is None:
                return False
            if command.worker_id != worker_id:
                return False
            if command.status in ("completed", "failed"):
                return True
            self.commands[command_id] = replace(
                command,
                status="completed" if success else "failed",
                last_error=error,
            )
            return True

    async def active_commands(self, session_name: str) -> list[WahaCommand]:
        return [
            c
            for c in self.commands.values()
            if c.session_name == session_name
            and c.status in ("pending", "claimed")
        ]

    async def apply_lifecycle(
        self,
        session_name: str,
        *,
        raw_status: str,
        mapped_status: str,
        event_at: datetime,
        account_phone: str | None = None,
    ) -> WahaTransitionOutcome | None:
        async with self._lock:
            row = self.rows.get(session_name)
            if row is None:
                return None
            stored_at = row.last_status_at
            stale = stored_at is not None and event_at < stored_at
            identical = (
                not stale
                and row.status == mapped_status
                and row.raw_status == raw_status
                and stored_at == event_at
            )
            if stale or identical:
                return WahaTransitionOutcome(
                    applied=False,
                    session_name=row.session_name,
                    status=row.status,
                    raw_status=row.raw_status,
                    previous_status=row.status,
                    event_seq=row.event_seq,
                    event_at=stored_at,
                    pending_effect=row.pending_effect,
                    pending_effect_at=row.pending_effect_at,
                    connection_updated=True,
                )
            effect = _effect_for(raw_status)
            if raw_status == "WORKING":
                identity_update = (
                    {"account_phone": account_phone, "account_identity_at": event_at}
                    if account_phone
                    else {}
                )
            else:
                identity_update = {"account_phone": None, "account_identity_at": None}
            updated = replace(
                row,
                status=mapped_status,
                raw_status=raw_status,
                last_status_at=event_at,
                event_seq=row.event_seq + 1,
                pending_effect=effect,
                pending_effect_at=event_at if effect else None,
                **identity_update,
            )
            self.rows[session_name] = updated
            return WahaTransitionOutcome(
                applied=True,
                session_name=session_name,
                status=mapped_status,
                raw_status=raw_status,
                previous_status=row.status,
                event_seq=updated.event_seq,
                event_at=event_at,
                pending_effect=effect,
                pending_effect_at=effect and event_at,
                connection_updated=True,
            )

    async def clear_pending_effect(
        self, session_name: str, *, effect: str, effect_at: datetime
    ) -> bool:
        async with self._lock:
            row = self.rows.get(session_name)
            if (
                row is None
                or row.pending_effect != effect
                or row.pending_effect_at != effect_at
            ):
                return False
            self.rows[session_name] = replace(
                row, pending_effect=None, pending_effect_at=None
            )
            return True

    async def record_qr(
        self,
        session_name: str,
        *,
        qr_hash: str,
        qr_image: str | None = None,
        effect_at: datetime,
    ) -> bool:
        async with self._lock:
            row = self.rows.get(session_name)
            if row is None:
                return False
            if (
                row.pending_effect != WahaLifecycleEffect.DELIVER_QR
                or row.pending_effect_at != effect_at
            ):
                return False
            self.rows[session_name] = replace(
                row,
                last_qr_hash=qr_hash,
                last_qr_image=qr_image,
                qr_updated_at=datetime.now(timezone.utc),
                pending_effect=None,
                pending_effect_at=None,
            )
            return True

    async def allocated_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.rows.values():
            counts[row.worker_id] = counts.get(row.worker_id, 0) + 1
        return counts


def _effect_for(raw_status: str) -> str | None:
    if raw_status == "WORKING":
        return WahaLifecycleEffect.CONFIRM_WORKING
    if raw_status == "SCAN_QR_CODE":
        return WahaLifecycleEffect.DELIVER_QR
    if raw_status in ("STOPPED", "FAILED"):
        return WahaLifecycleEffect.NOTIFY_DISCONNECTED
    return None


@dataclass
class HooksRecorder:
    established: list[str] = field(default_factory=list)
    disconnected: list[str] = field(default_factory=list)
    qrs: list[tuple[str, str]] = field(default_factory=list)
    fail_established: bool = False
    fail_qr: bool = False

    async def handle_connection_established_by_id(self, user_id: str) -> None:
        if self.fail_established:
            raise RuntimeError("welcome send failed")
        self.established.append(user_id)

    async def handle_disconnect_notification(self, user_id: str) -> None:
        self.disconnected.append(user_id)

    async def deliver_pairing_qr(self, user_id: str, image_base64: str) -> None:
        if self.fail_qr:
            raise RuntimeError("qr send failed")
        self.qrs.append((user_id, image_base64))


_SETTINGS = WahaSettings(
    onboarding_enabled=True,
    heartbeat_timeout_seconds=90.0,
    command_lease_seconds=120.0,
    command_max_attempts=5,
    command_poll_limit=10,
    qr_ttl_seconds=120.0,
)


def _service(
    repo: FakeRepo, hooks: HooksRecorder | None = None
) -> WahaSessionService:
    return WahaSessionService(
        sessions=repo,  # type: ignore[arg-type]
        settings=_SETTINGS,
        hooks=hooks,
    )


async def _assigned(repo: FakeRepo) -> WahaSessionAssignment:
    return await repo.allocate_and_enqueue_create(USER)


def _event(
    event_type: str,
    session: str,
    *,
    status: str | None = None,
    timestamp: float | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "worker_id": WORKER,
        "session": session,
        "type": event_type,
    }
    if status is not None:
        payload["status"] = status
    if timestamp is not None:
        payload["timestamp"] = timestamp
    if extra:
        payload.update(extra)
    return payload


async def _send(
    service: WahaSessionService,
    payload: dict[str, Any],
    *,
    worker_id: str = WORKER,
) -> str:
    return await service.handle_agent_event(payload, worker_id=worker_id)


# --- session_status events -------------------------------------------------------


async def test_status_matrix_starting_working() -> None:
    repo = FakeRepo()
    assignment = await _assigned(repo)
    hooks = HooksRecorder()
    service = _service(repo, hooks)

    assert (
        await _send(
            service,
            _event(
                "session_status", assignment.session_name,
                status="STARTING", timestamp=_EPOCH,
            ),
        )
        == "ok"
    )
    row = await repo.get(assignment.session_name)
    assert row is not None and row.status == "connecting"

    assert (
        await _send(
            service,
            _event(
                "session_status", assignment.session_name,
                status="WORKING", timestamp=_EPOCH + 5,
            ),
        )
        == "ok"
    )
    row = await repo.get(assignment.session_name)
    assert row is not None and row.status == "connected"
    assert hooks.established == [USER]
    assert row.pending_effect is None


async def test_stopped_and_failed_map_to_pairing_required_with_notify() -> None:
    repo = FakeRepo()
    assignment = await _assigned(repo)
    hooks = HooksRecorder()
    service = _service(repo, hooks)
    await _send(
        service,
        _event("session_status", assignment.session_name, status="WORKING", timestamp=_EPOCH),
    )
    await _send(
        service,
        _event("session_status", assignment.session_name, status="STOPPED", timestamp=_EPOCH + 5),
    )
    row = await repo.get(assignment.session_name)
    assert row is not None and row.status == "pairing_required"
    assert hooks.disconnected == [USER]

    await _send(
        service,
        _event("session_status", assignment.session_name, status="FAILED", timestamp=_EPOCH + 9),
    )
    row = await repo.get(assignment.session_name)
    assert row is not None and row.status == "pairing_required"
    assert len(hooks.disconnected) == 2


async def test_unknown_status_is_ignored_without_state_change() -> None:
    repo = FakeRepo()
    assignment = await _assigned(repo)
    service = _service(repo)
    await _send(
        service,
        _event("session_status", assignment.session_name, status="WORKING", timestamp=_EPOCH),
    )
    outcome = await _send(
        service,
        _event(
            "session_status", assignment.session_name,
            status="SOME_FUTURE_STATUS", timestamp=_EPOCH + 5,
        ),
    )
    assert outcome == "unknown_status"
    row = await repo.get(assignment.session_name)
    assert row is not None and row.status == "connected"


async def test_unknown_session_is_safely_ignored() -> None:
    repo = FakeRepo()
    service = _service(repo)
    assert (
        await _send(
            service,
            _event("session_status", "echo-unknown", status="WORKING", timestamp=_EPOCH),
        )
        == "unknown_session"
    )


async def test_foreign_worker_event_is_rejected() -> None:
    """A worker may only publish events for sessions assigned to it."""
    repo = FakeRepo()
    assignment = await _assigned(repo)
    service = _service(repo)
    event = _event(
        "session_status", assignment.session_name, status="WORKING", timestamp=_EPOCH
    )
    event["worker_id"] = OTHER_WORKER  # claims the authenticated worker...
    with pytest.raises(WahaCallbackForbiddenError, match="different worker"):
        await _send(service, event, worker_id=OTHER_WORKER)  # ...but the session is not its
    row = await repo.get(assignment.session_name)
    assert row is not None and row.status == "provisioning"


async def test_body_worker_id_mismatch_is_rejected() -> None:
    repo = FakeRepo()
    assignment = await _assigned(repo)
    service = _service(repo)
    event = _event("session_status", assignment.session_name, status="WORKING", timestamp=_EPOCH)
    event["worker_id"] = OTHER_WORKER
    with pytest.raises(WahaCallbackForbiddenError, match="does not match"):
        await _send(service, event)


async def test_malformed_events_rejected() -> None:
    repo = FakeRepo()
    assignment = await _assigned(repo)
    service = _service(repo)
    with pytest.raises(WahaCallbackBadRequestError, match="type"):
        await _send(service, {"session": assignment.session_name})
    with pytest.raises(WahaCallbackBadRequestError, match="session"):
        await _send(service, {"type": "session_status"})
    with pytest.raises(WahaCallbackBadRequestError, match="status"):
        await _send(
            service,
            {"type": "session_status", "session": assignment.session_name},
        )
    with pytest.raises(WahaCallbackBadRequestError, match="qr"):
        await _send(
            service,
            {
                "type": "qr_ready",
                "session": assignment.session_name,
                "timestamp": _EPOCH,
            },
        )


async def test_unknown_event_type_is_ignored() -> None:
    repo = FakeRepo()
    assignment = await _assigned(repo)
    service = _service(repo)
    assert (
        await _send(
            service, _event("engine.event", assignment.session_name, timestamp=_EPOCH)
        )
        == "ignored_event"
    )


async def test_duplicate_event_does_not_duplicate_effects() -> None:
    repo = FakeRepo()
    assignment = await _assigned(repo)
    hooks = HooksRecorder()
    service = _service(repo, hooks)
    await _send(
        service,
        _event("session_status", assignment.session_name, status="WORKING", timestamp=_EPOCH),
    )
    outcome = await _send(
        service,
        _event("session_status", assignment.session_name, status="WORKING", timestamp=_EPOCH),
    )
    assert outcome == "ok"
    assert hooks.established == [USER]


async def test_out_of_order_events_apply_newest_only() -> None:
    repo = FakeRepo()
    assignment = await _assigned(repo)
    service = _service(repo)
    await _send(
        service,
        _event("session_status", assignment.session_name, status="WORKING", timestamp=_EPOCH + 5),
    )
    outcome = await _send(
        service,
        _event("session_status", assignment.session_name, status="STARTING", timestamp=_EPOCH),
    )
    assert outcome == "ok"
    row = await repo.get(assignment.session_name)
    assert row is not None and row.status == "connected"


async def test_working_before_connection_row_completes_onboarding() -> None:
    """Early-event scenario: WORKING arrives while the create command is
    still in flight (no whatsapp_connections row exists at all)."""
    repo = FakeRepo()
    assignment = await _assigned(repo)
    hooks = HooksRecorder()
    service = _service(repo, hooks)
    outcome = await _send(
        service,
        _event("session_status", assignment.session_name, status="WORKING", timestamp=_EPOCH),
    )
    assert outcome == "ok"
    row = await repo.get(assignment.session_name)
    assert row is not None and row.status == "connected"
    assert hooks.established == [USER]


async def test_failed_effect_is_retried_on_duplicate_event() -> None:
    """Persisting the transition and executing its effect are separate:
    a failed confirmation is retried even though the state is committed."""
    repo = FakeRepo()
    assignment = await _assigned(repo)
    hooks = HooksRecorder()
    service = _service(repo, hooks)

    hooks.fail_established = True
    with pytest.raises(WahaCallbackRetryError):
        await _send(
            service,
            _event("session_status", assignment.session_name, status="WORKING", timestamp=_EPOCH),
        )
    row = await repo.get(assignment.session_name)
    assert row is not None
    assert row.status == "connected"  # state committed…
    assert row.pending_effect == WahaLifecycleEffect.CONFIRM_WORKING  # …effect pending

    hooks.fail_established = False
    outcome = await _send(
        service,
        _event("session_status", assignment.session_name, status="WORKING", timestamp=_EPOCH),
    )
    assert outcome == "ok"
    assert hooks.established == [USER]
    row = await repo.get(assignment.session_name)
    assert row is not None and row.pending_effect is None


# --- qr_ready events ----------------------------------------------------------------


async def test_qr_ready_delivers_qr_and_records_hash_and_image() -> None:
    repo = FakeRepo()
    assignment = await _assigned(repo)
    hooks = HooksRecorder()
    service = _service(repo, hooks)
    outcome = await _send(
        service,
        _event(
            "qr_ready",
            assignment.session_name,
            status="SCAN_QR_CODE",
            timestamp=_EPOCH,
            extra={"qr": {"mimetype": "image/png", "data": "cXItMQ=="}},
        ),
    )
    assert outcome == "ok"
    assert hooks.qrs == [(USER, "cXItMQ==")]
    row = await repo.get(assignment.session_name)
    assert row is not None
    assert row.status == "pairing_required"
    assert row.last_qr_hash == hashlib.sha256(b"cXItMQ==").hexdigest()
    assert row.last_qr_image == "cXItMQ=="
    assert row.pending_effect is None


async def test_qr_refresh_delivers_each_new_image() -> None:
    repo = FakeRepo()
    assignment = await _assigned(repo)
    hooks = HooksRecorder()
    service = _service(repo, hooks)
    await _send(
        service,
        _event(
            "qr_ready", assignment.session_name, status="SCAN_QR_CODE",
            timestamp=_EPOCH,
            extra={"qr": {"mimetype": "image/png", "data": "cXItMQ=="}},
        ),
    )
    outcome = await _send(
        service,
        _event(
            "qr_ready", assignment.session_name, status="SCAN_QR_CODE",
            timestamp=_EPOCH + 10,
            extra={"qr": {"mimetype": "image/png", "data": "cXItMg=="}},
        ),
    )
    assert outcome == "ok"
    assert [image for _user, image in hooks.qrs] == ["cXItMQ==", "cXItMg=="]
    row = await repo.get(assignment.session_name)
    assert row is not None and row.last_qr_image == "cXItMg=="


async def test_duplicate_qr_image_not_resent() -> None:
    repo = FakeRepo()
    assignment = await _assigned(repo)
    hooks = HooksRecorder()
    service = _service(repo, hooks)
    await _send(
        service,
        _event(
            "qr_ready", assignment.session_name, status="SCAN_QR_CODE",
            timestamp=_EPOCH,
            extra={"qr": {"mimetype": "image/png", "data": "cXItMQ=="}},
        ),
    )
    outcome = await _send(
        service,
        _event(
            "qr_ready", assignment.session_name, status="SCAN_QR_CODE",
            timestamp=_EPOCH,  # same event retried
            extra={"qr": {"mimetype": "image/png", "data": "cXItMQ=="}},
        ),
    )
    assert outcome == "qr_duplicate"
    assert len(hooks.qrs) == 1

    # Newer timestamp, identical image (idempotent refresh) — no re-send.
    outcome = await _send(
        service,
        _event(
            "qr_ready", assignment.session_name, status="SCAN_QR_CODE",
            timestamp=_EPOCH + 10,
            extra={"qr": {"mimetype": "image/png", "data": "cXItMQ=="}},
        ),
    )
    assert outcome == "qr_duplicate"
    assert len(hooks.qrs) == 1


async def test_session_status_scan_queues_qr_effect_without_delivery() -> None:
    """A session_status SCAN_QR_CODE has no image — it queues the effect but
    does not deliver; the qr_ready event delivers."""
    repo = FakeRepo()
    assignment = await _assigned(repo)
    hooks = HooksRecorder()
    service = _service(repo, hooks)
    outcome = await _send(
        service,
        _event(
            "session_status", assignment.session_name,
            status="SCAN_QR_CODE", timestamp=_EPOCH,
        ),
    )
    assert outcome == "ok"
    assert hooks.qrs == []
    row = await repo.get(assignment.session_name)
    assert row is not None and row.status == "pairing_required"

    outcome = await _send(
        service,
        _event(
            "qr_ready", assignment.session_name, status="SCAN_QR_CODE",
            timestamp=_EPOCH,
            extra={"qr": {"mimetype": "image/png", "data": "cXItMQ=="}},
        ),
    )
    assert outcome == "ok"
    assert hooks.qrs == [(USER, "cXItMQ==")]


async def test_stale_qr_discarded_after_working() -> None:
    """A delayed QR must never resurrect pairing state after WORKING."""
    repo = FakeRepo()
    assignment = await _assigned(repo)
    hooks = HooksRecorder()
    service = _service(repo, hooks)
    await _send(
        service,
        _event(
            "qr_ready", assignment.session_name, status="SCAN_QR_CODE",
            timestamp=_EPOCH,
            extra={"qr": {"mimetype": "image/png", "data": "cXItMQ=="}},
        ),
    )
    await _send(
        service,
        _event("session_status", assignment.session_name, status="WORKING", timestamp=_EPOCH + 5),
    )
    # Old QR event arrives late — its timestamp predates WORKING.
    outcome = await _send(
        service,
        _event(
            "qr_ready", assignment.session_name, status="SCAN_QR_CODE",
            timestamp=_EPOCH + 1,
            extra={"qr": {"mimetype": "image/png", "data": "cXItMw=="}},
        ),
    )
    assert outcome == "qr_superseded"
    assert len(hooks.qrs) == 1
    row = await repo.get(assignment.session_name)
    assert row is not None and row.status == "connected"
    assert row.last_qr_image == "cXItMQ=="  # not overwritten


async def test_concurrent_qr_refreshes_only_newest_delivered() -> None:
    """Two qr_ready refreshes race: the older image is discarded by the
    event-generation re-check; only the newest QR is delivered."""
    repo = FakeRepo()
    assignment = await _assigned(repo)
    hooks = HooksRecorder()
    service = _service(repo, hooks)
    # First (older) event applies but its delivery is made to block…
    first_event = _event(
        "qr_ready", assignment.session_name, status="SCAN_QR_CODE",
        timestamp=_EPOCH,
        extra={"qr": {"mimetype": "image/png", "data": "cXItMQ=="}},
    )
    # …simulate the race: a newer event lands between apply and delivery.
    # Apply the first event's state transition directly (as the handler
    # would), then let a newer event supersede it before delivery.
    await repo.apply_lifecycle(
        assignment.session_name,
        raw_status="SCAN_QR_CODE",
        mapped_status="pairing_required",
        event_at=event_datetime(_EPOCH),
    )
    second = await _send(
        service,
        _event(
            "qr_ready", assignment.session_name, status="SCAN_QR_CODE",
            timestamp=_EPOCH + 10,
            extra={"qr": {"mimetype": "image/png", "data": "cXItMg=="}},
        ),
    )
    assert second == "ok"
    # Now the older event's handler runs its delivery phase — superseded.
    outcome = await service.handle_agent_event(first_event, worker_id=WORKER)
    assert outcome in ("qr_duplicate", "qr_superseded")
    assert [image for _user, image in hooks.qrs] == ["cXItMg=="]


async def test_qr_delivery_failure_raises_retry_and_stays_queued() -> None:
    repo = FakeRepo()
    assignment = await _assigned(repo)
    hooks = HooksRecorder()
    hooks.fail_qr = True
    service = _service(repo, hooks)
    with pytest.raises(WahaCallbackRetryError):
        await _send(
            service,
            _event(
                "qr_ready", assignment.session_name, status="SCAN_QR_CODE",
                timestamp=_EPOCH,
                extra={"qr": {"mimetype": "image/png", "data": "cXItMQ=="}},
            ),
        )
    row = await repo.get(assignment.session_name)
    assert row is not None
    assert row.pending_effect == WahaLifecycleEffect.DELIVER_QR

    hooks.fail_qr = False
    outcome = await _send(
        service,
        _event(
            "qr_ready", assignment.session_name, status="SCAN_QR_CODE",
            timestamp=_EPOCH,
            extra={"qr": {"mimetype": "image/png", "data": "cXItMQ=="}},
        ),
    )
    assert outcome == "ok"
    assert hooks.qrs == [(USER, "cXItMQ==")]


async def test_qr_event_for_foreign_worker_rejected() -> None:
    repo = FakeRepo()
    assignment = await _assigned(repo)
    service = _service(repo)
    with pytest.raises(WahaCallbackForbiddenError):
        await _send(
            service,
            _event(
                "qr_ready", assignment.session_name, status="SCAN_QR_CODE",
                timestamp=_EPOCH,
                extra={"qr": {"mimetype": "image/png", "data": "cXItMQ=="}},
            ),
            worker_id=OTHER_WORKER,
        )


# --- command_result events -------------------------------------------------------------


async def test_command_result_completes_and_processes_status_payload() -> None:
    repo = FakeRepo()
    assignment = await _assigned(repo)
    hooks = HooksRecorder()
    service = _service(repo, hooks)
    command = (await repo.active_commands(assignment.session_name))[0]
    await repo.claim_for_worker(WORKER)

    outcome = await _send(
        service,
        {
            "event_id": str(uuid.uuid4()),
            "worker_id": WORKER,
            "session": assignment.session_name,
            "type": "command_result",
            "command_id": command.command_id,
            "success": True,
            "status": "WORKING",
            "timestamp": _EPOCH,
        },
    )
    assert outcome == "ok"
    assert repo.commands[command.command_id].status == "completed"
    row = await repo.get(assignment.session_name)
    assert row is not None and row.status == "connected"
    assert hooks.established == [USER]


async def test_command_result_with_qr_payload_delivers_qr() -> None:
    repo = FakeRepo()
    assignment = await _assigned(repo)
    hooks = HooksRecorder()
    service = _service(repo, hooks)
    command = (await repo.active_commands(assignment.session_name))[0]
    await repo.claim_for_worker(WORKER)
    outcome = await _send(
        service,
        {
            "event_id": str(uuid.uuid4()),
            "worker_id": WORKER,
            "session": assignment.session_name,
            "type": "command_result",
            "command_id": command.command_id,
            "success": True,
            "qr": {"mimetype": "image/png", "data": "cXItMQ=="},
            "status": "SCAN_QR_CODE",
            "timestamp": _EPOCH,
        },
    )
    assert outcome == "ok"
    assert hooks.qrs == [(USER, "cXItMQ==")]


async def test_command_result_failure_marks_command_failed() -> None:
    repo = FakeRepo()
    assignment = await _assigned(repo)
    service = _service(repo)
    command = (await repo.active_commands(assignment.session_name))[0]
    outcome = await _send(
        service,
        {
            "event_id": str(uuid.uuid4()),
            "worker_id": WORKER,
            "session": assignment.session_name,
            "type": "command_result",
            "command_id": command.command_id,
            "success": False,
            "error": "waha create rejected the session",
        },
    )
    assert outcome == "ok"
    assert repo.commands[command.command_id].status == "failed"


async def test_command_result_duplicate_is_idempotent() -> None:
    repo = FakeRepo()
    assignment = await _assigned(repo)
    hooks = HooksRecorder()
    service = _service(repo, hooks)
    command = (await repo.active_commands(assignment.session_name))[0]
    event = {
        "event_id": str(uuid.uuid4()),
        "worker_id": WORKER,
        "session": assignment.session_name,
        "type": "command_result",
        "command_id": command.command_id,
        "success": True,
        "status": "WORKING",
        "timestamp": _EPOCH,
    }
    assert await _send(service, event) == "ok"
    assert await _send(service, event) == "ok"
    assert hooks.established == [USER]


async def test_command_result_for_foreign_worker_rejected() -> None:
    repo = FakeRepo()
    assignment = await _assigned(repo)
    service = _service(repo)
    command = (await repo.active_commands(assignment.session_name))[0]
    with pytest.raises(WahaCallbackForbiddenError):
        await _send(
            service,
            {
                "event_id": str(uuid.uuid4()),
                "worker_id": OTHER_WORKER,
                "session": assignment.session_name,
                "type": "command_result",
                "command_id": command.command_id,
                "success": True,
            },
            worker_id=OTHER_WORKER,
        )


async def test_command_result_unknown_command_rejected() -> None:
    repo = FakeRepo()
    assignment = await _assigned(repo)
    service = _service(repo)
    with pytest.raises(WahaCallbackForbiddenError, match="not claimable"):
        await _send(
            service,
            {
                "event_id": str(uuid.uuid4()),
                "worker_id": WORKER,
                "session": assignment.session_name,
                "type": "command_result",
                "command_id": str(uuid.uuid4()),
                "success": True,
            },
        )


async def test_command_result_malformed_rejected() -> None:
    repo = FakeRepo()
    assignment = await _assigned(repo)
    service = _service(repo)
    command = (await repo.active_commands(assignment.session_name))[0]
    with pytest.raises(WahaCallbackBadRequestError, match="success"):
        await _send(
            service,
            {
                "event_id": str(uuid.uuid4()),
                "worker_id": WORKER,
                "session": assignment.session_name,
                "type": "command_result",
                "command_id": command.command_id,
            },
        )
    with pytest.raises(WahaCallbackBadRequestError, match="command_id"):
        await _send(
            service,
            {
                "event_id": str(uuid.uuid4()),
                "worker_id": WORKER,
                "session": assignment.session_name,
                "type": "command_result",
                "success": True,
            },
        )


# --- command claiming + reconciliation ----------------------------------------------------


async def test_claim_commands_returns_wire_shape_and_leases() -> None:
    repo = FakeRepo()
    assignment = await _assigned(repo)
    service = _service(repo)
    commands = await service.claim_commands(WORKER)
    assert len(commands) == 1
    assert commands[0]["action"] == WahaCommandAction.CREATE_AND_START
    assert commands[0]["session"] == assignment.session_name
    assert commands[0]["command_id"]
    # Claiming does not remove: a second poll reclaims after lease expiry
    # (fake: lease already expired) but respects the attempt cap via the
    # repo semantics.
    again = await service.claim_commands(WORKER)
    assert len(again) == 1  # lease expired (fake) → re-delivered


async def test_reconcile_session_enqueues_get_status() -> None:
    repo = FakeRepo()
    assignment = await _assigned(repo)
    service = _service(repo)
    assert await service.reconcile_session(assignment.session_name) == "ok"
    actions = [c.action for c in repo.commands.values()]
    assert WahaCommandAction.GET_STATUS in actions
    assert await service.reconcile_session(assignment.session_name) == "already_pending"
    assert await service.reconcile_session("echo-nope") == "unknown_session"


async def test_reconcile_connection_backfills_and_completes_pending_effect() -> None:
    repo = FakeRepo()
    assignment = await _assigned(repo)
    hooks = HooksRecorder()
    hooks.fail_established = True
    service = _service(repo, hooks)
    with pytest.raises(WahaCallbackRetryError):
        await _send(
            service,
            _event("session_status", assignment.session_name, status="WORKING", timestamp=_EPOCH),
        )
    hooks.fail_established = False
    assert await service.reconcile_connection(USER) == "ok"
    assert hooks.established == [USER]


async def test_reconcile_connection_without_state() -> None:
    repo = FakeRepo()
    service = _service(repo)
    assert await service.reconcile_connection(USER) == "no_assignment"
    await _assigned(repo)
    assert await service.reconcile_connection(USER) == "no_lifecycle_state"
    assert await service.reconcile_connection("22222222-2222-2222-2222-222222222222") == "no_assignment"


async def test_effect_without_hooks_is_cleared() -> None:
    repo = FakeRepo()
    assignment = await _assigned(repo)
    service = _service(repo, hooks=None)
    assert (
        await _send(
            service,
            _event("session_status", assignment.session_name, status="WORKING", timestamp=_EPOCH),
        )
        == "ok"
    )
    row = await repo.get(assignment.session_name)
    assert row is not None and row.pending_effect is None


def test_event_datetime_parses_and_falls_back() -> None:
    assert event_datetime(1795718136.42).timestamp() == pytest.approx(1795718136.42, abs=1)
    assert event_datetime(None) is not None
    assert event_datetime("not-a-number") is not None


async def test_unknown_pending_effect_is_cleared_safely() -> None:
    """A bogus pending effect value (future migration) never wedges the
    state machine: it is logged and cleared."""
    repo = FakeRepo()
    assignment = await _assigned(repo)
    service = _service(repo, HooksRecorder())
    event_at = event_datetime(_EPOCH)
    await repo.apply_lifecycle(
        assignment.session_name,
        raw_status="SCAN_QR_CODE",
        mapped_status="pairing_required",
        event_at=event_at,
    )
    repo.rows[assignment.session_name] = replace(
        repo.rows[assignment.session_name],
        pending_effect="some_future_effect",
        pending_effect_at=event_at,
    )
    outcome = await _send(
        service,
        _event(
            "session_status", assignment.session_name,
            status="SCAN_QR_CODE", timestamp=_EPOCH,
        ),
    )
    assert outcome == "ok"
    row = await repo.get(assignment.session_name)
    assert row is not None and row.pending_effect is None
