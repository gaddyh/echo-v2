"""OnboardingService — OTP-based WhatsApp onboarding flow.

Flow:
1. Unknown user messages the Echo bot.
2. Service creates a user row (phone, default timezone, default name).
3. Service creates a Green API instance via the provisioner.
4. Service generates a webhook token, configures the instance.
5. Service calls Green's ``getAuthorizationCode`` to get an 8-digit OTP.
6. Service sends the OTP + instructions to the user via the bot.
7. User opens WhatsApp → Settings → Linked Devices → Link with phone number.
8. User enters the 8-digit code.
9. Green API fires ``stateInstanceChanged`` → webhook → connection status updated.
10. Service detects connection → sends welcome message, asks for name.

Idempotency:
- If onboarding is already ``pending``, don't create a second instance.
  Re-send the OTP if the user asks.
- If onboarding is ``connected`` or ``active``, skip.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from typing import Protocol, runtime_checkable

from echo_v2.integrations.green.provisioner import GreenProvisioner
from echo_v2.persistence.identity import PhoneParseError, normalize_phone_e164
from echo_v2.persistence.whatsapp_connections import (
    StoredConnection,
    WhatsAppConnectionRepository,
)
from echo_v2.ports.bot import BotChannel
from echo_v2.ports.whatsapp import (
    ConnectionConfig,
    ConnectionStatus,
    WhatsAppEventSubscription,
)

__all__ = ["OnboardingService", "UserRepository"]

_logger = logging.getLogger("echo_v2.services.onboarding")

_DEFAULT_TIMEZONE = "Asia/Jerusalem"
_DEFAULT_NAME = "חבר"

# Onboarding instructions sent to the user.
_OTP_INSTRUCTIONS = (
    "הקוד שלך: {code}\n\n"
    "כדי לחבר את WhatsApp:\n"
    "1. פתח את WhatsApp בטלפון\n"
    "2. הגדרות ← מכשירים מקושרים ← קשר מכשיר\n"
    "3. בחר 'קשר עם מספר טלפון'\n"
    "4. הזן את הקוד: {code}\n\n"
    "הקוד בתוקף למשך כ-2.5 דקות."
)

_WELCOME_MESSAGE = (
    "היי {name}! 👋\n\n"
    "אני Echo. מעכשיו אני עוקב אחרי השיחות שלך "
    "ומזכיר לך מה מחכה לתשובה.\n\n"
    "כל בוקר תקבל סיכום של השיחות שמחכות לך.\n\n"
    "איך אפנה אליך? (שלח את השם שלך)"
)

_ALREADY_ONBOARDING = (
    "אני כבר מכין את החיבור שלך. שלח 'קוד' כדי לקבל את הקוד מחדש."
)

_RESEND_KEYWORD = "קוד"


@runtime_checkable
class UserRepository(Protocol):
    """Create and look up users for onboarding."""

    async def create_user(
        self,
        phone: str,
        *,
        timezone: str = _DEFAULT_TIMEZONE,
        first_name: str | None = None,
        onboarding_status: str = "pending",
    ) -> str:
        """Create a new user. Returns the user_id. Raises if the phone already exists."""
        ...

    async def get_by_phone(self, phone: str) -> tuple[str, str | None, str | None] | None:
        """Return (user_id, onboarding_status, first_name) or None."""
        ...

    async def update_onboarding_status(
        self,
        user_id: str,
        status: str,
    ) -> None:
        """Update the user's onboarding_status."""
        ...

    async def update_first_name(
        self,
        user_id: str,
        first_name: str,
    ) -> None:
        """Update the user's first_name and set onboarding_status='active'."""
        ...


class OnboardingService:
    """Orchestrates the OTP-based WhatsApp onboarding flow.

    Dependencies:
    * ``bot`` — sends OTP + instructions + welcome to the user.
    * ``user_repo`` — creates and updates users.
    * ``connection_repo`` — stores the Green API connection.
    * ``provisioner`` — creates Green instances and configures webhooks.
    * ``green_client`` — calls ``getAuthorizationCode`` for the OTP.
    * ``webhook_base_url`` — the public URL Green should send webhooks to.
    """

    def __init__(
        self,
        *,
        bot: BotChannel,
        user_repo: UserRepository,
        connection_repo: WhatsAppConnectionRepository,
        provisioner: GreenProvisioner,
        green_client,  # GreenClient — avoid circular import
        webhook_base_url: str,
        poll_interval: float = 5.0,
        poll_max_attempts: int = 24,
    ) -> None:
        self._bot = bot
        self._user_repo = user_repo
        self._connection_repo = connection_repo
        self._provisioner = provisioner
        self._green_client = green_client
        self._webhook_base_url = webhook_base_url.rstrip("/")
        self._poll_interval = poll_interval
        self._poll_max_attempts = poll_max_attempts

    async def handle_unknown_user(self, phone: str) -> None:
        """Start onboarding for an unknown user who messaged the bot.

        Idempotent: if onboarding is already pending, re-send OTP instructions
        instead of creating a new instance.

        The instance creation (which can take 1-2 minutes) runs in the
        background — we send an immediate "please wait" message, then
        fire off the provisioning + OTP as a background task.
        """
        normalized = self._normalize_phone(phone)
        if normalized is None:
            _logger.warning("onboarding: invalid phone %s", phone)
            return

        # Check if user already exists.
        existing = await self._user_repo.get_by_phone(normalized)
        if existing is not None:
            user_id, onboarding_status, _name = existing
            if onboarding_status in ("pending",):
                # Already onboarding — re-send instructions.
                await self._resend_otp(user_id, normalized)
                return
            if onboarding_status in ("connected", "active"):
                # Already onboarded — not our problem.
                return
            # ``failed`` — let them try again by creating a new instance.

        # Create the user.
        try:
            user_id = await self._user_repo.create_user(
                normalized,
                timezone=_DEFAULT_TIMEZONE,
                first_name=None,
                onboarding_status="pending",
            )
        except Exception:
            _logger.exception("onboarding: failed to create user %s", normalized)
            return

        # Send immediate "please wait" message — instance creation takes 1-2 min.
        await self._bot.send_text(
            normalized,
            "מחבר אותך ל-Echo... זה יכול לקחת דקה-שתיים. רגע ותקבל את הקוד. ⏳",
        )

        # Fire off the provisioning + OTP in the background.
        import asyncio
        asyncio.create_task(
            self._provision_and_send_otp(user_id, normalized)
        )

    async def _provision_and_send_otp(
        self,
        user_id: str,
        phone: str,
    ) -> None:
        """Create Green instance + get OTP + send instructions.

        Runs in the background — instance creation can take 1-2 minutes.
        Updates onboarding_status to ``failed`` on error.
        """
        # Create Green instance + configure webhook.
        webhook_token = secrets.token_urlsafe(32)
        webhook_url = f"{self._webhook_base_url}/webhooks/whatsapp/green"
        config = ConnectionConfig(
            webhook_url=webhook_url,
            webhook_token=webhook_token,
            subscriptions=WhatsAppEventSubscription(),
        )

        try:
            created = await self._provisioner.create_connection(config)
        except Exception:
            _logger.exception("onboarding: failed to create Green instance for %s", phone)
            await self._user_repo.update_onboarding_status(user_id, "failed")
            await self._bot.send_text(
                phone,
                "מצטער, לא הצלחתי ליצור את החיבור. נסה שוב מאוחר יותר.",
            )
            return

        # Store the connection.
        token_hash = hashlib.sha256(webhook_token.encode("utf-8")).digest()
        conn = StoredConnection(
            user_id=user_id,
            ref=created.ref,
            credentials=created.credentials,
            webhook_token_hash=token_hash,
            status=ConnectionStatus.PROVISIONING,
        )
        await self._connection_repo.save(conn)

        # Wait for the instance to be ready (Green API: poll getStateInstance
        # until it returns "notAuthorized" — the instance is still being
        # created for a few seconds after createInstance returns).
        api_token = created.credentials.data.decode("utf-8")
        ready = await self._wait_for_instance_ready(
            created.ref.provider_connection_id,
            api_token,
        )
        if not ready:
            _logger.error("onboarding: instance not ready for %s", phone)
            await self._user_repo.update_onboarding_status(user_id, "failed")
            await self._bot.send_text(
                phone,
                "מצטער, היצירה של החיבור לקחה יותר מדי זמן. נסה שוב.",
            )
            return

        # Get the OTP.
        phone_int = int(phone.lstrip("+"))
        try:
            code = await self._green_client.get_authorization_code(
                created.ref.provider_connection_id,
                api_token,
                phone_int,
            )
        except Exception:
            _logger.exception("onboarding: failed to get OTP for %s", phone)
            await self._user_repo.update_onboarding_status(user_id, "failed")
            await self._bot.send_text(
                phone,
                "מצטער, לא הצלחתי לקבל את קוד האימות. נסה שוב מאוחר יותר.",
            )
            return

        # Send OTP + instructions.
        message = _OTP_INSTRUCTIONS.format(code=code)
        await self._bot.send_text(phone, message)
        _logger.info("onboarding: OTP sent to %s", phone)

    async def _wait_for_instance_ready(
        self,
        id_instance: str,
        api_token: str,
    ) -> bool:
        """Poll getStateInstance until it returns a non-null state.

        Green API creates the instance asynchronously — ``createInstance``
        returns immediately, but the instance isn't ready for pairing
        until ``getStateInstance`` returns a non-null ``stateInstance``.
        We poll every ``poll_interval`` seconds for up to
        ``poll_max_attempts`` attempts (default: 5s × 24 = 2 minutes).

        Returns ``True`` if the instance is ready, ``False`` on timeout.
        """
        import asyncio

        for attempt in range(self._poll_max_attempts):
            try:
                state = await self._green_client.get_state_instance(
                    id_instance,
                    api_token,
                )
                # ``None`` means the instance is still being created.
                # Any non-null state (``notAuthorized``, ``authorized``) means
                # the instance is ready.
                if state is not None:
                    _logger.info(
                        "onboarding: instance %s ready (state=%s, attempt=%d)",
                        id_instance,
                        state,
                        attempt + 1,
                    )
                    return True
            except Exception:
                _logger.warning(
                    "onboarding: getStateInstance failed (attempt=%d)",
                    attempt + 1,
                    exc_info=True,
                )
            await asyncio.sleep(self._poll_interval)

        return False

    async def handle_resend_request(self, phone: str) -> None:
        """Handle a user sending 'קוד' to re-request the OTP."""
        normalized = self._normalize_phone(phone)
        if normalized is None:
            return

        existing = await self._user_repo.get_by_phone(normalized)
        if existing is None:
            return
        user_id, onboarding_status, _name = existing
        if onboarding_status != "pending":
            return

        await self._resend_otp(user_id, normalized)

    async def handle_connection_established(
        self,
        user_id: str,
        phone: str,
    ) -> None:
        """Called when Green API confirms the connection (stateInstanceChanged).

        Updates onboarding status and sends the welcome message.
        """
        await self._user_repo.update_onboarding_status(user_id, "connected")
        await self._bot.send_text(phone, _WELCOME_MESSAGE.format(name=_DEFAULT_NAME))
        _logger.info("onboarding: connection established for user %s", user_id)

    async def handle_connection_established_by_id(
        self,
        user_id: str,
    ) -> None:
        """Called when Green API confirms the connection, resolving phone by user_id.

        Looks up the user's phone from the user repo, then delegates to
        :meth:`handle_connection_established`.
        """
        phone = await self._lookup_phone(user_id)
        if phone is None:
            _logger.warning("onboarding: no phone found for user %s", user_id)
            return
        await self.handle_connection_established(user_id, phone)

    async def _lookup_phone(self, user_id: str) -> str | None:
        """Look up a user's phone by user_id via the user repo."""
        if hasattr(self._user_repo, "get_phone_by_id"):
            return await self._user_repo.get_phone_by_id(user_id)
        return None

    async def handle_name_response(self, phone: str, name: str) -> bool:
        """Handle a text message from a user who just connected.

        If the user's onboarding_status is ``connected``, treat the message
        as their name, store it, and set onboarding to ``active``.

        Returns ``True`` if this was handled as a name response.
        """
        normalized = self._normalize_phone(phone)
        if normalized is None:
            return False

        existing = await self._user_repo.get_by_phone(normalized)
        if existing is None:
            return False
        user_id, onboarding_status, _existing_name = existing
        if onboarding_status != "connected":
            return False

        clean_name = name.strip()[:50]  # sanitize
        if not clean_name:
            return False

        await self._user_repo.update_first_name(user_id, clean_name)
        await self._bot.send_text(
            normalized,
            f"נחמד להכיר אותך, {clean_name}! 🎉\n\n"
            "אני אתחיל לעקוב אחרי השיחות שלך עכשיו. "
            "כל בוקר תקבל סיכום של מה שמחכה לך.",
        )
        _logger.info("onboarding: name set to %s for user %s", clean_name, user_id)
        return True

    async def is_onboarding(self, phone: str) -> bool:
        """Check if the user is in the onboarding flow (pending or connected)."""
        normalized = self._normalize_phone(phone)
        if normalized is None:
            return False
        existing = await self._user_repo.get_by_phone(normalized)
        if existing is None:
            return False
        _user_id, onboarding_status, _name = existing
        return onboarding_status in ("pending", "connected")

    async def _resend_otp(self, user_id: str, phone: str) -> None:
        """Re-request the OTP for an existing pending onboarding."""
        conn = await self._connection_repo.get_by_user(user_id)
        if conn is None:
            _logger.warning("onboarding: no connection for user %s", user_id)
            return

        api_token = conn.credentials.data.decode("utf-8")
        phone_int = int(phone.lstrip("+"))
        try:
            code = await self._green_client.get_authorization_code(
                conn.ref.provider_connection_id,
                api_token,
                phone_int,
            )
        except Exception:
            _logger.exception("onboarding: failed to re-send OTP for %s", phone)
            await self._bot.send_text(
                phone,
                "לא הצלחתי לקבל קוד חדש. ודא שהמכשיר אינו מחובר כבר.",
            )
            return

        message = _OTP_INSTRUCTIONS.format(code=code)
        await self._bot.send_text(phone, message)

    @staticmethod
    def _normalize_phone(phone: str) -> str | None:
        """Normalize to E.164. Returns None if invalid."""
        try:
            return normalize_phone_e164(phone)
        except PhoneParseError:
            return None
