"""WAHA account identity: extraction contract, ingestion plumbing,
verification semantics.

The identity contract (migration 0068): the agent reports the
provider-authenticated account identity (``me`` object from the local WAHA
session API) on WORKING session_status events and get_status command
results. Echo accepts only the phone-number JID user part — never a LID,
never user input — persists it atomically with the lifecycle transition,
clears it on any non-WORKING transition (re-pairing safety), and uses it
for child-phone verification. Missing identity leaves verification
pending, never verified.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from echo_v2.domain.waha import WahaSessionAssignment
from echo_v2.persistence.whatsapp_connections import StoredConnection
from echo_v2.ports.whatsapp import (
    ConnectionRef,
    ConnectionStatus,
    ConnectionStatusSnapshot,
    ProviderCredentials,
)
from echo_v2.services.onboarding import OnboardingService
from echo_v2.services.waha_session_service import extract_account_phone
from tests.services.test_waha_session_service import (
    USER,
    WORKER,
    FakeRepo,
    HooksRecorder,
    _event,
    _service,
)

# --- extract_account_phone: the wire contract --------------------------------


def test_extract_phone_from_me_id_c_us() -> None:
    payload = {"me": {"id": "972512217037@c.us", "pushName": "x"}}
    assert extract_account_phone(payload) == "972512217037"


def test_extract_phone_from_device_jid() -> None:
    payload = {"me": {"id": "", "jid": "972512217037:70@s.whatsapp.net"}}
    assert extract_account_phone(payload) == "972512217037"


def test_extract_prefers_id_over_jid() -> None:
    payload = {
        "me": {"id": "972512217037@c.us", "jid": "972512217037:70@s.whatsapp.net"}
    }
    assert extract_account_phone(payload) == "972512217037"


def test_lid_only_identity_yields_none() -> None:
    """A LID is opaque and can never be mapped back to a phone — identity
    is unavailable, verification must stay pending."""
    payload = {"me": {"id": "", "jid": None, "lid": "21758421798914@lid"}}
    assert extract_account_phone(payload) is None
    payload = {"me": {"lid": "21758421798914@lid"}}
    assert extract_account_phone(payload) is None


def test_empty_me_yields_none() -> None:
    # Unpaired WORKING-but-empty sessions report exactly this shape.
    payload = {"me": {"id": "", "pushName": "", "lid": "", "jid": None}}
    assert extract_account_phone(payload) is None


def test_missing_or_malformed_me_yields_none() -> None:
    assert extract_account_phone({}) is None
    assert extract_account_phone({"me": None}) is None
    assert extract_account_phone({"me": "not-a-dict"}) is None
    assert extract_account_phone({"me": {"id": 123}}) is None
    assert extract_account_phone({"me": {"id": "not-a-number@c.us"}}) is None
    assert extract_account_phone({"me": {"id": "972512217037@other.domain"}}) is None


# --- ingestion plumbing --------------------------------------------------------


async def _assigned(repo: FakeRepo) -> WahaSessionAssignment:
    return await repo.allocate_and_enqueue_create(USER)


async def test_working_event_with_identity_persists_and_fires_effect() -> None:
    repo = FakeRepo()
    hooks = HooksRecorder()
    service = _service(repo, hooks)
    assignment = await _assigned(repo)
    me = {"id": "972512217037@c.us", "pushName": "שיוש"}
    outcome = await service.handle_agent_event(
        _event("session_status", assignment.session_name, status="WORKING",
               timestamp=time.time(), extra={"me": me}),
        worker_id=WORKER,
    )
    assert outcome == "ok"
    assert hooks.established == [USER]
    stored = await repo.get(assignment.session_name)
    assert stored is not None
    assert stored.account_phone == "972512217037"


async def test_working_event_without_identity_leaves_none() -> None:
    repo = FakeRepo()
    service = _service(repo, HooksRecorder())
    assignment = await _assigned(repo)
    await service.handle_agent_event(
        _event("session_status", assignment.session_name, status="WORKING",
               timestamp=time.time()),
        worker_id=WORKER,
    )
    stored = await repo.get(assignment.session_name)
    assert stored is not None
    assert stored.account_phone is None


async def test_get_status_command_result_carries_identity() -> None:
    repo = FakeRepo()
    hooks = HooksRecorder()
    service = _service(repo, hooks)
    assignment = await _assigned(repo)
    # Session already connected (no identity yet — the pre-fix state).
    await service.handle_agent_event(
        _event("session_status", assignment.session_name, status="WORKING",
               timestamp=1000.0),
        worker_id=WORKER,
    )
    hooks.established.clear()
    # Claim the create command + complete it, then simulate a get_status
    # command result carrying the identity.
    commands = await service.claim_commands(WORKER)
    create_cmd = next(c for c in commands if c["action"] == "create_and_start")
    outcome = await service.handle_agent_event(
        {
            "type": "command_result",
            "worker_id": WORKER,
            "command_id": create_cmd["command_id"],
            "session": assignment.session_name,
            "success": True,
            "status": "WORKING",
            "timestamp": time.time(),
            "me": {"id": "972512217037@c.us", "pushName": "x"},
        },
        worker_id=WORKER,
    )
    assert outcome == "ok"
    stored = await repo.get(assignment.session_name)
    assert stored is not None
    assert stored.account_phone == "972512217037"
    # The status re-application re-fires confirm_working — verification
    # is re-driven automatically.
    assert hooks.established == [USER]


async def test_stopped_transition_clears_identity() -> None:
    repo = FakeRepo()
    service = _service(repo, HooksRecorder())
    assignment = await _assigned(repo)
    base = time.time()
    await service.handle_agent_event(
        _event("session_status", assignment.session_name, status="WORKING",
               timestamp=base, extra={"me": {"id": "972512217037@c.us"}}),
        worker_id=WORKER,
    )
    await service.handle_agent_event(
        _event("session_status", assignment.session_name, status="STOPPED",
               timestamp=base + 60),
        worker_id=WORKER,
    )
    stored = await repo.get(assignment.session_name)
    assert stored is not None
    assert stored.account_phone is None
    assert stored.account_identity_at is None


async def test_repairing_cycle_clears_then_restores_identity() -> None:
    """Stale-identity safety: after a new pairing the OLD identity is gone;
    only the newly reported identity is persisted."""
    repo = FakeRepo()
    service = _service(repo, HooksRecorder())
    assignment = await _assigned(repo)
    base = time.time()
    await service.handle_agent_event(
        _event("session_status", assignment.session_name, status="WORKING",
               timestamp=base, extra={"me": {"id": "972500000001@c.us"}}),
        worker_id=WORKER,
    )
    # Re-pair: SCAN_QR_CODE clears the old identity...
    await service.handle_agent_event(
        _event("session_status", assignment.session_name, status="SCAN_QR_CODE",
               timestamp=base + 60),
        worker_id=WORKER,
    )
    stored = await repo.get(assignment.session_name)
    assert stored is not None and stored.account_phone is None
    # ...and a different account scanning the new QR replaces it.
    await service.handle_agent_event(
        _event("session_status", assignment.session_name, status="WORKING",
               timestamp=base + 120, extra={"me": {"id": "972500000002@c.us"}}),
        worker_id=WORKER,
    )
    stored = await repo.get(assignment.session_name)
    assert stored is not None
    assert stored.account_phone == "972500000002"


# --- provisioner snapshot -------------------------------------------------------


async def test_provisioner_exposes_persisted_phone_in_snapshot() -> None:
    from echo_v2.integrations.waha.provisioner import WahaProvisioner
    from echo_v2.integrations.waha.settings import WahaSettings

    repo = FakeRepo()
    assignment = await _assigned(repo)
    now = datetime.now(timezone.utc)
    await repo.apply_lifecycle(
        assignment.session_name,
        raw_status="WORKING",
        mapped_status="connected",
        event_at=now,
        account_phone="972512217037",
    )
    provisioner = WahaProvisioner(
        sessions=repo,  # type: ignore[arg-type]
        settings=WahaSettings(
            onboarding_enabled=True,
            heartbeat_timeout_seconds=90.0,
            command_lease_seconds=120.0,
            command_max_attempts=5,
            command_poll_limit=10,
            qr_ttl_seconds=120.0,
        ),
    )
    ref = ConnectionRef(provider="waha", provider_connection_id=assignment.session_name)
    snapshot = await provisioner.get_status(ref)
    assert snapshot.status is ConnectionStatus.CONNECTED
    assert snapshot.phone_number == "972512217037"


# --- child-phone verification ---------------------------------------------------


@dataclass
class _Bot:
    sent: list[tuple[str, str]] = field(default_factory=list)

    async def send_text(self, user_phone: str, text: str) -> None:
        self.sent.append((user_phone, text))


class _UserRepo:
    def __init__(self, phones: dict[str, str]) -> None:
        self._phones = phones  # user_id -> phone

    async def get_phone_by_id(self, user_id: str) -> str | None:
        return self._phones.get(user_id)

    async def get_by_phone(self, phone: str) -> tuple[str, str, str | None] | None:
        for uid, p in self._phones.items():
            if p == phone:
                return (uid, "active", None)
        return None


class _ConnectionRepo:
    def __init__(self, conn: StoredConnection) -> None:
        self._conn = conn

    async def get_by_user(self, user_id: str) -> StoredConnection | None:
        return self._conn


class _Provisioner:
    def __init__(self, snapshot: ConnectionStatusSnapshot) -> None:
        self._snapshot = snapshot

    async def get_status(self, ref: ConnectionRef) -> ConnectionStatusSnapshot:
        return self._snapshot


class _Links:
    """Minimal guardian-link repo: one pending+authorized link."""

    def __init__(self, guardian_id: str, child_id: str) -> None:
        from echo_v2.domain.guard import GuardianChildLink, GuardianChildLinkStatus

        now = datetime.now(timezone.utc)
        self._link = GuardianChildLink(
            id="link-1",
            guardian_user_id=guardian_id,
            child_user_id=child_id,
            status=GuardianChildLinkStatus.PENDING,
            guardian_authorized_at=now,
        )
        self.activated: list[tuple[str, str]] = []
        self._activate_cls = GuardianChildLink
        self._status_cls = GuardianChildLinkStatus

    async def list_pending_for_child(self, child_user_id: str) -> list[Any]:
        return [self._link]

    async def get_active_for_pair(
        self, *, guardian_user_id: str, child_user_id: str
    ) -> Any:
        return None

    async def activate(
        self, *, guardian_user_id: str, child_user_id: str, now: datetime
    ) -> None:
        self.activated.append((guardian_user_id, child_user_id))
        self._link = self._activate_cls(
            id=self._link.id,
            guardian_user_id=guardian_user_id,
            child_user_id=child_user_id,
            status=self._status_cls.ACTIVE,
            guardian_authorized_at=self._link.guardian_authorized_at,
            safety_enabled_at=now,
        )


def _verification_service(
    *,
    snapshot: ConnectionStatusSnapshot,
    provider: str = "waha",
    child_phone: str = "+972512217037",
) -> tuple[OnboardingService, _Bot, _Links]:
    guardian_id = "22222222-2222-2222-2222-222222222222"
    child_id = "33333333-3333-3333-3333-333333333333"
    conn = StoredConnection(
        id="conn-1",
        user_id=child_id,
        ref=ConnectionRef(provider=provider, provider_connection_id="echo-x"),
        credentials=ProviderCredentials(data=b""),
        webhook_token_hash="h",
        status="connected",
    )
    service = OnboardingService(
        bot=_Bot(),  # type: ignore[arg-type]
        user_repo=_UserRepo({child_id: child_phone, guardian_id: "+972546610653"}),  # type: ignore[arg-type]
        connection_repo=_ConnectionRepo(conn),  # type: ignore[arg-type]
        provisioner=_Provisioner(snapshot),  # type: ignore[arg-type]
        webhook_base_url="https://echo.example.com",
        guardian_child_links=None,
    )
    links = _Links(guardian_id, child_id)
    service._guardian_child_links = links  # type: ignore[assignment]
    return service, service._bot, links  # type: ignore[return-value]


async def _activate(service: OnboardingService) -> bool:
    return await service._activate_child_connection("33333333-3333-3333-3333-333333333333")


async def test_waha_matching_phone_activates_guard() -> None:
    snapshot = ConnectionStatusSnapshot(
        status=ConnectionStatus.CONNECTED,
        provider_raw_status="WORKING",
        phone_number="972512217037",
    )
    service, bot, links = _verification_service(snapshot=snapshot)
    assert await _activate(service) is True
    assert links.activated, "guardian link must activate on a match"
    assert any("הושלם" in text or "פעיל" in text for _p, text in bot.sent), bot.sent


async def test_waha_phone_normalization_variants_match() -> None:
    """The provider reports digits-only JID user parts; child phones are
    stored E.164 — normalization must equate them."""
    snapshot = ConnectionStatusSnapshot(
        status=ConnectionStatus.CONNECTED,
        provider_raw_status="WORKING",
        phone_number="972512217037",  # no +, no punctuation — JID user part
    )
    service, _bot, links = _verification_service(snapshot=snapshot)
    assert await _activate(service) is True
    assert links.activated


async def test_waha_mismatching_phone_rejected_not_activated() -> None:
    snapshot = ConnectionStatusSnapshot(
        status=ConnectionStatus.CONNECTED,
        provider_raw_status="WORKING",
        phone_number="972500000999",
    )
    service, bot, links = _verification_service(snapshot=snapshot)
    assert await _activate(service) is True
    assert not links.activated
    assert any("לא תואם" in text for _p, text in bot.sent), bot.sent


async def test_waha_missing_identity_is_pending_never_verified() -> None:
    """No identity reported yet: no activation, no rejection message —
    verification stays pending until an event carries the identity."""
    snapshot = ConnectionStatusSnapshot(
        status=ConnectionStatus.CONNECTED,
        provider_raw_status="WORKING",
        phone_number=None,
    )
    service, bot, links = _verification_service(snapshot=snapshot)
    assert await _activate(service) is True  # handled — no legacy fallthrough
    assert not links.activated
    assert not any("לא תואם" in text for _p, text in bot.sent), bot.sent


async def test_waha_not_connected_snapshot_is_pending() -> None:
    snapshot = ConnectionStatusSnapshot(
        status=ConnectionStatus.CONNECTING,
        provider_raw_status="STARTING",
        phone_number=None,
    )
    service, bot, links = _verification_service(snapshot=snapshot)
    assert await _activate(service) is True
    assert not links.activated
    assert not any("לא תואם" in text for _p, text in bot.sent)


async def test_baileys_verification_regression_match() -> None:
    snapshot = ConnectionStatusSnapshot(
        status=ConnectionStatus.CONNECTED,
        provider_raw_status="open",
        phone_number="972512217037",
    )
    service, _bot, links = _verification_service(snapshot=snapshot, provider="baileys")
    assert await _activate(service) is True
    assert links.activated


async def test_baileys_verification_regression_mismatch_rejected() -> None:
    snapshot = ConnectionStatusSnapshot(
        status=ConnectionStatus.CONNECTED,
        provider_raw_status="open",
        phone_number="972500000999",
    )
    service, bot, links = _verification_service(snapshot=snapshot, provider="baileys")
    assert await _activate(service) is True
    assert not links.activated
    assert any("לא תואם" in text for _p, text in bot.sent)


async def test_baileys_missing_phone_still_rejected() -> None:
    """Baileys semantics unchanged: missing phone is a rejection (the
    connector answers synchronously — absence means something is wrong),
    unlike WAHA where identity is asynchronous."""
    snapshot = ConnectionStatusSnapshot(
        status=ConnectionStatus.CONNECTED,
        provider_raw_status="open",
        phone_number=None,
    )
    service, bot, links = _verification_service(snapshot=snapshot, provider="baileys")
    assert await _activate(service) is True
    assert not links.activated
    assert any("לא תואם" in text for _p, text in bot.sent)


async def test_event_datetime_fallback_for_invalid_timestamps() -> None:
    from echo_v2.services.waha_session_service import event_datetime

    assert event_datetime("not-a-number").tzinfo is not None
    assert event_datetime(None).tzinfo is not None
    assert event_datetime(float("inf")).tzinfo is not None  # OverflowError path
    valid = event_datetime(1795718136.42)
    assert valid.year == 2026
