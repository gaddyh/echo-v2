"""Edge-case tests for OnboardingService — targets uncovered branches.

Covers error paths, idempotency skips, invalid inputs, polling fallbacks,
and the ``failed`` retry path that the main test file does not exercise.

Flow (simplified — name collected before provisioning):
1. start_onboarding → create user (pending) + ask name
2. handle_name_response → store name + start provisioning
3. _poll_until_authorized → send OTP once, then complete to active
4. handle_connection_established → status=active + welcome
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field

import pytest

from echo_v2.integrations.green.provisioner import GreenProvisioner
from echo_v2.persistence.whatsapp_connections import (
    InMemoryWhatsAppConnectionRepository,
)
from echo_v2.ports.bot import BotEvent, BotEventType
from echo_v2.ports.whatsapp import ConnectionConfig, CreatedConnection
from echo_v2.services.onboarding import OnboardingService

__all__ = []

PHONE = "+972546610653"


# --- Fakes -----------------------------------------------------------------


@dataclass
class FakeBot:
    """Records sent messages for assertion."""

    sent: list[tuple[str, str]] = field(default_factory=list)
    sent_buttons: list[tuple[str, str, list[dict]]] = field(default_factory=list)
    send_buttons_should_fail: bool = False
    send_text_should_fail: bool = False

    async def send_text(self, user_phone: str, text: str) -> None:
        if self.send_text_should_fail:
            raise RuntimeError("text boom")
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

    async def send_buttons(
        self,
        user_phone: str,
        *,
        body_text: str,
        buttons: list[dict],
    ) -> str:
        if self.send_buttons_should_fail:
            raise RuntimeError("buttons boom")
        self.sent_buttons.append((user_phone, body_text, buttons))
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
        """Update first_name only — does NOT change onboarding_status."""
        phone = self._by_id.get(user_id)
        if phone is None:
            return
        self._users[phone]["name"] = first_name

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
        """Update first_name only — does NOT change onboarding_status."""
        phone = self._by_id.get(user_id)
        if phone is None:
            return
        self._users[phone]["name"] = first_name


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


async def _start_and_name(
    service: OnboardingService, phone: str = PHONE, name: str = "Dana"
) -> None:
    """Helper: consent → create user → send name → start provisioning."""
    await service.start_onboarding(phone)
    await service.handle_name_response(phone, name)


# --- start_onboarding: failed / create_user failure -----------------------


async def test_failed_user_retries_onboarding():
    """User with 'failed' status falls through to create a new instance."""
    user_repo = FakeUserRepo(allow_recreate=True)
    service, bot, user_repo, _conn, green_client = _make_service(
        user_repo=user_repo
    )

    # Pre-create a user in 'failed' state.
    await user_repo.create_user(PHONE, onboarding_status="failed")
    assert (await user_repo.get_by_phone(PHONE))[1] == "failed"

    # start_onboarding for failed user → creates new user (pending), asks name.
    await service.start_onboarding(PHONE)

    # User is now pending, name prompt sent.
    user = await user_repo.get_by_phone(PHONE)
    assert user[1] == "pending"
    assert len(bot.sent) == 1  # name prompt

    # Send name → provisioning starts.
    await service.handle_name_response(PHONE, "Dana")
    await asyncio.sleep(0.2)

    # A new instance was provisioned and OTP requested.
    assert len(green_client.otp_calls) == 1


async def test_create_user_exception_returns():
    """create_user raising → logged and returns silently."""
    user_repo = FakeUserRepo()
    user_repo.create_should_fail = True
    service, bot, _user_repo, _conn, green_client = _make_service(
        user_repo=user_repo
    )

    await service.start_onboarding(PHONE)

    assert len(green_client.otp_calls) == 0
    assert len(bot.sent) == 0


# --- _provision_and_poll: provisioner / OTP failures -----------------------


async def test_provisioner_create_connection_fails():
    """provisioner.create_connection raising → status=failed + failure msg."""
    service, bot, user_repo, _conn, green_client = _make_service(
        provisioner=FailingProvisioner()
    )

    await _start_and_name(service)
    await asyncio.sleep(0.2)

    user = await user_repo.get_by_phone(PHONE)
    assert user is not None
    assert user[1] == "failed"
    assert len(green_client.otp_calls) == 0
    # name prompt + name confirm + failure message.
    assert len(bot.sent) == 3
    _phone, failure_msg = bot.sent[2]
    assert "מצטער" in failure_msg


async def test_get_authorization_code_fails():
    """getAuthorizationCode raising → status=failed + failure msg."""
    green_client = FakeGreenClient(otp_code="87654321")
    green_client.get_auth_should_fail = True
    service, bot, user_repo, _conn, green_client = _make_service(
        green_client=green_client
    )

    # State sequence: notAuthorized (triggers OTP attempt which fails).
    green_client.set_state_sequence(["notAuthorized"])

    await _start_and_name(service)
    await asyncio.sleep(0.2)

    user = await user_repo.get_by_phone(PHONE)
    assert user is not None
    assert user[1] == "failed"
    # name prompt + name confirm + OTP failure message.
    assert len(bot.sent) == 3
    _phone, failure_msg = bot.sent[2]
    assert "מצטער" in failure_msg
    assert "קוד האימות" in failure_msg


# --- Consent-first: handle_unknown_event / send_introduction ---------------


def _button_event(phone: str, button_id: str) -> BotEvent:
    """Build a BUTTON_REPLY BotEvent for tests."""
    return BotEvent(
        event_id="test-evt",
        user_phone=phone,
        type=BotEventType.BUTTON_REPLY,
        button_id=button_id,
    )


def _text_event(phone: str, text: str) -> BotEvent:
    """Build a TEXT BotEvent for tests."""
    return BotEvent(
        event_id="test-evt",
        user_phone=phone,
        type=BotEventType.TEXT,
        text=text,
    )


def _contact_event(phone: str) -> BotEvent:
    """Build a CONTACT BotEvent for tests."""
    from echo_v2.ports.bot import BotContact

    return BotEvent(
        event_id="test-evt",
        user_phone=phone,
        type=BotEventType.CONTACT,
        contact=BotContact(phone="+972500000000", name="Test"),
    )


async def test_handle_unknown_event_start_button_triggers_onboarding():
    """onboarding:start button → start_onboarding (creates user, asks name)."""
    service, _bot, user_repo, _conn, _green_client = _make_service()

    event = _button_event(PHONE, "onboarding:start")
    await service.handle_unknown_event(event)

    # User was created (pending, no name yet, no provisioning).
    user = await user_repo.get_by_phone(PHONE)
    assert user is not None
    assert user[1] == "pending"
    assert user[2] is None


async def test_handle_unknown_event_info_button_sends_explanation():
    """onboarding:info button → send_explanation (no user created, no Green call)."""
    service, bot, user_repo, _conn, green_client = _make_service()

    event = _button_event(PHONE, "onboarding:info")
    await service.handle_unknown_event(event)

    # No user created.
    user = await user_repo.get_by_phone(PHONE)
    assert user is None
    # No OTP call.
    assert len(green_client.otp_calls) == 0
    # Explanation buttons sent.
    assert len(bot.sent_buttons) == 1
    _phone, body, buttons = bot.sent_buttons[0]
    assert _phone == PHONE
    assert "WhatsApp" in body
    # Only the connect button (not the info button again).
    assert len(buttons) == 1
    assert buttons[0]["id"] == "onboarding:start"


async def test_handle_unknown_event_text_consent_phrase_triggers_onboarding():
    """Text 'חברו אותי' → start_onboarding (fallback for button delivery failure)."""
    service, _bot, user_repo, _conn, _green_client = _make_service()

    event = _text_event(PHONE, "חברו אותי")
    await service.handle_unknown_event(event)

    user = await user_repo.get_by_phone(PHONE)
    assert user is not None
    assert user[1] == "pending"


async def test_handle_unknown_event_text_hello_sends_intro():
    """Text 'hello' (not the consent phrase) → send_introduction, no user created."""
    service, bot, user_repo, _conn, green_client = _make_service()

    event = _text_event(PHONE, "hello")
    await service.handle_unknown_event(event)

    # No user created, no OTP.
    user = await user_repo.get_by_phone(PHONE)
    assert user is None
    assert len(green_client.otp_calls) == 0
    # Intro buttons sent.
    assert len(bot.sent_buttons) == 1
    _phone, body, buttons = bot.sent_buttons[0]
    assert _phone == PHONE
    assert "Echo" in body
    assert len(buttons) == 2
    ids = [b["id"] for b in buttons]
    assert "onboarding:start" in ids
    assert "onboarding:info" in ids


async def test_handle_unknown_event_generic_yes_does_not_trigger_onboarding():
    """Text 'כן' is NOT accepted as consent — only the deliberate phrase."""
    service, bot, user_repo, _conn, _green_client = _make_service()

    event = _text_event(PHONE, "כן")
    await service.handle_unknown_event(event)

    user = await user_repo.get_by_phone(PHONE)
    assert user is None
    # Intro sent instead.
    assert len(bot.sent_buttons) == 1


async def test_handle_unknown_event_unknown_button_sends_intro():
    """Unknown button_id → send_introduction."""
    service, bot, user_repo, _conn, _green_client = _make_service()

    event = _button_event(PHONE, "something:else")
    await service.handle_unknown_event(event)

    user = await user_repo.get_by_phone(PHONE)
    assert user is None
    assert len(bot.sent_buttons) == 1


async def test_handle_unknown_event_contact_sends_intro():
    """CONTACT event from unknown user → send_introduction."""
    service, bot, user_repo, _conn, _green_client = _make_service()

    event = _contact_event(PHONE)
    await service.handle_unknown_event(event)

    user = await user_repo.get_by_phone(PHONE)
    assert user is None
    assert len(bot.sent_buttons) == 1


async def test_send_introduction_sends_two_buttons():
    """send_introduction sends the intro body with start + info buttons."""
    service, bot, _user_repo, _conn, _green_client = _make_service()

    await service.send_introduction(PHONE)

    assert len(bot.sent_buttons) == 1
    _phone, body, buttons = bot.sent_buttons[0]
    assert _phone == PHONE
    assert "רוצה להתחבר" in body
    assert len(buttons) == 2
    assert buttons[0]["id"] == "onboarding:start"
    assert buttons[1]["id"] == "onboarding:info"


async def test_send_explanation_sends_one_button():
    """send_explanation sends the info body with only the connect button."""
    service, bot, _user_repo, _conn, _green_client = _make_service()

    await service.send_explanation(PHONE)

    assert len(bot.sent_buttons) == 1
    _phone, body, buttons = bot.sent_buttons[0]
    assert _phone == PHONE
    assert "מכשיר מקושר" in body
    assert len(buttons) == 1
    assert buttons[0]["id"] == "onboarding:start"


async def test_send_introduction_send_failure_does_not_crash():
    """If send_buttons raises in send_introduction, it logs and returns."""
    service, bot, _user_repo, _conn, _green_client = _make_service()
    bot.send_buttons_should_fail = True

    # Should not raise.
    await service.send_introduction(PHONE)


# --- _poll_until_authorized: authorized / exception / timeout ---------------


@pytest.fixture
def no_sleep(monkeypatch):
    """Patch asyncio.sleep to a no-op so polling loops run instantly."""

    async def _fake_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)


async def test_poll_until_authorized_detects_authorized(no_sleep):
    """Poll finds 'authorized' → completes onboarding (status=active)."""
    service, bot, user_repo, _conn, green_client = _make_service()

    user_id = await user_repo.create_user(PHONE, onboarding_status="pending")
    await user_repo.update_first_name(user_id, "Dana")

    green_client.set_state_sequence(["authorized"])

    await service._poll_until_authorized(user_id, PHONE, "inst-1", "token-1")

    # handle_connection_established ran → status active + welcome sent.
    user = await user_repo.get_by_phone(PHONE)
    assert user[1] == "active"
    assert len(bot.sent) == 1
    _phone, welcome = bot.sent[0]
    assert "היי" in welcome


async def test_poll_until_authorized_exception_then_authorized(no_sleep):
    """Transient exception during poll is swallowed, polling continues."""
    service, _bot, user_repo, _conn, green_client = _make_service()

    user_id = await user_repo.create_user(PHONE, onboarding_status="pending")
    await user_repo.update_first_name(user_id, "Dana")

    # First call raises, then succeed with 'authorized'.
    call_count = {"n": 0}

    async def _flaky(id_instance, api_token):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("transient 401")
        return "authorized"

    green_client.get_state_instance = _flaky  # type: ignore[assignment]

    await service._poll_until_authorized(user_id, PHONE, "inst-1", "token-1")

    user = await user_repo.get_by_phone(PHONE)
    assert user[1] == "active"


async def test_poll_until_authorized_times_out(no_sleep):
    """Poll never sees 'authorized' → times out, user remains pending.

    If the instance became ready (notAuthorized) and OTP was sent, but the
    user never authorized, the user remains ``pending`` so they can retry
    via 'קוד'. The timeout message tells them to reply 'קוד' for a fresh code.
    """
    service, bot, user_repo, _conn, green_client = _make_service()

    user_id = await user_repo.create_user(PHONE, onboarding_status="pending")
    await user_repo.update_first_name(user_id, "Dana")

    # Always returns notAuthorized (instance ready, OTP sent, but never authorized).
    green_client.set_state_sequence(["notAuthorized"] * 30)

    await service._poll_until_authorized(user_id, PHONE, "inst-1", "token-1")

    # No welcome sent — onboarding not completed by the poll.
    user = await user_repo.get_by_phone(PHONE)
    assert user[1] == "pending"
    # OTP + timeout message sent.
    assert len(bot.sent) == 2
    _phone, otp_msg = bot.sent[0]
    assert "הקוד שלך" in otp_msg
    _phone, msg = bot.sent[1]
    assert _phone == PHONE
    assert "לא התחברת בזמן" in msg
    assert "קוד" in msg


async def test_poll_timeout_user_remains_pending_and_resend_works(no_sleep):
    """After timeout (instance ready, OTP sent), user remains pending and 'קוד' works."""
    service, bot, user_repo, conn_repo, green_client = _make_service()

    user_id = await user_repo.create_user(PHONE, onboarding_status="pending")
    await user_repo.update_first_name(user_id, "Dana")

    # Store a connection row so _resend_otp can find it.
    from echo_v2.persistence.whatsapp_connections import StoredConnection
    from echo_v2.ports.whatsapp import (
        ConnectionRef,
        ConnectionStatus,
        ProviderCredentials,
    )

    conn = StoredConnection(
        user_id=user_id,
        ref=ConnectionRef(provider="green", provider_connection_id="inst-1"),
        credentials=ProviderCredentials(data=b"token-1"),
        webhook_token_hash=b"\x00" * 32,
        status=ConnectionStatus.PROVISIONING,
    )
    await conn_repo.save(conn)

    # Poll times out (instance ready, OTP sent, but user didn't authorize).
    green_client.set_state_sequence(["notAuthorized"] * 30)
    await service._poll_until_authorized(user_id, PHONE, "inst-1", "token-1")

    # User remains pending (can retry via 'קוד').
    user = await user_repo.get_by_phone(PHONE)
    assert user[1] == "pending"

    # Replying 'קוד' triggers a fresh OTP.
    sent_before = len(bot.sent)
    otp_calls_before = len(green_client.otp_calls)
    await service.handle_resend_request(PHONE)

    # A new OTP was requested and a new OTP message was sent.
    assert len(green_client.otp_calls) == otp_calls_before + 1
    assert len(bot.sent) == sent_before + 1
    _phone, otp_msg = bot.sent[-1]
    assert "הקוד שלך" in otp_msg


async def test_poll_timeout_send_failure_does_not_crash(no_sleep):
    """If sending the timeout message raises, polling still exits cleanly."""
    service, bot, user_repo, _conn, green_client = _make_service()

    user_id = await user_repo.create_user(PHONE, onboarding_status="pending")
    await user_repo.update_first_name(user_id, "Dana")

    green_client.set_state_sequence(["notAuthorized"] * 30)

    # Make send_text raise on the timeout message.
    call_count = {"n": 0}

    async def _failing_send(phone, text):
        call_count["n"] += 1
        # The timeout message is the only send in this test path.
        raise RuntimeError("bot send boom")

    bot.send_text = _failing_send  # type: ignore[assignment]

    # Should not raise.
    await service._poll_until_authorized(user_id, PHONE, "inst-1", "token-1")

    # The timeout send was attempted.
    assert call_count["n"] >= 1
    # User remains pending (instance was ready, OTP sent, but not authorized).
    user = await user_repo.get_by_phone(PHONE)
    assert user[1] == "pending"


async def test_poll_sends_otp_once_on_not_authorized(no_sleep):
    """OTP is sent exactly once when state reaches 'notAuthorized'."""
    service, _bot, _user_repo, _conn, green_client = _make_service()

    user_id = await _ensure_user(service)

    # notAuthorized multiple times, then authorized.
    green_client.set_state_sequence(
        ["notAuthorized", "notAuthorized", "notAuthorized", "authorized"]
    )

    await service._poll_until_authorized(user_id, PHONE, "inst-1", "token-1")

    # OTP requested exactly once despite multiple notAuthorized states.
    assert len(green_client.otp_calls) == 1


async def _ensure_user(service: OnboardingService) -> str:
    """Create a pending user with a name for poll tests."""
    user_repo = service._user_repo  # type: ignore[attr-defined]
    user_id = await user_repo.create_user(PHONE, first_name="Dana")
    await user_repo.update_first_name(user_id, "Dana")
    return user_id


# --- handle_resend_request: invalid phone / unknown / not pending -----------


async def test_resend_request_invalid_phone():
    """Invalid phone → returns silently."""
    service, bot, _user_repo, _conn, green_client = _make_service()

    handled = await service.handle_resend_request("not-a-phone")

    assert handled is False
    assert len(green_client.otp_calls) == 0
    assert len(bot.sent) == 0


async def test_resend_request_unknown_user():
    """Unknown user → returns False."""
    service, bot, _user_repo, _conn, green_client = _make_service()

    handled = await service.handle_resend_request(PHONE)

    assert handled is False
    assert len(green_client.otp_calls) == 0
    assert len(bot.sent) == 0


async def test_resend_request_no_connection():
    """Known user with no connection row → returns False, no OTP."""
    service, bot, user_repo, _conn, green_client = _make_service()

    user_id = await user_repo.create_user(PHONE, onboarding_status="active")
    await user_repo.update_onboarding_status(user_id, "active")

    handled = await service.handle_resend_request(PHONE)

    assert handled is False
    assert len(green_client.otp_calls) == 0
    assert len(bot.sent) == 0


async def test_resend_request_active_user_notAuthorized_issues_otp():
    """Active user with connection, Green says notAuthorized → fresh OTP issued."""
    from echo_v2.persistence.whatsapp_connections import StoredConnection
    from echo_v2.ports.whatsapp import (
        ConnectionRef,
        ConnectionStatus,
        ProviderCredentials,
    )

    service, bot, user_repo, conn_repo, green_client = _make_service()

    user_id = await user_repo.create_user(PHONE, onboarding_status="active")
    await user_repo.update_onboarding_status(user_id, "active")

    conn = StoredConnection(
        user_id=user_id,
        ref=ConnectionRef(provider="green", provider_connection_id="inst-1"),
        credentials=ProviderCredentials(data=b"token-1"),
        webhook_token_hash=b"\x00" * 32,
        status=ConnectionStatus.CONNECTED,
    )
    await conn_repo.save(conn)

    # Green says notAuthorized (instance disconnected, needs re-pair).
    green_client.set_state_sequence(["notAuthorized"])

    handled = await service.handle_resend_request(PHONE)

    assert handled is True
    assert len(green_client.otp_calls) == 1
    assert len(bot.sent) == 1
    _phone, msg = bot.sent[0]
    assert "הקוד שלך" in msg


async def test_resend_request_pending_user_authorized_completes_active():
    """Stale DB (pending), Green says authorized → 'already connected', DB → active."""
    from echo_v2.persistence.whatsapp_connections import StoredConnection
    from echo_v2.ports.whatsapp import (
        ConnectionRef,
        ConnectionStatus,
        ProviderCredentials,
    )

    service, bot, user_repo, conn_repo, green_client = _make_service()

    # Stale DB: pending, but Green says authorized.
    user_id = await user_repo.create_user(PHONE, onboarding_status="pending")

    conn = StoredConnection(
        user_id=user_id,
        ref=ConnectionRef(provider="green", provider_connection_id="inst-1"),
        credentials=ProviderCredentials(data=b"token-1"),
        webhook_token_hash=b"\x00" * 32,
        status=ConnectionStatus.PROVISIONING,
    )
    await conn_repo.save(conn)

    green_client.set_state_sequence(["authorized"])

    handled = await service.handle_resend_request(PHONE)

    assert handled is True
    assert len(green_client.otp_calls) == 0
    assert len(bot.sent) == 1
    _phone, msg = bot.sent[0]
    assert "כבר מחובר" in msg
    # DB updated from pending → active.
    user = await user_repo.get_by_phone(PHONE)
    assert user[1] == "active"


async def test_resend_request_failed_user_issues_otp():
    """Failed user with connection → fresh OTP issued (Green is the gate, not DB status)."""
    from echo_v2.persistence.whatsapp_connections import StoredConnection
    from echo_v2.ports.whatsapp import (
        ConnectionRef,
        ConnectionStatus,
        ProviderCredentials,
    )

    service, bot, user_repo, conn_repo, green_client = _make_service()

    user_id = await user_repo.create_user(PHONE, onboarding_status="failed")
    await user_repo.update_onboarding_status(user_id, "failed")

    conn = StoredConnection(
        user_id=user_id,
        ref=ConnectionRef(provider="green", provider_connection_id="inst-1"),
        credentials=ProviderCredentials(data=b"token-1"),
        webhook_token_hash=b"\x00" * 32,
        status=ConnectionStatus.PROVISIONING,
    )
    await conn_repo.save(conn)

    green_client.set_state_sequence(["notAuthorized"])

    handled = await service.handle_resend_request(PHONE)

    assert handled is True
    assert len(green_client.otp_calls) == 1
    assert len(bot.sent) == 1
    assert "הקוד שלך" in bot.sent[0][1]


async def test_resend_request_returns_false_for_unknown_user():
    """Unknown user → returns False (so dispatch can fall through to intro)."""
    service, _bot, _user_repo, _conn, green_client = _make_service()

    handled = await service.handle_resend_request(PHONE)

    assert handled is False
    assert len(green_client.otp_calls) == 0


# --- handle_connection_established: already active -------------------------


async def test_connection_established_already_active_skips_welcome():
    """Already-active user → welcome skipped."""
    service, bot, user_repo, _conn, _green_client = _make_service()

    user_id = await user_repo.create_user(
        PHONE, onboarding_status="active", first_name="Dana"
    )
    await user_repo.update_onboarding_status(user_id, "active")

    await service.handle_connection_established(user_id, PHONE)

    assert len(bot.sent) == 0


# --- handle_connection_established_by_id: no phone -------------------------


async def test_connection_established_by_id_no_phone_lookup():
    """User repo without get_phone_by_id → _lookup_phone None → return."""
    user_repo = FakeUserRepoNoPhoneLookup()
    service, bot, _user_repo, _conn, _green_client = _make_service(
        user_repo=user_repo
    )

    await user_repo.create_user(PHONE, onboarding_status="pending")

    # No phone resolvable — should return without sending welcome.
    await service.handle_connection_established_by_id("some-user-id")

    assert len(bot.sent) == 0


# --- handle_disconnect_notification --------------------------------------


async def test_disconnect_notification_active_user():
    """Active user → disconnect notification sent."""
    service, bot, user_repo, _conn, _green_client = _make_service()

    user_id = await user_repo.create_user(
        PHONE, onboarding_status="active", first_name="Dana"
    )
    await user_repo.update_onboarding_status(user_id, "active")

    await service.handle_disconnect_notification(user_id)

    assert len(bot.sent) == 1
    _phone, msg = bot.sent[0]
    assert _phone == PHONE
    assert "התנתק" in msg
    assert "קוד" in msg


async def test_disconnect_notification_pending_user_suppressed():
    """Pending user → no notification (poll handles their case)."""
    service, bot, user_repo, _conn, _green_client = _make_service()

    user_id = await user_repo.create_user(PHONE, onboarding_status="pending")

    await service.handle_disconnect_notification(user_id)

    assert len(bot.sent) == 0


async def test_disconnect_notification_failed_user_suppressed():
    """Failed user → no notification."""
    service, bot, user_repo, _conn, _green_client = _make_service()

    user_id = await user_repo.create_user(PHONE, onboarding_status="failed")
    await user_repo.update_onboarding_status(user_id, "failed")

    await service.handle_disconnect_notification(user_id)

    assert len(bot.sent) == 0


async def test_disconnect_notification_no_phone_lookup():
    """User repo without get_phone_by_id → no crash, no send."""
    user_repo = FakeUserRepoNoPhoneLookup()
    service, bot, _user_repo, _conn, _green_client = _make_service(
        user_repo=user_repo
    )

    await user_repo.create_user(PHONE, onboarding_status="active")

    await service.handle_disconnect_notification("some-user-id")

    assert len(bot.sent) == 0


async def test_disconnect_notification_send_failure_logged():
    """Bot send failure is caught and logged, does not crash."""
    service, bot, user_repo, _conn, _green_client = _make_service()
    bot.send_text_should_fail = True

    user_id = await user_repo.create_user(
        PHONE, onboarding_status="active", first_name="Dana"
    )
    await user_repo.update_onboarding_status(user_id, "active")

    # Should not raise.
    await service.handle_disconnect_notification(user_id)


# --- handle_name_response: invalid phone / unknown / empty name ------------


async def test_name_response_invalid_phone():
    """Invalid phone → returns False."""
    service, _bot, _user_repo, _conn, _green_client = _make_service()

    handled = await service.handle_name_response("not-a-phone", "Dana")
    assert handled is False


async def test_name_response_unknown_user():
    """Unknown user → returns False."""
    service, _bot, _user_repo, _conn, _green_client = _make_service()

    handled = await service.handle_name_response(PHONE, "Dana")
    assert handled is False


async def test_name_response_empty_name():
    """Blank name → returns False."""
    service, _bot, user_repo, _conn, _green_client = _make_service()

    await user_repo.create_user(PHONE, onboarding_status="pending")

    handled = await service.handle_name_response(PHONE, "   ")
    assert handled is False


# --- is_onboarding: invalid phone ------------------------------------------


async def test_is_onboarding_invalid_phone():
    """Invalid phone → returns False."""
    service, _bot, _user_repo, _conn, _green_client = _make_service()

    assert await service.is_onboarding("not-a-phone") is False


# --- _resend_otp: no connection / get_code failure --------------------------


async def test_resend_otp_no_connection():
    """Pending user with no stored connection → returns False, no OTP."""
    service, bot, user_repo, _conn, green_client = _make_service()

    user_id = await user_repo.create_user(PHONE, onboarding_status="pending")
    await user_repo.update_onboarding_status(user_id, "pending")

    handled = await service.handle_resend_request(PHONE)

    assert handled is False
    assert len(green_client.otp_calls) == 0
    # No OTP message sent because no connection exists.
    assert len(bot.sent) == 0


async def test_resend_otp_get_code_fails():
    """getAuthorizationCode failing during resend → error message."""
    green_client = FakeGreenClient(otp_code="87654321")
    service, bot, _user_repo, _conn, green_client = _make_service(
        green_client=green_client
    )

    # Full flow: consent → name → provisioning (OTP sent).
    await _start_and_name(service)
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
