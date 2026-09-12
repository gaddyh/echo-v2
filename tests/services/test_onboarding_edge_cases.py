"""Edge-case tests for OnboardingService — targets uncovered branches.

Covers error paths, idempotency skips, invalid inputs, polling fallbacks,
and the ``failed`` retry path that the main test file does not exercise.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

import pytest

from echo_v2.integrations.green.provisioner import GreenProvisioner
from echo_v2.persistence.whatsapp_connections import (
    InMemoryWhatsAppConnectionRepository,
)
from echo_v2.ports.whatsapp import ConnectionConfig, CreatedConnection
from echo_v2.services.onboarding import OnboardingService

__all__ = []

PHONE = "+972546610653"


# --- Fakes -----------------------------------------------------------------


@dataclass
class FakeBot:
    """Records sent messages for assertion."""

    sent: list[tuple[str, str]] = field(default_factory=list)

    async def send_text(self, user_phone: str, text: str) -> None:
        self.sent.append((user_phone, text))

    async def send_template(
        self,
        user_phone: str,
        template_name: str,
        language: str,
        body_params: list[str],
    ) -> str:
        self.sent.append((user_phone, f"[template:{template_name}]"))
        return "fake-msg-id"


class FakeUserRepo:
    """In-memory user repository with configurable failure modes."""

    def __init__(self, *, allow_recreate: bool = False) -> None:
        self._users: dict[str, dict] = {}
        self._by_id: dict[str, str] = {}
        self._allow_recreate = allow_recreate
        self.create_should_fail = False

    async def create_user(
        self,
        phone: str,
        *,
        timezone: str = "Asia/Jerusalem",
        first_name: str | None = None,
        onboarding_status: str = "pending",
    ) -> str:
        if self.create_should_fail:
            raise RuntimeError("boom")
        if phone in self._users and not self._allow_recreate:
            raise ValueError(f"User with phone {phone} already exists")
        user_id = secrets.token_hex(16)
        self._users[phone] = {
            "id": user_id,
            "onboarding": onboarding_status,
            "name": first_name,
            "timezone": timezone,
        }
        self._by_id[user_id] = phone
        return user_id

    async def get_by_phone(
        self, phone: str
    ) -> tuple[str, str | None, str | None] | None:
        user = self._users.get(phone)
        if user is None:
            return None
        return (user["id"], user["onboarding"], user["name"])

    async def update_onboarding_status(self, user_id: str, status: str) -> None:
        phone = self._by_id.get(user_id)
        if phone is None:
            return
        self._users[phone]["onboarding"] = status

    async def update_first_name(self, user_id: str, first_name: str) -> None:
        phone = self._by_id.get(user_id)
        if phone is None:
            return
        self._users[phone]["name"] = first_name
        self._users[phone]["onboarding"] = "active"

    async def get_phone_by_id(self, user_id: str) -> str | None:
        return self._by_id.get(user_id)


class FakeUserRepoNoPhoneLookup:
    """User repo without ``get_phone_by_id`` — _lookup_phone returns None."""

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
        user_id = secrets.token_hex(16)
        self._users[phone] = {
            "id": user_id,
            "onboarding": onboarding_status,
            "name": first_name,
            "timezone": timezone,
        }
        self._by_id[user_id] = phone
        return user_id

    async def get_by_phone(
        self, phone: str
    ) -> tuple[str, str | None, str | None] | None:
        user = self._users.get(phone)
        if user is None:
            return None
        return (user["id"], user["onboarding"], user["name"])

    async def update_onboarding_status(self, user_id: str, status: str) -> None:
        phone = self._by_id.get(user_id)
        if phone is None:
            return
        self._users[phone]["onboarding"] = status

    async def update_first_name(self, user_id: str, first_name: str) -> None:
        phone = self._by_id.get(user_id)
        if phone is None:
            return
        self._users[phone]["name"] = first_name
        self._users[phone]["onboarding"] = "active"


class FakeGreenClient:
    """Fake GreenClient with configurable state sequences and failures."""

    def __init__(self, otp_code: str = "12345678") -> None:
        self._otp_code = otp_code
        self.otp_calls: list[tuple[str, str, int]] = []
        self._state_sequence: list[str | None] = []
        self.get_auth_should_fail = False
        self.get_state_should_fail = False

    def set_state_sequence(self, states: list[str | None]) -> None:
        self._state_sequence = list(states)

    async def get_authorization_code(
        self,
        id_instance: str,
        api_token: str,
        phone_number: int,
    ) -> str:
        self.otp_calls.append((id_instance, api_token, phone_number))
        if self.get_auth_should_fail:
            raise RuntimeError("otp boom")
        return self._otp_code

    async def create_instance(self, payload: dict) -> dict:
        return {
            "idInstance": "fake-instance-123",
            "apiTokenInstance": "fake-api-token-456",
        }

    async def set_settings(
        self,
        id_instance: str,
        api_token: str,
        settings: dict,
    ) -> None:
        pass

    async def get_state_instance(self, id_instance: str, api_token: str) -> str | None:
        if self.get_state_should_fail:
            raise RuntimeError("state boom")
        if self._state_sequence:
            return self._state_sequence.pop(0)
        return "notAuthorized"

    async def get_qr_ws(self, id_instance: str, api_token: str, **kwargs) -> dict:
        return {"type": "qrCode", "message": "base64data"}

    async def logout(self, id_instance: str, api_token: str) -> None:
        pass

    async def delete_instance(self, instance_id: str) -> None:
        pass


class FailingProvisioner:
    """Provisioner whose create_connection always raises."""

    async def create_connection(self, config: ConnectionConfig) -> CreatedConnection:
        raise RuntimeError("provision boom")


# --- Fixtures --------------------------------------------------------------


def _make_service(
    *,
    bot=None,
    user_repo=None,
    connection_repo=None,
    green_client=None,
    provisioner=None,
    webhook_base_url="https://echo.example.com",
    poll_interval=0.01,
    poll_max_attempts=3,
) -> tuple[OnboardingService, FakeBot, FakeUserRepo, InMemoryWhatsAppConnectionRepository, FakeGreenClient]:
    bot = bot or FakeBot()
    user_repo = user_repo or FakeUserRepo()
    connection_repo = connection_repo or InMemoryWhatsAppConnectionRepository()
    green_client = green_client or FakeGreenClient(otp_code="87654321")
    if provisioner is None:
        provisioner = GreenProvisioner(
            client=green_client,
            credential_resolver=connection_repo,
        )
    service = OnboardingService(
        bot=bot,
        user_repo=user_repo,
        connection_repo=connection_repo,
        provisioner=provisioner,
        green_client=green_client,
        webhook_base_url=webhook_base_url,
        poll_interval=poll_interval,
        poll_max_attempts=poll_max_attempts,
    )
    return service, bot, user_repo, connection_repo, green_client


# --- handle_unknown_user: connected / failed / create_user failure ----------


async def test_connected_user_skipped():
    """Already-connected user → onboarding skipped (no OTP, no message)."""
    service, bot, user_repo, _conn, green_client = _make_service()

    user_id = await user_repo.create_user(PHONE, onboarding_status="connected")
    await user_repo.update_onboarding_status(user_id, "connected")

    await service.handle_unknown_user(PHONE)

    assert len(green_client.otp_calls) == 0
    assert len(bot.sent) == 0


async def test_failed_user_retries_onboarding():
    """User with 'failed' status falls through to create a new instance."""
    user_repo = FakeUserRepo(allow_recreate=True)
    service, bot, user_repo, _conn, green_client = _make_service(
        user_repo=user_repo
    )

    # Pre-create a user in 'failed' state.
    await user_repo.create_user(PHONE, onboarding_status="failed")
    assert (await user_repo.get_by_phone(PHONE))[1] == "failed"

    await service.handle_unknown_user(PHONE)
    import asyncio
    await asyncio.sleep(0.2)

    # A new instance was provisioned and OTP requested.
    assert len(green_client.otp_calls) == 1
    # "please wait" + OTP instructions sent.
    assert len(bot.sent) == 2


async def test_create_user_exception_returns():
    """create_user raising → logged and returns silently (lines 179-181)."""
    user_repo = FakeUserRepo()
    user_repo.create_should_fail = True
    service, bot, _user_repo, _conn, green_client = _make_service(
        user_repo=user_repo
    )

    await service.handle_unknown_user(PHONE)

    assert len(green_client.otp_calls) == 0
    assert len(bot.sent) == 0


# --- _provision_and_send_otp: provisioner / OTP failures ---------------------


async def test_provisioner_create_connection_fails():
    """provisioner.create_connection raising → status=failed + failure msg."""
    service, bot, user_repo, _conn, green_client = _make_service(
        provisioner=FailingProvisioner()
    )

    await service.handle_unknown_user(PHONE)
    import asyncio
    await asyncio.sleep(0.2)

    user = await user_repo.get_by_phone(PHONE)
    assert user is not None
    assert user[1] == "failed"
    assert len(green_client.otp_calls) == 0
    # "please wait" + failure message.
    assert len(bot.sent) == 2
    _phone, failure_msg = bot.sent[1]
    assert "מצטער" in failure_msg


async def test_get_authorization_code_fails():
    """getAuthorizationCode raising → status=failed + failure msg (266-273)."""
    green_client = FakeGreenClient(otp_code="87654321")
    green_client.get_auth_should_fail = True
    service, bot, user_repo, _conn, green_client = _make_service(
        green_client=green_client
    )

    await service.handle_unknown_user(PHONE)
    import asyncio
    await asyncio.sleep(0.2)

    user = await user_repo.get_by_phone(PHONE)
    assert user is not None
    assert user[1] == "failed"
    # "please wait" + OTP failure message.
    assert len(bot.sent) == 2
    _phone, failure_msg = bot.sent[1]
    assert "מצטער" in failure_msg
    assert "קוד האימות" in failure_msg


# --- _poll_for_authorization: authorized / exception / timeout ---------------


@pytest.fixture
def no_sleep(monkeypatch):
    """Patch asyncio.sleep to a no-op so polling loops run instantly."""
    import asyncio

    async def _fake_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)


async def test_poll_for_authorization_detects_authorized(no_sleep):
    """Poll finds 'authorized' → completes onboarding (lines 311-323)."""
    service, bot, user_repo, _conn, green_client = _make_service()

    user_id = await user_repo.create_user(PHONE, onboarding_status="pending")
    await user_repo.update_onboarding_status(user_id, "pending")

    green_client.set_state_sequence(["authorized"])

    await service._poll_for_authorization(user_id, PHONE, "inst-1", "token-1")

    # handle_connection_established ran → status connected + welcome sent.
    user = await user_repo.get_by_phone(PHONE)
    assert user[1] == "connected"
    assert len(bot.sent) == 1
    _phone, welcome = bot.sent[0]
    assert "היי" in welcome


async def test_poll_for_authorization_exception_then_authorized(no_sleep):
    """Transient exception during poll is swallowed, polling continues (324-326)."""
    service, _bot, user_repo, _conn, green_client = _make_service()

    user_id = await user_repo.create_user(PHONE, onboarding_status="pending")

    # First call raises, then succeed with 'authorized'.
    call_count = {"n": 0}

    async def _flaky(id_instance, api_token):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("transient 401")
        return "authorized"

    green_client.get_state_instance = _flaky  # type: ignore[assignment]

    await service._poll_for_authorization(user_id, PHONE, "inst-1", "token-1")

    user = await user_repo.get_by_phone(PHONE)
    assert user[1] == "connected"


async def test_poll_for_authorization_times_out(no_sleep):
    """Poll never sees 'authorized' → times out after 30 attempts (328-332)."""
    service, bot, user_repo, _conn, green_client = _make_service()

    user_id = await user_repo.create_user(PHONE, onboarding_status="pending")

    # Always returns a non-authorized state.
    green_client.set_state_sequence(["notAuthorized"] * 30)

    await service._poll_for_authorization(user_id, PHONE, "inst-1", "token-1")

    # No welcome sent — onboarding not completed by the poll.
    user = await user_repo.get_by_phone(PHONE)
    assert user[1] == "pending"
    assert len(bot.sent) == 0


# --- _wait_for_instance_ready: exception branch -----------------------------


async def test_wait_for_instance_ready_handles_exception():
    """getStateInstance raising during readiness poll is logged (375-377)."""
    green_client = FakeGreenClient()
    green_client.get_state_should_fail = True
    service, _bot, _user_repo, _conn, green_client = _make_service(
        green_client=green_client
    )

    ready = await service._wait_for_instance_ready("inst-1", "token-1")
    assert ready is False


# --- handle_resend_request: invalid phone / unknown / not pending -----------


async def test_resend_request_invalid_phone():
    """Invalid phone → returns silently (line 390)."""
    service, bot, _user_repo, _conn, green_client = _make_service()

    await service.handle_resend_request("not-a-phone")

    assert len(green_client.otp_calls) == 0
    assert len(bot.sent) == 0


async def test_resend_request_unknown_user():
    """Unknown user → returns silently (line 394)."""
    service, bot, _user_repo, _conn, green_client = _make_service()

    await service.handle_resend_request(PHONE)

    assert len(green_client.otp_calls) == 0
    assert len(bot.sent) == 0


async def test_resend_request_not_pending():
    """Non-pending user → returns silently (line 397)."""
    service, bot, user_repo, _conn, green_client = _make_service()

    user_id = await user_repo.create_user(PHONE, onboarding_status="active")
    await user_repo.update_onboarding_status(user_id, "active")

    await service.handle_resend_request(PHONE)

    assert len(green_client.otp_calls) == 0
    assert len(bot.sent) == 0


# --- handle_connection_established: already connected/active ---------------


async def test_connection_established_already_connected_skips_welcome():
    """Already-connected user → welcome skipped (lines 417-422)."""
    service, bot, user_repo, _conn, _green_client = _make_service()

    user_id = await user_repo.create_user(PHONE, onboarding_status="connected")
    await user_repo.update_onboarding_status(user_id, "connected")

    await service.handle_connection_established(user_id, PHONE)

    # No welcome message sent.
    assert len(bot.sent) == 0
    user = await user_repo.get_by_phone(PHONE)
    assert user[1] == "connected"


async def test_connection_established_already_active_skips_welcome():
    """Already-active user → welcome skipped (lines 417-422)."""
    service, bot, user_repo, _conn, _green_client = _make_service()

    user_id = await user_repo.create_user(
        PHONE, onboarding_status="active", first_name="Dana"
    )
    await user_repo.update_onboarding_status(user_id, "active")

    await service.handle_connection_established(user_id, PHONE)

    assert len(bot.sent) == 0


# --- handle_connection_established_by_id: no phone -------------------------


async def test_connection_established_by_id_no_phone_lookup():
    """User repo without get_phone_by_id → _lookup_phone None → return (439-447)."""
    user_repo = FakeUserRepoNoPhoneLookup()
    service, bot, _user_repo, _conn, _green_client = _make_service(
        user_repo=user_repo
    )

    await user_repo.create_user(PHONE, onboarding_status="pending")

    # No phone resolvable — should return without sending welcome.
    await service.handle_connection_established_by_id("some-user-id")

    assert len(bot.sent) == 0


# --- handle_name_response: invalid phone / unknown / empty name -------------


async def test_name_response_invalid_phone():
    """Invalid phone → returns False (line 459)."""
    service, _bot, _user_repo, _conn, _green_client = _make_service()

    handled = await service.handle_name_response("not-a-phone", "Dana")
    assert handled is False


async def test_name_response_unknown_user():
    """Unknown user → returns False (line 463)."""
    service, _bot, _user_repo, _conn, _green_client = _make_service()

    handled = await service.handle_name_response(PHONE, "Dana")
    assert handled is False


async def test_name_response_empty_name():
    """Blank name → returns False (line 470)."""
    service, _bot, user_repo, _conn, _green_client = _make_service()

    user_id = await user_repo.create_user(PHONE, onboarding_status="connected")
    await user_repo.update_onboarding_status(user_id, "connected")

    handled = await service.handle_name_response(PHONE, "   ")
    assert handled is False


# --- is_onboarding: invalid phone ------------------------------------------


async def test_is_onboarding_invalid_phone():
    """Invalid phone → returns False (line 486)."""
    service, _bot, _user_repo, _conn, _green_client = _make_service()

    assert await service.is_onboarding("not-a-phone") is False


# --- _resend_otp: no connection / get_code failure --------------------------


async def test_resend_otp_no_connection():
    """Pending user with no stored connection → returns silently (497-498)."""
    service, bot, user_repo, _conn, green_client = _make_service()

    user_id = await user_repo.create_user(PHONE, onboarding_status="pending")
    await user_repo.update_onboarding_status(user_id, "pending")

    await service.handle_resend_request(PHONE)

    assert len(green_client.otp_calls) == 0
    # No OTP message sent because no connection exists.
    assert len(bot.sent) == 0


async def test_resend_otp_get_code_fails():
    """getAuthorizationCode failing during resend → error message (508-514)."""
    green_client = FakeGreenClient(otp_code="87654321")
    service, bot, _user_repo, _conn, green_client = _make_service(
        green_client=green_client
    )

    # Start onboarding to create a connection record (succeeds, user pending).
    await service.handle_unknown_user(PHONE)
    import asyncio
    await asyncio.sleep(0.2)
    assert len(green_client.otp_calls) == 1

    # Now make the resend fail to get a new code.
    green_client.get_auth_should_fail = True
    sent_before = len(bot.sent)
    await service.handle_resend_request(PHONE)

    assert len(green_client.otp_calls) == 2
    # An error message was sent.
    assert len(bot.sent) == sent_before + 1
    _phone, error_msg = bot.sent[-1]
    assert "לא הצלחתי לקבל קוד" in error_msg
