"""Unit tests for the WAHA provisioner (control-plane, no HTTP to WAHA)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.domain.waha import (
    WahaAllocationError,
    WahaCommandAction,
    WahaSessionAssignment,
)
from echo_v2.integrations.waha.provisioner import WahaProvisioner
from echo_v2.integrations.waha.settings import WahaSettings
from echo_v2.ports.whatsapp import (
    ConnectionConfig,
    ConnectionRef,
    ConnectionStatus,
    PairingOutcome,
)
from echo_v2.runtime.errors import PermanentError

USER = "11111111-111-1111-1111-111111111111"
_CONFIG = ConnectionConfig(
    webhook_url="https://echo.example/webhooks/whatsapp/green",
    webhook_token="tok",
    user_id=USER,
)

_SETTINGS = WahaSettings(
    onboarding_enabled=True,
    heartbeat_timeout_seconds=90.0,
    command_lease_seconds=120.0,
    command_max_attempts=5,
    command_poll_limit=10,
    qr_ttl_seconds=120.0,
)


class FakeRepo:
    def __init__(self, *, fail_allocate: bool = False) -> None:
        self.fail_allocate = fail_allocate
        self.assignment = WahaSessionAssignment(
            session_name="echo-0001",
            user_id=USER,
            worker_id="waha-1",
            status="provisioning",
        )
        self.enqueued: list[tuple[str, str]] = []

    async def allocate_and_enqueue_create(
        self, user_id: str, *, heartbeat_timeout_seconds: float = 90.0
    ) -> WahaSessionAssignment:
        if self.fail_allocate:
            raise WahaAllocationError("no_capacity")
        return self.assignment

    async def get(self, session_name: str):
        if session_name == self.assignment.session_name:
            return self.assignment
        return None

    async def get_by_user(self, user_id: str):
        return self.assignment if user_id == USER else None

    async def enqueue(
        self, session_name: str, *, worker_id: str, action: str
    ) -> bool:
        if (session_name, action) in self.enqueued:
            return False
        self.enqueued.append((session_name, action))
        return True


def _provisioner(
    repo: FakeRepo | None = None,
) -> tuple[WahaProvisioner, FakeRepo]:
    repo = repo or FakeRepo()
    return (
        WahaProvisioner(sessions=repo, settings=_SETTINGS),  # type: ignore[arg-type]
        repo,
    )


# --- create_connection ---------------------------------------------------------


async def test_create_connection_requires_user_id() -> None:
    provisioner, _repo = _provisioner()
    with pytest.raises(PermanentError, match="user_id"):
        await provisioner.create_connection(
            ConnectionConfig(webhook_url="u", webhook_token="t")
        )


async def test_create_connection_allocates_and_enqueues_command() -> None:
    provisioner, repo = _provisioner()
    created = await provisioner.create_connection(_CONFIG)
    assert created.ref.provider == "waha"
    assert created.ref.provider_connection_id == "echo-0001"
    assert created.credentials.data == b""
    assert repo.enqueued == []  # the atomic allocate path enqueued create itself


async def test_create_connection_allocation_failure_propagates() -> None:
    provisioner, _repo = _provisioner(FakeRepo(fail_allocate=True))
    with pytest.raises(WahaAllocationError):
        await provisioner.create_connection(_CONFIG)


# --- get_status (persisted state, no I/O) ---------------------------------------


async def test_get_status_serves_persisted_state() -> None:
    provisioner, repo = _provisioner()
    repo.assignment = replace(
        repo.assignment,
        status="connected",
        raw_status="WORKING",
    )
    snapshot = await provisioner.get_status(ConnectionRef("waha", "echo-0001"))
    assert snapshot.status is ConnectionStatus.CONNECTED
    assert snapshot.provider_raw_status == "WORKING"


async def test_get_status_without_assignment_fails() -> None:
    provisioner, _repo = _provisioner()
    with pytest.raises(PermanentError, match="no WAHA assignment"):
        await provisioner.get_status(ConnectionRef("waha", "echo-nope"))


# --- get_pairing_qr (on-demand, command-enqueuing) ---------------------------------


async def test_get_pairing_qr_already_authorized() -> None:
    provisioner, repo = _provisioner()
    repo.assignment = replace(
        repo.assignment, status="connected", raw_status="WORKING"
    )
    result = await provisioner.get_pairing_qr(ConnectionRef("waha", "echo-0001"))
    assert result.outcome is PairingOutcome.ALREADY_AUTHORIZED


async def test_get_pairing_qr_stopped_enqueues_start_once() -> None:
    provisioner, repo = _provisioner()
    repo.assignment = replace(
        repo.assignment, status="pairing_required", raw_status="STOPPED"
    )
    result = await provisioner.get_pairing_qr(ConnectionRef("waha", "echo-0001"))
    assert result.outcome is PairingOutcome.PREPARING
    assert repo.enqueued == [("echo-0001", WahaCommandAction.START)]

    # Cooldown: a second immediate attempt does not enqueue another start.
    result = await provisioner.get_pairing_qr(ConnectionRef("waha", "echo-0001"))
    assert result.outcome is PairingOutcome.PREPARING
    assert repo.enqueued.count(("echo-0001", WahaCommandAction.START)) == 1


async def test_get_pairing_qr_enqueues_get_qr_when_pairing() -> None:
    provisioner, repo = _provisioner()
    repo.assignment = replace(
        repo.assignment, status="pairing_required", raw_status="SCAN_QR_CODE"
    )
    result = await provisioner.get_pairing_qr(ConnectionRef("waha", "echo-0001"))
    assert result.outcome is PairingOutcome.PREPARING
    assert repo.enqueued == [("echo-0001", WahaCommandAction.GET_QR)]

    # Already-active command → no duplicate enqueue.
    result = await provisioner.get_pairing_qr(ConnectionRef("waha", "echo-0001"))
    assert result.outcome is PairingOutcome.PREPARING
    assert repo.enqueued == [("echo-0001", WahaCommandAction.GET_QR)]


async def test_get_pairing_qr_returns_fresh_stored_qr() -> None:
    provisioner, repo = _provisioner()
    repo.assignment = replace(
        repo.assignment,
        status="pairing_required",
        raw_status="SCAN_QR_CODE",
        last_qr_image="cXItMQ==",
        qr_updated_at=datetime.now(timezone.utc),
    )
    result = await provisioner.get_pairing_qr(ConnectionRef("waha", "echo-0001"))
    assert result.outcome is PairingOutcome.QR_READY
    assert result.qr is not None
    assert result.qr.image_base64 == "cXItMQ=="


async def test_get_pairing_qr_expired_stored_qr_not_resent() -> None:
    provisioner, repo = _provisioner()
    repo.assignment = replace(
        repo.assignment,
        status="pairing_required",
        raw_status="SCAN_QR_CODE",
        last_qr_image="cXItMQ==",
        qr_updated_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    result = await provisioner.get_pairing_qr(ConnectionRef("waha", "echo-0001"))
    assert result.outcome is PairingOutcome.PREPARING
    assert repo.enqueued == [("echo-0001", WahaCommandAction.GET_QR)]


async def test_get_pairing_qr_provisioning_enqueues_get_qr() -> None:
    provisioner, repo = _provisioner()
    result = await provisioner.get_pairing_qr(ConnectionRef("waha", "echo-0001"))
    assert result.outcome is PairingOutcome.PREPARING
    assert repo.enqueued == [("echo-0001", WahaCommandAction.GET_QR)]


async def test_get_pairing_qr_without_assignment_fails() -> None:
    provisioner, _repo = _provisioner()
    with pytest.raises(PermanentError, match="no WAHA assignment"):
        await provisioner.get_pairing_qr(ConnectionRef("waha", "echo-nope"))


# --- unsupported operations -------------------------------------------------------


async def test_unsupported_operations_raise() -> None:
    provisioner, _repo = _provisioner()
    ref = ConnectionRef("waha", "echo-0001")
    with pytest.raises(PermanentError, match="not supported"):
        await provisioner.unpair(ref)
    with pytest.raises(PermanentError, match="not supported"):
        await provisioner.delete_connection(ref)
    # configure_connection is a deliberate no-op (the agent configures the
    # local lifecycle webhook at session creation).
    await provisioner.configure_connection(ref, _CONFIG)


# --- ProvisionerRouter ------------------------------------------------------------


class DefaultSpy:
    def __init__(self) -> None:
        self.configured: list[ConnectionRef] = []
        self.unpaired: list[ConnectionRef] = []
        self.deleted: list[ConnectionRef] = []
        self.created = 0

    async def create_connection(self, config):
        self.created += 1
        from echo_v2.ports.whatsapp import CreatedConnection

        return CreatedConnection(
            ref=ConnectionRef("baileys", "default-conn"), credentials=b""
        )

    async def configure_connection(self, connection, config) -> None:
        self.configured.append(connection)

    async def get_status(self, connection):
        raise AssertionError("not under test")

    async def get_pairing_qr(self, connection):
        raise AssertionError("not under test")

    async def unpair(self, connection) -> None:
        self.unpaired.append(connection)

    async def delete_connection(self, connection) -> None:
        self.deleted.append(connection)


async def test_router_routes_non_create_ops_by_provider() -> None:
    from echo_v2.services.provisioner_router import ProvisionerRouter

    default = DefaultSpy()
    provisioner, _repo = _provisioner()
    router = ProvisionerRouter(
        default=default,  # type: ignore[arg-type]
        waha=provisioner,
        waha_onboarding_enabled=False,
    )
    baileys_ref = ConnectionRef("baileys", "c1")
    waha_ref = ConnectionRef("waha", "echo-0001")

    await router.configure_connection(baileys_ref, _CONFIG)
    await router.unpair(baileys_ref)
    await router.delete_connection(baileys_ref)
    assert default.configured == [baileys_ref]
    assert default.unpaired == [baileys_ref]
    assert default.deleted == [baileys_ref]

    # WAHA refs route to the WAHA provisioner regardless of the flag.
    await router.configure_connection(waha_ref, _CONFIG)  # no-op
    with pytest.raises(PermanentError, match="not supported"):
        await router.unpair(waha_ref)
    with pytest.raises(PermanentError, match="not supported"):
        await router.delete_connection(waha_ref)


async def test_router_create_gated_by_flag() -> None:
    from echo_v2.services.provisioner_router import ProvisionerRouter

    # Flag off → default provider (existing behavior preserved).
    default = DefaultSpy()
    provisioner, repo = _provisioner()
    router = ProvisionerRouter(
        default=default,  # type: ignore[arg-type]
        waha=provisioner,
        waha_onboarding_enabled=False,
    )
    await router.create_connection(_CONFIG)
    assert default.created == 1
    assert repo.enqueued == []

    # Flag on → WAHA (command enqueued via the atomic allocation).
    default = DefaultSpy()
    router = ProvisionerRouter(
        default=default,  # type: ignore[arg-type]
        waha=provisioner,
        waha_onboarding_enabled=True,
    )
    created = await router.create_connection(_CONFIG)
    assert default.created == 0
    assert created.ref.provider == "waha"
