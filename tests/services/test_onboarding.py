"""Tests for the OnboardingService.

Tests the OTP-based onboarding flow using fakes for all external
dependencies (bot, provisioner, green client, repos).
"""

from __future__ import annotations

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
        phone = self._by_id.get(user_id)
        if phone is None:
            return
        self._users[phone]["name"] = first_name
        self._users[phone]["onboarding"] = "active"

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
        return "authorized"

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
    )
    return service, bot, user_repo, connection_repo, green_client


# --- Tests -----------------------------------------------------------------


async def test_unknown_user_starts_onboarding(fakes):
    """Unknown user sends a message → onboarding starts, OTP sent."""
    service, bot, user_repo, _conn_repo, green_client = fakes

    await service.handle_unknown_user("+972546610653")

    # User created with pending status.
    user = await user_repo.get_by_phone("+972546610653")
    assert user is not None
    _user_id, onboarding, name = user
    assert onboarding == "pending"
    assert name is None

    # OTP was requested.
    assert len(green_client.otp_calls) == 1
    _id_instance, _token, phone_int = green_client.otp_calls[0]
    assert phone_int == 972546610653

    # Bot sent the OTP instructions.
    assert len(bot.sent) == 1
    _phone, message = bot.sent[0]
    assert "87654321" in message
    assert "הקוד שלך" in message


async def test_pending_user_resends_otp(fakes):
    """Already-pending user triggers onboarding again → re-sends OTP, no new instance."""
    service, _bot, user_repo, _conn_repo, green_client = fakes

    # First message starts onboarding.
    await service.handle_unknown_user("+972546610653")
    assert len(green_client.otp_calls) == 1

    # Second message (same user) should re-send OTP, not create a new instance.
    await service.handle_unknown_user("+972546610653")
    assert len(green_client.otp_calls) == 2  # re-requested OTP

    # Only one user exists.
    user = await user_repo.get_by_phone("+972546610653")
    assert user is not None


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

    await service.handle_unknown_user("+972546610653")

    # No OTP requested, no message sent.
    assert len(green_client.otp_calls) == 0
    assert len(bot.sent) == 0


async def test_connection_established_sends_welcome(fakes):
    """Connection established → welcome message sent, status=connected."""
    service, bot, user_repo, _conn_repo, _green_client = fakes

    await service.handle_unknown_user("+972546610653")
    user = await user_repo.get_by_phone("+972546610653")
    user_id = user[0]

    await service.handle_connection_established(user_id, "+972546610653")

    # Status updated to connected.
    user = await user_repo.get_by_phone("+972546610653")
    assert user[1] == "connected"

    # Welcome message sent (second message, after OTP).
    assert len(bot.sent) == 2
    _phone, welcome = bot.sent[1]
    assert "היי" in welcome
    assert "Echo" in welcome
    assert "איך אפנה אליך" in welcome


async def test_name_response_completes_onboarding(fakes):
    """After connection, user sends name → stored, status=active."""
    service, bot, user_repo, _conn_repo, _green_client = fakes

    await service.handle_unknown_user("+972546610653")
    user = await user_repo.get_by_phone("+972546610653")
    user_id = user[0]

    await service.handle_connection_established(user_id, "+972546610653")

    # User sends their name.
    handled = await service.handle_name_response("+972546610653", "Dana")
    assert handled is True

    user = await user_repo.get_by_phone("+972546610653")
    assert user[1] == "active"
    assert user[2] == "Dana"

    # Confirmation message sent.
    assert len(bot.sent) == 3
    _phone, confirm = bot.sent[2]
    assert "Dana" in confirm
    assert "נחמד להכיר" in confirm


async def test_name_response_ignored_if_not_connected(fakes):
    """Name response is ignored if onboarding is still pending."""
    service, _bot, _user_repo, _conn_repo, _green_client = fakes

    await service.handle_unknown_user("+972546610653")

    handled = await service.handle_name_response("+972546610653", "Dana")
    assert handled is False


async def test_is_onboarding_true_for_pending(fakes):
    """is_onboarding returns True for pending users."""
    service, _bot, _user_repo, _conn_repo, _green_client = fakes

    await service.handle_unknown_user("+972546610653")
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
    """User sends 'קוד' while pending → re-sends OTP."""
    service, bot, _user_repo, _conn_repo, green_client = fakes

    await service.handle_unknown_user("+972546610653")
    assert len(green_client.otp_calls) == 1

    await service.handle_resend_request("+972546610653")
    assert len(green_client.otp_calls) == 2

    # Second message sent.
    assert len(bot.sent) == 2
    _phone, message = bot.sent[1]
    assert "87654321" in message


async def test_connection_established_by_id(fakes):
    """handle_connection_established_by_id resolves phone and sends welcome."""
    service, bot, user_repo, _conn_repo, _green_client = fakes

    await service.handle_unknown_user("+972546610653")
    user = await user_repo.get_by_phone("+972546610653")
    user_id = user[0]

    await service.handle_connection_established_by_id(user_id)

    user = await user_repo.get_by_phone("+972546610653")
    assert user[1] == "connected"
    assert len(bot.sent) == 2  # OTP + welcome


async def test_invalid_phone_ignored(fakes):
    """Invalid phone number is ignored gracefully."""
    service, bot, _user_repo, _conn_repo, green_client = fakes

    await service.handle_unknown_user("not-a-phone")

    assert len(green_client.otp_calls) == 0
    assert len(bot.sent) == 0
