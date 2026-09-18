"""Tests for the OnboardingService.

Tests the click-driven onboarding flow using fakes for all external
dependencies (bot, provisioner, green client, repos).

Flow (click-driven — name collected before pairing; QR only on click):
1. start_onboarding → create user (pending) + ask name
2. handle_name_response → store name + send [חבר אותי] button
3. handle_onboarding_connect → start_pairing (pool hit = QR immediately,
   pool miss = background create → [הצג QR] button)
4. handle_onboarding_show_qr → fetch + send QR + poll
5. _poll_until_authorized → send QR/OTP, then complete to active
6. handle_connection_established → status=active + welcome
"""

from __future__ import annotations

import asyncio
import base64
import secrets
from dataclasses import dataclass, field

import pytest

from echo_v2.integrations.green.provisioner import GreenProvisioner
from echo_v2.persistence.whatsapp_connections import (
    InMemoryWhatsAppConnectionRepository,
)
from echo_v2.ports.whatsapp import ConnectionRef, ProviderCredentials
from echo_v2.services.onboarding import OnboardingService

__all__ = []


# --- Fakes -----------------------------------------------------------------


@dataclass
class FakeBot:
    """Records sent messages for assertion."""

    sent: list[tuple[str, str]] = field(default_factory=list)
    sent_images: list[tuple[str, bytes, str | None]] = field(default_factory=list)
    sent_buttons: list[tuple[str, str, list[dict]]] = field(default_factory=list)
    send_image_should_fail: bool = False

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
        if self.send_image_should_fail:
            raise RuntimeError("image boom")
        self.sent_images.append((user_phone, image_bytes, caption))
        return "fake-image-msg-id"

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
        self.sent_buttons.append((user_phone, body_text, buttons))
        return "fake-buttons-msg-id"


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
        self._default_state: str = "notAuthorized"
        self.qr_response: dict = {
            "type": "qrCode",
            "message": base64.b64encode(b"fake-qr-png").decode("utf-8"),
        }
        self.qr_should_fail: bool = False

    def set_state_sequence(self, states: list[str | None]) -> None:
        """Set the sequence of states returned by get_state_instance."""
        self._state_sequence = states

    def set_default_state(self, state: str) -> None:
        """Set the default state returned when the sequence is exhausted."""
        self._default_state = state

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
        return self._default_state

    async def get_qr_ws(self, id_instance: str, api_token: str, **kwargs) -> dict:
        if self.qr_should_fail:
            raise RuntimeError("QR fetch boom")
        return self.qr_response

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


async def test_name_response_sends_connect_button(fakes):
    """After sending name, [חבר אותי] button is sent (no auto-provisioning)."""
    service, bot, user_repo, _conn_repo, green_client = fakes

    await service.start_onboarding("+972546610653")
    handled = await service.handle_name_response("+972546610653", "Dana")
    assert handled is True

    # Name stored, status pending (NOT active — no auto-provisioning).
    user = await user_repo.get_by_phone("+972546610653")
    assert user is not None
    _user_id, onboarding, name = user
    assert onboarding == "pending"
    assert name == "Dana"

    # No OTP requested, no QR sent (no provisioning started).
    assert len(green_client.otp_calls) == 0
    assert len(bot.sent_images) == 0

    # Bot sent: 1) name prompt. Then a buttons message with [חבר אותי].
    assert len(bot.sent) == 1
    assert len(bot.sent_buttons) == 1
    _phone, body, buttons = bot.sent_buttons[0]
    assert "Dana" in body
    assert any(b["id"] == "onboarding:connect" for b in buttons)


async def test_connect_click_pool_miss_creates_instance(fakes):
    """Pool miss: [חבר אותי] → "preparing" → background creates → [הצג QR]."""
    service, bot, user_repo, _conn_repo, green_client = fakes

    # Pool is None (default fixture) → always pool miss.
    green_client.set_state_sequence(["notAuthorized", "authorized"])

    await service.start_onboarding("+972546610653")
    await service.handle_name_response("+972546610653", "Dana")

    # User clicks [חבר אותי].
    await service.handle_onboarding_connect("+972546610653")
    # Background task runs createInstance + ready poll.
    await asyncio.sleep(0.2)

    # Connection saved as PROVISIONING then PAIRING_REQUIRED.
    user = await user_repo.get_by_phone("+972546610653")
    assert user is not None
    _uid, onboarding, _name = user
    assert onboarding == "pending"  # not active until authorized

    # Bot sent "preparing" text + "ready" button (no QR yet).
    preparing_msgs = [m for _p, m in bot.sent if "מכין" in m]
    assert len(preparing_msgs) == 1
    ready_buttons = [
        (p, b, btns) for p, b, btns in bot.sent_buttons
        if any(x["id"] == "onboarding:show_qr" for x in btns)
    ]
    assert len(ready_buttons) == 1
    assert len(bot.sent_images) == 0  # no QR sent yet

    # User clicks [הצג QR].
    await service.handle_onboarding_show_qr("+972546610653")
    await asyncio.sleep(0.2)

    # QR image sent.
    assert len(bot.sent_images) == 1
    # User becomes active (authorized).
    user = await user_repo.get_by_phone("+972546610653")
    assert user[1] == "active"


async def test_connect_click_pool_hit_sends_qr_immediately(fakes):
    """Pool hit: [חבר אותי] → QR sent immediately (no "preparing")."""
    from echo_v2.persistence.green_instance_pool import (
        InMemoryGreenInstancePoolRepository,
    )
    from echo_v2.services.green_instance_pool import GreenInstancePool

    service, bot, user_repo, connection_repo, green_client = fakes

    # Build a pool with one available instance.
    pool_repo = InMemoryGreenInstancePoolRepository()
    pool = GreenInstancePool(
        provisioner=service._provisioner,
        green_client=green_client,
        repo=pool_repo,
        connection_repo=connection_repo,
        webhook_base_url="https://echo.example.com",
        target_size=1,
        ready_poll_interval=0.01,
        ready_max_attempts=3,
    )
    # Manually create an available pool row.
    row_id = await pool_repo.reserve_creation_slot(1)
    assert row_id is not None
    await pool_repo.mark_created(
        row_id,
        ConnectionRef(provider="green", provider_connection_id="pool-inst-1"),
        ProviderCredentials(data=b"pool-token-1"),
        b"fake-hash",
    )
    await pool_repo.mark_available(row_id)

    # Reconstruct service with pool.
    service_with_pool = OnboardingService(
        bot=bot,
        user_repo=user_repo,
        connection_repo=connection_repo,
        provisioner=service._provisioner,
        green_client=green_client,
        webhook_base_url="https://echo.example.com",
        poll_interval=0.01,
        poll_max_attempts=3,
        pool=pool,
    )

    green_client.set_default_state("authorized")
    green_client.set_state_sequence([])

    await service_with_pool.start_onboarding("+972546610653")
    await service_with_pool.handle_name_response("+972546610653", "Dana")
    await service_with_pool.handle_onboarding_connect("+972546610653")
    await asyncio.sleep(0.3)

    # QR sent immediately (pool hit).
    assert len(bot.sent_images) == 1
    # No "preparing" message.
    preparing_msgs = [m for _p, m in bot.sent if "מכין" in m]
    assert len(preparing_msgs) == 0
    # User becomes active.
    user = await user_repo.get_by_phone("+972546610653")
    assert user[1] == "active"
    # Pool row finalized (deleted).
    available = await pool_repo.get_by_state("available")
    assert len(available) == 0
    # Clean up the refill task.
    await pool.aclose()


async def test_double_click_connect_pool_miss_creates_once(fakes):
    """Double-click [חבר אותי] + pool miss → createInstance called once."""
    service, bot, _user_repo, _conn_repo, green_client = fakes

    green_client.set_state_sequence(["notAuthorized"] * 10)

    await service.start_onboarding("+972546610653")
    await service.handle_name_response("+972546610653", "Dana")

    # First click starts preparation.
    await service.handle_onboarding_connect("+972546610653")
    # Second click immediately after — should see "still preparing".
    await service.handle_onboarding_connect("+972546610653")
    await asyncio.sleep(0.2)

    # Only one "preparing" message (second click sends "still preparing").
    preparing_msgs = [m for _p, m in bot.sent if "מכין חיבור" in m]
    assert len(preparing_msgs) == 1
    still_msgs = [m for _p, m in bot.sent if "עדיין מכין" in m]
    assert len(still_msgs) == 1


async def test_connect_click_no_name_reasks_name(fakes):
    """[חבר אותי] from a user with no name → re-asks for name."""
    service, bot, _user_repo, _conn_repo, _green_client = fakes

    # Create user with no name.
    await service.start_onboarding("+972546610653")
    # Don't send name — click connect directly (stale/forged callback).
    await service.handle_onboarding_connect("+972546610653")

    # Name prompt re-sent.
    assert len(bot.sent) >= 2
    _phone, prompt = bot.sent[-1]
    assert "איך אפנה" in prompt


async def test_start_onboarding_pending_with_name_no_connection_sends_button(fakes):
    """Re-entry: pending + name + no connection → [חבר אותי] button (no QR)."""
    service, bot, _user_repo, _conn_repo, _green_client = fakes

    await service.start_onboarding("+972546610653")
    await service.handle_name_response("+972546610653", "Dana")

    bot.sent_buttons.clear()
    bot.sent.clear()

    # Re-enter onboarding.
    await service.start_onboarding("+972546610653")

    # Button sent, no QR.
    assert len(bot.sent_images) == 0
    assert len(bot.sent_buttons) == 1
    _phone, _body, buttons = bot.sent_buttons[0]
    assert any(b["id"] == "onboarding:connect" for b in buttons)


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
    """User sends 'קוד' while pending with a connection → re-sends OTP (fallback)."""
    service, bot, _user_repo, _conn_repo, green_client = fakes

    # Full flow: consent → name → connect click → show_qr click → QR sent.
    green_client.set_state_sequence(["notAuthorized", "authorized"])
    await service.start_onboarding("+972546610653")
    await service.handle_name_response("+972546610653", "Dana")
    await service.handle_onboarding_connect("+972546610653")
    await asyncio.sleep(0.2)
    # Pool miss: instance ready, [הצג QR] button sent (no QR yet).
    assert len(bot.sent_images) == 0
    # Click [הצג QR] to get QR.
    await service.handle_onboarding_show_qr("+972546610653")
    await asyncio.sleep(0.2)
    # Default path sends QR, not OTP.
    assert len(green_client.otp_calls) == 0
    assert len(bot.sent_images) == 1

    # Resend OTP via 'קוד' command.
    await service.handle_resend_request("+972546610653")
    await asyncio.sleep(0.1)
    assert len(green_client.otp_calls) == 1

    # Last text message is the re-sent OTP.
    _phone, message = bot.sent[-1]
    assert "87654321" in message


async def test_handle_onboarding_qr_resends_qr(fakes):
    """User sends 'qr' while pending with a connection → re-sends QR image."""
    service, bot, _user_repo, _conn_repo, green_client = fakes

    # Full flow: consent → name → connect → show_qr → QR sent.
    green_client.set_state_sequence(["notAuthorized", "authorized"])
    await service.start_onboarding("+972546610653")
    await service.handle_name_response("+972546610653", "Dana")
    await service.handle_onboarding_connect("+972546610653")
    await asyncio.sleep(0.2)
    await service.handle_onboarding_show_qr("+972546610653")
    await asyncio.sleep(0.2)
    assert len(bot.sent_images) == 1

    # Re-send QR via 'qr' command.
    handled = await service.handle_onboarding_qr("+972546610653")
    assert handled is True
    await asyncio.sleep(0.1)

    # Second QR image sent.
    assert len(bot.sent_images) == 2
    # No OTP requested (QR path).
    assert len(green_client.otp_calls) == 0


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
    await service.handle_onboarding_connect("+972546610653")
    # poll_interval=0.01, poll_max_attempts=3 → ~0.03s before timeout
    await asyncio.sleep(0.5)

    user = await user_repo.get_by_phone("+972546610653")
    assert user is not None
    assert user[1] == "failed"

    # Bot sent: 1) name prompt 2) "preparing" 3) failure message.
    assert len(bot.sent) == 3
    _phone, failure_msg = bot.sent[2]
    assert "מצטער" in failure_msg
    assert len(green_client.otp_calls) == 0


async def test_instance_starting_then_not_authorized(fakes):
    """Instance goes through 'starting' before 'notAuthorized' — QR sent on click."""
    service, bot, _user_repo, _conn_repo, green_client = fakes

    # Simulate: starting → notAuthorized (ready for QR), then times out.
    green_client.set_state_sequence(["starting", "notAuthorized"])

    await service.start_onboarding("+972546610653")
    await service.handle_name_response("+972546610653", "Dana")
    await service.handle_onboarding_connect("+972546610653")
    await asyncio.sleep(0.3)

    # No OTP requested, no QR yet (pool miss: [הצג QR] button sent).
    assert len(green_client.otp_calls) == 0
    assert len(bot.sent_images) == 0
    ready_buttons = [
        (p, b, btns) for p, b, btns in bot.sent_buttons
        if any(x["id"] == "onboarding:show_qr" for x in btns)
    ]
    assert len(ready_buttons) == 1

    # User clicks [הצג QR] → QR sent + poll.
    await service.handle_onboarding_show_qr("+972546610653")
    await asyncio.sleep(0.3)

    # QR image sent after click.
    assert len(bot.sent_images) == 1
    # Bot sent text: 1) name prompt 2) "preparing" 3) timeout.
    assert len(bot.sent) == 3
    _phone, timeout = bot.sent[2]
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

    If the instance became ready (QR sent) but the user never authorized,
    the user remains ``pending`` so they can retry via 'qr' or 'קוד'.
    """
    service, bot, user_repo, _conn_repo, green_client = fakes

    user_id = await user_repo.create_user("+972546610653", first_name="Dana")

    # Always returns notAuthorized (instance ready, QR sent, but never authorized).
    green_client.set_state_sequence(["notAuthorized"] * 30)

    await service._poll_until_authorized(user_id, "+972546610653", "inst-1", "token-1")

    # User remains pending (can retry via 'qr' or 'קוד').
    user = await user_repo.get_by_phone("+972546610653")
    assert user[1] == "pending"
    # QR image sent + timeout text message.
    assert len(bot.sent_images) == 1
    assert len(bot.sent) == 1
    _phone, msg = bot.sent[0]
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


async def test_qr_fetch_failure_falls_back_to_otp(fakes):
    """If QR fetch fails, onboarding falls back to OTP."""
    service, bot, _user_repo, _conn_repo, green_client = fakes

    # QR fetch will fail.
    green_client.qr_should_fail = True
    # Instance becomes ready then authorized.
    green_client.set_state_sequence(["notAuthorized", "authorized"])

    await service.start_onboarding("+972546610653")
    await service.handle_name_response("+972546610653", "Dana")
    await service.handle_onboarding_connect("+972546610653")
    await asyncio.sleep(0.2)
    # Pool miss: instance ready, [הצג QR] button sent.
    await service.handle_onboarding_show_qr("+972546610653")
    await asyncio.sleep(0.2)

    # No QR image sent (fetch failed).
    assert len(bot.sent_images) == 0
    # OTP fallback was used.
    assert len(green_client.otp_calls) == 1
    # OTP instructions + fallback notice sent.
    # bot.sent: 1) name prompt 2) "preparing" 3) fallback notice 4) OTP msg
    _phone, fallback_notice = bot.sent[2]
    assert "קוד טלפון" in fallback_notice or "QR" in fallback_notice
    _phone, otp_msg = bot.sent[3]
    assert "87654321" in otp_msg
    assert "הקוד שלך" in otp_msg


async def test_qr_send_image_failure_falls_back_to_otp(fakes):
    """If bot.send_image fails, onboarding falls back to OTP."""
    service, bot, _user_repo, _conn_repo, green_client = fakes

    # send_image will fail.
    bot.send_image_should_fail = True
    # Instance becomes ready then authorized.
    green_client.set_state_sequence(["notAuthorized", "authorized"])

    await service.start_onboarding("+972546610653")
    await service.handle_name_response("+972546610653", "Dana")
    await service.handle_onboarding_connect("+972546610653")
    await asyncio.sleep(0.2)
    await service.handle_onboarding_show_qr("+972546610653")
    await asyncio.sleep(0.2)

    # No QR image sent (send failed).
    assert len(bot.sent_images) == 0
    # OTP fallback was used.
    assert len(green_client.otp_calls) == 1
    # OTP instructions sent (before the welcome).
    otp_msgs = [msg for _p, msg in bot.sent if "87654321" in msg]
    assert len(otp_msgs) == 1
    assert "הקוד שלך" in otp_msgs[0]


async def test_qr_already_authorized_completes_onboarding(fakes):
    """If QR returns ALREADY_AUTHORIZED, onboarding completes immediately."""
    service, bot, user_repo, _conn_repo, green_client = fakes

    # QR returns alreadyLogged → ALREADY_AUTHORIZED.
    green_client.qr_response = {"type": "alreadyLogged"}
    # Instance is notAuthorized (triggers QR fetch) then authorized.
    green_client.set_state_sequence(["notAuthorized", "authorized"])

    user_id = await user_repo.create_user("+972546610653", first_name="Dana")
    await user_repo.update_first_name(user_id, "Dana")

    await service._poll_until_authorized(user_id, "+972546610653", "inst-1", "token-1")

    # No QR image sent, no OTP requested.
    assert len(bot.sent_images) == 0
    assert len(green_client.otp_calls) == 0
    # Welcome sent (onboarding completed via ALREADY_AUTHORIZED).
    assert len(bot.sent) >= 1
    _phone, welcome = bot.sent[0]
    assert "היי" in welcome
