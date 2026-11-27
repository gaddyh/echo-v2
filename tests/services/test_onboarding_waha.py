"""End-to-end WAHA onboarding tests (Phase 3, agent-based management).

Wires the real ``OnboardingService`` through the ``ProvisionerRouter`` to
the real ``WahaProvisioner`` / ``WahaSessionService`` with an in-memory
(connection-mirroring) session repository and a **FakeAgent** that polls the
command queue, "executes" commands against a simulated local WAHA, and
reports events — exactly the contract the UpCloud agent implements.

Covers: the complete onboarding flow (click → atomic allocate+command →
agent create → QR event → user scans → WORKING → connected + welcome),
events arriving before the connection record is created, restart of an
existing stopped session without re-creating, on-demand QR from the
short-lived stored image, and the flag-off deployment-safety path.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

import pytest

from echo_v2.domain.waha import (
    WahaCommandAction,
    WahaLifecycleEffect,
    WahaSessionAssignment,
    WahaTransitionOutcome,
)
from echo_v2.integrations.waha.provisioner import WahaProvisioner
from echo_v2.integrations.waha.settings import WahaSettings
from echo_v2.persistence.whatsapp_connections import (
    InMemoryWhatsAppConnectionRepository,
)
from echo_v2.ports.whatsapp import (
    ConnectionRef,
    ConnectionStatus,
    CreatedConnection,
    PairingOutcome,
    PairingResult,
)
from echo_v2.services.onboarding import OnboardingService
from echo_v2.services.provisioner_router import ProvisionerRouter
from echo_v2.services.waha_session_service import WahaSessionService

PHONE = "+972500000001"
USER = "user-1"


# --- fakes ----------------------------------------------------------------------


@dataclass
class FakeBot:
    sent: list[tuple[str, str]] = field(default_factory=list)
    sent_images: list[tuple[str, bytes, str | None]] = field(default_factory=list)
    sent_buttons: list[tuple[str, str, list[dict]]] = field(default_factory=list)

    async def send_text(self, user_phone: str, text: str) -> None:
        self.sent.append((user_phone, text))

    async def send_image(
        self,
        user_phone: str,
        *,
        image_bytes: bytes,
        mime_type: str,
        caption: str | None = None,
    ) -> str:
        self.sent_images.append((user_phone, image_bytes, caption))
        return "img-msg-id"

    async def send_template(
        self, user_phone: str, template_name: str, language: str, body_params: list[str]
    ) -> str:
        return "tpl-msg-id"

    async def send_buttons(
        self, user_phone: str, *, body_text: str, buttons: list[dict]
    ) -> str:
        self.sent_buttons.append((user_phone, body_text, buttons))
        return "btn-msg-id"


class FakeUserRepo:
    def __init__(self) -> None:
        self._users: dict[str, dict] = {}
        self._by_id: dict[str, str] = {}

    async def create_user(
        self,
        phone: str,
        *,
        timezone: str = "Asia/Jerusalem",
        first_name: str | None = None,
        onboarding_status: str = "pending",
    ) -> str:
        user_id = f"user-{len(self._users) + 1}"
        self._users[phone] = {
            "id": user_id,
            "status": onboarding_status,
            "name": first_name,
        }
        self._by_id[user_id] = phone
        return user_id

    async def get_by_phone(self, phone: str):
        user = self._users.get(phone)
        if not user:
            return None
        return (user["id"], user["status"], user["name"])

    async def update_onboarding_status(self, user_id: str, status: str) -> None:
        phone = self._by_id[user_id]
        self._users[phone]["status"] = status

    async def update_first_name(self, user_id: str, first_name: str) -> None:
        phone = self._by_id[user_id]
        self._users[phone]["name"] = first_name

    async def get_phone_by_id(self, user_id: str) -> str | None:
        return self._by_id.get(user_id)


def _effect_for(raw_status: str) -> str | None:
    if raw_status == "WORKING":
        return WahaLifecycleEffect.CONFIRM_WORKING
    if raw_status == "SCAN_QR_CODE":
        return WahaLifecycleEffect.DELIVER_QR
    if raw_status in ("STOPPED", "FAILED"):
        return WahaLifecycleEffect.NOTIFY_DISCONNECTED
    return None


class MirroringSessionRepo:
    """In-memory session+command repository that mirrors the Postgres
    semantics, including the whatsapp_connections mirror."""

    def __init__(self, connections: InMemoryWhatsAppConnectionRepository) -> None:
        self._connections = connections
        self.rows: dict[str, WahaSessionAssignment] = {}
        self._by_user: dict[str, str] = {}
        self.commands: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def allocate_and_enqueue_create(
        self, user_id: str, *, heartbeat_timeout_seconds: float = 90.0
    ) -> WahaSessionAssignment:
        async with self._lock:
            name = self._by_user.get(user_id)
            if name is None:
                name = f"echo-{len(self.rows) + 1:04d}"
                self.rows[name] = WahaSessionAssignment(
                    session_name=name,
                    user_id=user_id,
                    worker_id="waha-1",
                    status="provisioning",
                )
                self._by_user[user_id] = name
            await self._enqueue_locked(name, WahaCommandAction.CREATE_AND_START)
            return self.rows[name]

    async def get(self, session_name: str):
        return self.rows.get(session_name)

    async def get_by_user(self, user_id: str):
        name = self._by_user.get(user_id)
        return self.rows.get(name) if name else None

    async def _enqueue_locked(self, session_name: str, action: str) -> bool:
        for command in self.commands.values():
            if (
                command["session_name"] == session_name
                and command["action"] == action
                and command["status"] in ("pending", "claimed")
            ):
                return False
        self.commands[str(uuid.uuid4())] = {
            "worker_id": "waha-1",
            "session_name": session_name,
            "action": action,
            "status": "pending",
            "attempts": 0,
        }
        return True

    async def enqueue(
        self, session_name: str, *, worker_id: str, action: str
    ) -> bool:
        async with self._lock:
            return await self._enqueue_locked(session_name, action)

    async def claim_for_worker(
        self,
        worker_id: str,
        *,
        limit: int = 10,
        lease_seconds: float = 120.0,
        max_attempts: int = 5,
    ) -> list[Any]:
        from echo_v2.domain.waha import WahaCommand

        async with self._lock:
            claimed = []
            for command_id, command in self.commands.items():
                if command["worker_id"] != worker_id:
                    continue
                if command["status"] == "pending":
                    command["status"] = "claimed"
                    command["attempts"] += 1
                    claimed.append(
                        WahaCommand(
                            command_id=command_id,
                            worker_id=worker_id,
                            session_name=command["session_name"],
                            action=command["action"],
                            status="claimed",
                            attempts=command["attempts"],
                        )
                    )
            return claimed

    async def complete_command(
        self,
        command_id: str,
        *,
        worker_id: str,
        success: bool,
        error: str | None = None,
    ) -> bool:
        command = self.commands.get(command_id)
        if command is None or command["worker_id"] != worker_id:
            return False
        if command["status"] in ("completed", "failed"):
            return True
        command["status"] = "completed" if success else "failed"
        return True

    async def active_commands(self, session_name: str) -> list[Any]:
        return [
            c
            for c in self.commands.values()
            if c["session_name"] == session_name
            and c["status"] in ("pending", "claimed")
        ]

    async def apply_lifecycle(
        self,
        session_name: str,
        *,
        raw_status: str,
        mapped_status: str,
        event_at: datetime,
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
                connection_updated = await self._mirror(
                    row.session_name, row.status, row.raw_status
                )
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
                    connection_updated=connection_updated,
                )
            effect = _effect_for(raw_status)
            updated = replace(
                row,
                status=mapped_status,
                raw_status=raw_status,
                last_status_at=event_at,
                event_seq=row.event_seq + 1,
                pending_effect=effect,
                pending_effect_at=event_at if effect else None,
            )
            self.rows[session_name] = updated
            connection_updated = await self._mirror(
                session_name, mapped_status, raw_status
            )
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
                connection_updated=connection_updated,
            )

    async def _mirror(self, session_name: str, status: str, raw: str | None) -> bool:
        existing = await self._connections.get(ConnectionRef("waha", session_name))
        if existing is None:
            return False
        await self._connections.update_status(
            ConnectionRef("waha", session_name), ConnectionStatus(status), raw
        )
        return True

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
        return {}


class FakeWaha:
    """Simulated local WAHA on the VM, driven by the FakeAgent."""

    def __init__(self) -> None:
        self.sessions: set[str] = set()
        self.status: dict[str, str] = {}
        self.qr: dict[str, str] = {}
        self.create_calls = 0
        self.start_calls = 0

    def create_and_start(self, session: str) -> str:
        self.create_calls += 1
        if session not in self.sessions:
            self.sessions.add(session)
            self.status[session] = "STARTING"
        return self.status[session]

    def start(self, session: str) -> None:
        self.start_calls += 1
        self.status[session] = "SCAN_QR_CODE"
        self.qr[session] = "cXItMQ=="


class FakeAgent:
    """Polls the queue, executes against the FakeWaha, reports events."""

    def __init__(self, service: WahaSessionService, waha: FakeWaha) -> None:
        self._service = service
        self._waha = waha
        # Real "now" so emitted timestamps postdate any seeded state.
        self.clock = time.time()
        self.executed: list[str] = []

    async def pump(self) -> int:
        """One poll cycle: claim → execute → report results + events."""
        commands = await self._service.claim_commands("waha-1")
        for command in commands:
            await self.execute(command)
        return len(commands)

    async def execute(self, command: dict[str, str]) -> None:
        self.executed.append(command["action"])
        self.clock += 1.0
        session = command["session"]
        action = command["action"]
        result: dict[str, Any] = {
            "event_id": str(uuid.uuid4()),
            "worker_id": "waha-1",
            "session": session,
            "type": "command_result",
            "command_id": command["command_id"],
            "success": True,
            "timestamp": self.clock,
        }
        if action == WahaCommandAction.CREATE_AND_START:
            # Idempotent: reuse the session if it already exists.
            status = self._waha.create_and_start(session)
            await self._emit(session, status)
        elif action == WahaCommandAction.START:
            self._waha.start(session)
            await self._emit(session, "SCAN_QR_CODE")
            result["qr"] = {
                "mimetype": "image/png",
                "data": self._waha.qr[session],
            }
            result["status"] = "SCAN_QR_CODE"
        elif action == WahaCommandAction.GET_QR:
            result["qr"] = {
                "mimetype": "image/png",
                "data": self._waha.qr.get(session, "cXItMQ=="),
            }
            result["status"] = self._waha.status.get(session, "SCAN_QR_CODE")
        elif action == WahaCommandAction.GET_STATUS:
            result["status"] = self._waha.status.get(session, "STOPPED")
        # The result timestamp must postdate any events emitted during
        # execution (the same causal ordering the real agent reports).
        result["timestamp"] = self.clock
        await self._service.handle_agent_event(result, worker_id="waha-1")

    async def _emit(self, session: str, status: str) -> None:
        self.clock += 1.0
        event = {
            "event_id": str(uuid.uuid4()),
            "worker_id": "waha-1",
            "session": session,
            "type": "session_status",
            "status": status,
            "timestamp": self.clock,
        }
        await self._service.handle_agent_event(event, worker_id="waha-1")

    async def emit_qr(self, session: str, image: str) -> str:
        """Simulate the local WAHA webhook: SCAN_QR_CODE + QR image."""
        self.clock += 1.0
        return await self._service.handle_agent_event(
            {
                "event_id": str(uuid.uuid4()),
                "worker_id": "waha-1",
                "session": session,
                "type": "qr_ready",
                "status": "SCAN_QR_CODE",
                "qr": {"mimetype": "image/png", "data": image},
                "timestamp": self.clock,
            },
            worker_id="waha-1",
        )

    async def emit_status(self, session: str, status: str) -> str:
        self.clock += 1.0
        return await self._service.handle_agent_event(
            {
                "event_id": str(uuid.uuid4()),
                "worker_id": "waha-1",
                "session": session,
                "type": "session_status",
                "status": status,
                "timestamp": self.clock,
            },
            worker_id="waha-1",
        )


class DefaultProvisionerSpy:
    def __init__(self) -> None:
        self.create_calls = 0

    async def create_connection(self, config) -> CreatedConnection:
        self.create_calls += 1
        return CreatedConnection(
            ref=ConnectionRef("baileys", "default-conn"), credentials=b""
        )

    async def configure_connection(self, connection, config) -> None:
        return None

    async def get_status(self, connection) -> Any:
        from echo_v2.ports.whatsapp import ConnectionStatusSnapshot

        return ConnectionStatusSnapshot(
            status=ConnectionStatus.UNKNOWN, provider_raw_status=None
        )

    async def get_pairing_qr(self, connection) -> PairingResult:
        return PairingResult(outcome=PairingOutcome.TIMEOUT)

    async def unpair(self, connection) -> None:
        return None

    async def delete_connection(self, connection) -> None:
        return None


_SETTINGS = WahaSettings(
    onboarding_enabled=True,
    heartbeat_timeout_seconds=90.0,
    command_lease_seconds=120.0,
    command_max_attempts=5,
    command_poll_limit=10,
    qr_ttl_seconds=120.0,
)


class Harness:
    def __init__(self, *, onboarding_enabled: bool = True) -> None:
        self.bot = FakeBot()
        self.users = FakeUserRepo()
        self.connections = InMemoryWhatsAppConnectionRepository()
        self.sessions = MirroringSessionRepo(self.connections)
        self.waha = FakeWaha()
        self.provisioner = WahaProvisioner(
            sessions=self.sessions,  # type: ignore[arg-type]
            settings=_SETTINGS,
        )
        self.default = DefaultProvisionerSpy()
        self.router = ProvisionerRouter(
            default=self.default,  # type: ignore[arg-type]
            waha=self.provisioner,
            waha_onboarding_enabled=onboarding_enabled,
        )
        self.service = WahaSessionService(
            sessions=self.sessions,  # type: ignore[arg-type]
            settings=_SETTINGS,
        )
        self.agent = FakeAgent(self.service, self.waha)
        self.onboarding = OnboardingService(
            bot=self.bot,  # type: ignore[arg-type]
            user_repo=self.users,  # type: ignore[arg-type]
            connection_repo=self.connections,
            provisioner=self.router,  # type: ignore[arg-type]
            webhook_base_url="https://echo.example",
            poll_interval=0.01,
            poll_max_attempts=3,
            waha_reconciler=self.service,
        )
        self.service.bind_onboarding_hooks(self.onboarding)


async def _until(predicate, timeout: float = 3.0) -> None:
    async def _check() -> bool:
        result = predicate()
        if asyncio.iscoroutine(result):
            return await result
        return bool(result)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await _check():
            return
        await asyncio.sleep(0.01)
    assert await _check(), "condition not met before timeout"


async def _has_connection(h: Harness) -> bool:
    return await h.connections.get_by_user(USER) is not None


async def _connected(h: Harness) -> bool:
    row = await h.connections.get_by_user(USER)
    return row is not None and row.status is ConnectionStatus.CONNECTED


# --- tests -------------------------------------------------------------------------


async def test_complete_waha_onboarding_flow() -> None:
    h = Harness()
    await h.users.create_user(PHONE, first_name="Gaddy")

    # 1. User clicks [חבר אותי] → atomic allocation + create command →
    #    connection saved PROVISIONING → ready button.
    await h.onboarding.handle_onboarding_connect(PHONE)
    await _until(lambda: _has_connection(h))
    connection = await h.connections.get_by_user(USER)
    assert connection is not None
    assert connection.ref.provider == "waha"
    assert connection.status is ConnectionStatus.PROVISIONING
    assert h.default.create_calls == 0
    session_name = connection.ref.provider_connection_id

    # 2. The agent polls and executes create_and_start.
    assert await h.agent.pump() == 1
    assert h.waha.create_calls == 1
    assert (await h.sessions.get(session_name)).status == "connecting"

    # 3. WAHA produces a QR → agent forwards it → delivered to the user.
    h.waha.status[session_name] = "SCAN_QR_CODE"
    h.waha.qr[session_name] = "cXItMQ=="
    assert await h.agent.emit_qr(session_name, "cXItMQ==") == "ok"
    assert len(h.bot.sent_images) == 1
    phone, image, _caption = h.bot.sent_images[0]
    assert phone == PHONE
    assert image == b"qr-1"

    # 4. The user scans → agent forwards WORKING → connected + welcome.
    assert await h.agent.emit_status(session_name, "WORKING") == "ok"
    connection = await h.connections.get_by_user(USER)
    assert connection is not None
    assert connection.status is ConnectionStatus.CONNECTED
    assert connection.provider_raw_status == "WORKING"
    user = await h.users.get_by_phone(PHONE)
    assert user[1] == "active"
    assert any("Echo" in text for _phone, text in h.bot.sent)
    row = await h.sessions.get(session_name)
    assert row is not None and row.pending_effect is None


async def test_events_before_connection_record_completes_onboarding() -> None:
    """WORKING can arrive before the create command completes (before the
    whatsapp_connections record exists): the state is persisted in
    waha_sessions, and the onboarding save reconciles + completes."""
    h = Harness()
    await h.users.create_user(PHONE, first_name="Gaddy")

    # The agent races the onboarding flow: it executes the create command
    # (emitting WORKING) while create_connection is still returning —
    # before the connection row is saved.
    original_allocate = h.sessions.allocate_and_enqueue_create

    async def racing_allocate(user_id: str, **kwargs: Any):
        assignment = await original_allocate(user_id, **kwargs)
        existing = await h.connections.get(
            ConnectionRef("waha", assignment.session_name)
        )
        assert existing is None, "connection row must not exist yet"
        await h.agent.emit_status(assignment.session_name, "WORKING")
        return assignment

    h.sessions.allocate_and_enqueue_create = racing_allocate  # type: ignore[method-assign]

    await h.onboarding.handle_onboarding_connect(PHONE)
    await _until(lambda: _has_connection(h))

    # The connection row is reconciled to WORKING; no leftover
    # PROVISIONING, no ready button.
    await _until(lambda: _connected(h))
    connection = await h.connections.get_by_user(USER)
    assert connection is not None
    assert connection.status is ConnectionStatus.CONNECTED
    user = await h.users.get_by_phone(PHONE)
    assert user[1] == "active"
    assert any("Echo" in text for _phone, text in h.bot.sent)
    assert not any("QR" in b[1] for b in h.bot.sent_buttons)


async def test_qr_command_restarts_existing_session_without_creating() -> None:
    """A stopped session is started (never re-created) on explicit user
    action; the QR then arrives through the event path."""
    h = Harness()
    await h.users.create_user(PHONE, first_name="Gaddy")
    from echo_v2.persistence.whatsapp_connections import StoredConnection

    session_name = "echo-0001"
    await h.connections.save(
        StoredConnection(
            user_id=USER,
            ref=ConnectionRef("waha", session_name),
            credentials=b"",
            webhook_token_hash=b"",
            status=ConnectionStatus.PAIRING_REQUIRED,
            provider_raw_status="STOPPED",
        )
    )
    h.sessions.rows[session_name] = WahaSessionAssignment(
        session_name=session_name,
        user_id=USER,
        worker_id="waha-1",
        status="pairing_required",
        raw_status="STOPPED",
        last_status_at=datetime(2026, 11, 26, 17, 0, 0, tzinfo=timezone.utc),
    )
    h.sessions._by_user[USER] = session_name
    h.waha.sessions.add(session_name)
    h.waha.status[session_name] = "STOPPED"

    # 'qr' command → enqueue start (never create) → agent executes →
    # SCAN_QR_CODE + QR image → delivered to the user.
    assert await h.onboarding.handle_onboarding_qr(PHONE) is True
    await h.agent.pump()  # executes the START command → QR event
    await _until(lambda: len(h.bot.sent_images) == 1)
    assert h.waha.create_calls == 0
    assert h.waha.start_calls == 1
    phone, _image, _caption = h.bot.sent_images[0]
    assert phone == PHONE


async def test_on_demand_click_uses_stored_qr_without_agent_roundtrip() -> None:
    """After the event path delivered + stored a QR, an explicit [הצג QR]
    click re-sends the stored image (short-lived) without new commands."""
    h = Harness()
    await h.users.create_user(PHONE, first_name="Gaddy")
    await h.onboarding.handle_onboarding_connect(PHONE)
    await _until(lambda: _has_connection(h))
    connection = await h.connections.get_by_user(USER)
    assert connection is not None
    session_name = connection.ref.provider_connection_id
    h.waha.status[session_name] = "SCAN_QR_CODE"
    h.waha.qr[session_name] = "cXItMQ=="
    await h.agent.emit_qr(session_name, "cXItMQ==")
    assert len(h.bot.sent_images) == 1

    # Explicit click → stored QR re-sent; no new agent commands.
    commands_before = len(h.sessions.commands)
    await h.onboarding.handle_onboarding_show_qr(PHONE)
    await _until(lambda: len(h.bot.sent_images) == 2)
    assert len(h.sessions.commands) == commands_before
    phone, image, _caption = h.bot.sent_images[1]
    assert phone == PHONE
    assert image == b"qr-1"


async def test_flag_off_routes_creates_to_default_provider() -> None:
    """Deployment safety: with WAHA onboarding disabled, new connections go
    to the default provider; existing WAHA connections still route to WAHA
    (manageable even when the flag is off)."""
    h = Harness(onboarding_enabled=False)
    await h.users.create_user(PHONE, first_name="Gaddy")

    await h.onboarding.handle_onboarding_connect(PHONE)
    await _until(lambda: h.default.create_calls == 1)
    connection = await h.connections.get_by_user(USER)
    assert connection is not None
    assert connection.ref.provider == "baileys"
    assert h.waha.create_calls == 0
    assert h.sessions.commands == {}

    # An existing WAHA connection still reaches the WAHA provisioner (the
    # default spy would return TIMEOUT instead of raising about the
    # missing assignment).
    from echo_v2.runtime.errors import PermanentError

    with pytest.raises(PermanentError, match="no WAHA assignment"):
        await h.router.get_pairing_qr(ConnectionRef("waha", "echo-nope"))
