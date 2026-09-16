"""Tests for the OnboardingService.

Tests the OTP-based onboarding flow using fakes for all external
dependencies (bot, provisioner, green client, repos).

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
from echo_v2.services.onboarding import OnboardingService

__all__ = []


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
    """In-memory user repository for onboarding tests."""

    def __init__(self) -> None:
        self._users: dict[str, dict] = {}  # phone -> {id, onboarding, name}
        self._by_id: dict[str, str] = {}  # id -> phone

    async def create_user(
        self,
        phone: str,
        *,
        timezone: str = "Asia/Jerusalem",
        first_name: str | None = None,
        onboarding_status: str = "pending",
    ) -> str:
        if phone in self._users:
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

    async def resolve(self, phone: str) -> tuple[str, str] | None:
        user = self._users.get(phone)
        if user is None:
            return None
        return (user["id"], user["timezone"])


class FakeGreenClient:
    """Fake GreenClient that returns a fixed OTP code."""

    def __init__(self, otp_code: str = "12345678") -> None:
        self._otp_code = otp_code
        self.otp_calls: list[tuple[str, str, int]] = []
        self._state_sequence: list[str | None] = []

    def set_state_sequence(self, states: list[str | None]) -> None:
        """Set the sequence of states returned by get_state_instance."""
        self._state_sequence = states

    async def get_authorization_code(
        self,
        id_instance: str,
        api_token: str,
        phone_number: int,
    ) -> str:
        self.otp_calls.append((id_instance, api_token, phone_number))
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
        if self._state_sequence:
            return self._state_sequence.pop(0)
        return "notAuthorized"

    async def get_qr_ws(self, id_instance: str, api_token: str, **kwargs) -> dict:
        return {"type": "qrCode", "message": "base64data"}

    async def logout(self, id_instance: str, api_token: str) -> None:
        pass

    async def delete_instance(self, instance_id: str) -> None:
        pass


# --- Fixtures --------------------------------------------------------------


@pytest.fixture
def fakes():
    bot = FakeBot()
    user_repo = FakeUserRepo()
    connection_repo = InMemoryWhatsAppConnectionRepository()
    green_client = FakeGreenClient(otp_code="87654321")
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
        webhook_base_url="https://echo.example.com",
        poll_interval=0.01,
        poll_max_attempts=3,
    )
    return service, bot, user_repo, connection_repo, green_client


# --- Tests -----------------------------------------------------------------


async def test_unknown_user_starts_onboarding(fakes):
    """Unknown user consents → user created (pending), name prompt sent."""
    service, bot, user_repo, _conn_repo, green_client = fakes

    await service.start_onboarding("+972546610653")

    # User created with pending status, no name yet.
    user = await user_repo.get_by_phone("+972546610653")
    assert user is not None
    _user_id, onboarding, name = user
    assert onboarding == "pending"
    assert name is None

    # No OTP requested yet (provisioning starts after name).
    assert len(green_client.otp_calls) == 0

    # Bot sent the name prompt.
    assert len(bot.sent) == 1
    _phone, prompt = bot.sent[0]
    assert "איך אפנה" in prompt


async def test_name_response_starts_provisioning(fakes):
    """After sending name, provisioning starts and OTP is sent."""
    service, bot, user_repo, _conn_repo, green_client = fakes

    # Instance becomes ready (notAuthorized → OTP sent) then authorized.
    green_client.set_state_sequence(["notAuthorized", "authorized"])

    await service.start_onboarding("+972546610653")
    # Send the name.
    handled = await service.handle_name_response("+972546610653", "Dana")
    assert handled is True

    # Wait for background provisioning + poll.
    await asyncio.sleep(0.1)

    # Name stored, status active (authorized).
    user = await user_repo.get_by_phone("+972546610653")
    assert user is not None
    _user_id, onboarding, name = user
    assert onboarding == "active"
    assert name == "Dana"

    # OTP was requested.
    assert len(green_client.otp_calls) == 1
    _id_instance, _token, phone_int = green_client.otp_calls[0]
    assert phone_int == 972546610653

    # Bot sent: 1) name prompt 2) name confirmation 3) OTP instructions 4) welcome.
    assert len(bot.sent) == 4
    _phone, confirm = bot.sent[1]
    assert "Dana" in confirm
    _phone, message = bot.sent[2]
    assert "87654321" in message
    assert "הקוד שלך" in message
    _phone, welcome = bot.sent[3]
    assert "היי" in welcome
    assert "Dana" in welcome


async def test_pending_user_resends_name_prompt(fakes):
    """Already-pending user with no name → re-ask for name."""
    service, bot, _user_repo, _conn_repo, green_client = fakes

    # First consent → creates user, asks name.
    await service.start_onboarding("+972546610653")
    assert len(bot.sent) == 1

    # Second start_onboarding (e.g. user taps button again) → re-ask name.
    await service.start_onboarding("+972546610653")
    assert len(bot.sent) == 2
    _phone, prompt = bot.sent[1]
    assert "איך אפנה" in prompt

    # No OTP requested (no name yet, no provisioning).
    assert len(green_client.otp_calls) == 0


async def test_active_user_skipped(fakes):
    """Already-active user sends a message → onboarding does not start."""
    service, bot, user_repo, _conn_repo, green_client = fakes

    # Create a user that's already active.
    user_id = await user_repo.create_user(
        "+972546610653",
        onboarding_status="active",
        first_name="Dana",
    )
    await user_repo.update_onboarding_status(user_id, "active")

    await service.start_onboarding("+972546610653")

    # No OTP requested, no message sent.
    assert len(green_client.otp_calls) == 0
    assert len(bot.sent) == 0


async def test_connection_established_sends_welcome(fakes):
    """Connection established → welcome message sent, status=active."""
    service, bot, user_repo, _conn_repo, _green_client = fakes

    # Set up: user pending with name.
    user_id = await user_repo.create_user("+972546610653", first_name="Dana")
    await user_repo.update_first_name(user_id, "Dana")

    await service.handle_connection_established(user_id, "+972546610653")

    # Status updated to active.
    user = await user_repo.get_by_phone("+972546610653")
    assert user[1] == "active"

    # Welcome sent.
    assert len(bot.sent) == 1
    _phone, welcome = bot.sent[0]
    assert "היי" in welcome
    assert "Dana" in welcome
    assert "Echo" in welcome


async def test_connection_established_uses_default_name(fakes):
    """If no name stored, welcome uses the default name."""
    service, bot, user_repo, _conn_repo, _green_client = fakes

    user_id = await user_repo.create_user("+972546610653")

    await service.handle_connection_established(user_id, "+972546610653")

    user = await user_repo.get_by_phone("+972546610653")
    assert user[1] == "active"
    assert len(bot.sent) == 1
    _phone, welcome = bot.sent[0]
    assert "חבר" in welcome  # default name


async def test_name_response_ignored_if_already_has_name(fakes):
    """Name response is ignored if user already has a name (provisioning)."""
    service, _bot, user_repo, _conn_repo, _green_client = fakes

    await service.start_onboarding("+972546610653")
    handled = await service.handle_name_response("+972546610653", "Dana")
    assert handled is True

    # Second name response — already has a name.
    handled = await service.handle_name_response("+972546610653", "Roni")
    assert handled is False

    # Name unchanged.
    user = await user_repo.get_by_phone("+972546610653")
    assert user[2] == "Dana"


async def test_name_response_ignored_if_not_pending(fakes):
    """Name response is ignored if onboarding is active."""
    service, _bot, user_repo, _conn_repo, _green_client = fakes

    user_id = await user_repo.create_user(
        "+972546610653", onboarding_status="active", first_name="Dana"
    )
    await user_repo.update_onboarding_status(user_id, "active")

    handled = await service.handle_name_response("+972546610653", "Roni")
    assert handled is False


async def test_is_onboarding_true_for_pending(fakes):
    """is_onboarding returns True for pending users."""
    service, _bot, _user_repo, _conn_repo, _green_client = fakes

    await service.start_onboarding("+972546610653")
    assert await service.is_onboarding("+972546610653") is True


async def test_is_onboarding_false_for_active(fakes):
    """is_onboarding returns False for active users."""
    service, _bot, user_repo, _conn_repo, _green_client = fakes

    user_id = await user_repo.create_user("+972546610653", onboarding_status="active")
    await user_repo.update_onboarding_status(user_id, "active")

    assert await service.is_onboarding("+972546610653") is False


async def test_is_onboarding_false_for_unknown(fakes):
    """is_onboarding returns False for unknown users."""
    service, _bot, _user_repo, _conn_repo, _green_client = fakes

    assert await service.is_onboarding("+972546610653") is False


async def test_resend_request_sends_new_otp(fakes):
    """User sends 'קוד' while pending with a connection → re-sends OTP."""
    service, bot, _user_repo, _conn_repo, green_client = fakes

    # Full flow: consent → name → provisioning (OTP sent).
    await service.start_onboarding("+972546610653")
    await service.handle_name_response("+972546610653", "Dana")
    await asyncio.sleep(0.1)
    assert len(green_client.otp_calls) == 1

    # Resend.
    await service.handle_resend_request("+972546610653")
    await asyncio.sleep(0.1)
    assert len(green_client.otp_calls) == 2

    # Last message is the re-sent OTP.
    _phone, message = bot.sent[-1]
    assert "87654321" in message


async def test_connection_established_by_id(fakes):
    """handle_connection_established_by_id resolves phone and sends welcome."""
    service, bot, user_repo, _conn_repo, _green_client = fakes

    user_id = await user_repo.create_user("+972546610653", first_name="Dana")
    await user_repo.update_first_name(user_id, "Dana")

    await service.handle_connection_established_by_id(user_id)

    user = await user_repo.get_by_phone("+972546610653")
    assert user[1] == "active"
    assert len(bot.sent) == 1


async def test_invalid_phone_ignored(fakes):
    """Invalid phone number is ignored gracefully."""
    service, bot, _user_repo, _conn_repo, green_client = fakes

    await service.start_onboarding("not-a-phone")

    assert len(green_client.otp_calls) == 0
    assert len(bot.sent) == 0


async def test_instance_not_ready_sends_failure(fakes):
    """If instance never becomes ready (stuck in 'starting'), user gets failure."""
    service, bot, user_repo, _conn_repo, green_client = fakes

    # Simulate instance stuck in "starting" — never reaches notAuthorized/authorized.
    green_client.set_state_sequence(["starting"] * 30)

    await service.start_onboarding("+972546610653")
    await service.handle_name_response("+972546610653", "Dana")
    # poll_interval=0.01, poll_max_attempts=3 → ~0.03s before timeout
    await asyncio.sleep(0.5)

    user = await user_repo.get_by_phone("+972546610653")
    assert user is not None
    assert user[1] == "failed"

    # Bot sent: 1) name prompt 2) name confirm 3) failure message.
    assert len(bot.sent) == 3
    _phone, failure_msg = bot.sent[2]
    assert "מצטער" in failure_msg
    assert len(green_client.otp_calls) == 0


async def test_instance_starting_then_not_authorized(fakes):
    """Instance goes through 'starting' before 'notAuthorized' — OTP sent."""
    service, bot, _user_repo, _conn_repo, green_client = fakes

    # Simulate: starting → notAuthorized (ready for OTP), then times out.
    green_client.set_state_sequence(["starting", "notAuthorized"])

    await service.start_onboarding("+972546610653")
    await service.handle_name_response("+972546610653", "Dana")
    await asyncio.sleep(0.5)

    # OTP was requested after reaching notAuthorized.
    assert len(green_client.otp_calls) == 1
    # Bot sent: 1) name prompt 2) name confirm 3) OTP instructions 4) timeout.
    assert len(bot.sent) == 4
    _phone, message = bot.sent[2]
    assert "87654321" in message
    _phone, timeout = bot.sent[3]
    assert "לא התחברת בזמן" in timeout


async def test_poll_until_authorized_completes_onboarding(fakes):
    """Poll finds 'authorized' → user becomes active, welcome sent."""
    service, bot, user_repo, _conn_repo, green_client = fakes

    user_id = await user_repo.create_user("+972546610653", first_name="Dana")
    await user_repo.update_first_name(user_id, "Dana")

    green_client.set_state_sequence(["authorized"])

    await service._poll_until_authorized(user_id, "+972546610653", "inst-1", "token-1")

    user = await user_repo.get_by_phone("+972546610653")
    assert user[1] == "active"
    assert len(bot.sent) == 1
    _phone, welcome = bot.sent[0]
    assert "היי" in welcome


async def test_poll_until_authorized_times_out(fakes):
    """Poll sees notAuthorized but never 'authorized' → times out, user stays pending.

    If the instance became ready (OTP sent) but the user never authorized,
    the user remains ``pending`` so they can retry via 'קוד'.
    """
    service, bot, user_repo, _conn_repo, green_client = fakes

    user_id = await user_repo.create_user("+972546610653", first_name="Dana")

    # Always returns notAuthorized (instance ready, OTP sent, but never authorized).
    green_client.set_state_sequence(["notAuthorized"] * 30)

    await service._poll_until_authorized(user_id, "+972546610653", "inst-1", "token-1")

    # User remains pending (can retry via 'קוד').
    user = await user_repo.get_by_phone("+972546610653")
    assert user[1] == "pending"
    # OTP + timeout message sent.
    assert len(bot.sent) == 2
    _phone, otp_msg = bot.sent[0]
    assert "הקוד שלך" in otp_msg
    _phone, msg = bot.sent[1]
    assert "לא התחברת בזמן" in msg
    assert "קוד" in msg


async def test_resolve_context_returns_none_for_unknown(fakes):
    """resolve_context returns None for unknown phone."""
    service, _bot, _user_repo, _conn_repo, _green_client = fakes

    ctx = await service.resolve_context("+972546610653")
    assert ctx is None


async def test_resolve_context_returns_typed_context(fakes):
    """resolve_context returns a typed OnboardingContext for known users."""
    service, _bot, user_repo, _conn_repo, _green_client = fakes

    user_id = await user_repo.create_user("+972546610653", first_name="Dana")

    ctx = await service.resolve_context("+972546610653")
    assert ctx is not None
    assert ctx.user_id == user_id
    assert ctx.phone == "+972546610653"
    assert ctx.onboarding_status == "pending"
    assert ctx.first_name == "Dana"
    assert ctx.connection is None
