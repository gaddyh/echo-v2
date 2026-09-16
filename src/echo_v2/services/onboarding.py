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
from echo_v2.ports.bot import BotChannel, BotEvent, BotEventType
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

# Sent when the 5-minute authorization poll times out (user didn't enter OTP).
_OTP_TIMED_OUT = 'לא התחברת בזמן. שלח "קוד" כדי לקבל קוד חדש.'

_DISCONNECTED = (
    "החיבור של Echo ל־WhatsApp התנתק.\n"
    "שלח קוד כדי להתחבר מחדש."
)

_RESEND_KEYWORD = "קוד"

# --- Consent-first introduction ---------------------------------------------
# First contact from an unknown user shows this intro with two buttons.
# No DB row or Green instance is created until the user taps "חברו אותי".
_INTRO_BODY = (
    "היי, אני Echo 👋\n\n"
    "אני עוזר לך לזהות שיחות ב־WhatsApp שמחכות לתשובה, "
    "ושולח לך סיכום יומי.\n\n"
    "כדי לעשות זאת, נחבר את חשבון ה־WhatsApp שלך כמכשיר מקושר. "
    "החיבור מאפשר ל־Echo לקרוא את השיחות כדי לזהות מה ממתין לך; "
    "Echo שולח הודעות רק כשאתה מבקש ממנו.\n\n"
    "רוצה להתחבר?"
)

# Short reassurance shown when the user taps "איך זה עובד?".
_INFO_BODY = (
    "Echo מתחבר ל־WhatsApp שלך כמו מכשיר מקושר נוסף — בלי להחליף אותך.\n\n"
    "הוא קורא את השיחות כדי לזהות מה מחכה לתגובה, ושולח לך סיכום יומי. "
    "Echo שולח הודעות משמך רק כשאתה מבקש ממנו תזכורת.\n\n"
    "החשבון שלך נשאר שלך לגמרי — אפשר לנתק את Echo בכל רגע מהגדרות WhatsApp."
)

_ONBOARDING_START_BUTTON = {"id": "onboarding:start", "title": "חברו אותי"}
_ONBOARDING_INFO_BUTTON = {"id": "onboarding:info", "title": "איך זה עובד?"}

# Text fallback for consent if button delivery is unavailable.
_CONSENT_PHRASE = "חברו אותי"


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
        poll_max_attempts: int = 60,
    ) -> None:
        self._bot = bot
        self._user_repo = user_repo
        self._connection_repo = connection_repo
        self._provisioner = provisioner
        self._green_client = green_client
        self._webhook_base_url = webhook_base_url.rstrip("/")
        self._poll_interval = poll_interval
        self._poll_max_attempts = poll_max_attempts

    async def handle_unknown_event(self, event: BotEvent) -> None:
        """Handle an event from a user with no DB row — consent-first.

        No DB row or Green instance is created until the user explicitly
        consents via the "חברו אותי" button (or the text fallback).

        - BUTTON_REPLY ``onboarding:start`` → start_onboarding
        - BUTTON_REPLY ``onboarding:info`` → send_explanation
        - TEXT matching the consent phrase → start_onboarding (fallback)
        - Anything else → send_introduction
        """
        phone = event.user_phone

        if event.type is BotEventType.BUTTON_REPLY:
            if event.button_id == "onboarding:start":
                await self.start_onboarding(phone)
                return
            if event.button_id == "onboarding:info":
                await self.send_explanation(phone)
                return
            # Unknown button → show intro.
            await self.send_introduction(phone)
            return

        # TEXT event — only the deliberate consent phrase triggers onboarding.
        if event.type is BotEventType.TEXT and event.text:
            if event.text.strip() == _CONSENT_PHRASE:
                await self.start_onboarding(phone)
                return
            await self.send_introduction(phone)
            return

        # Any other event type (CONTACT, LIST_REPLY) → show intro.
        await self.send_introduction(phone)

    async def send_introduction(self, phone: str) -> None:
        """Send the consent-first intro message with two buttons."""
        normalized = self._normalize_phone(phone)
        if normalized is None:
            _logger.warning("onboarding: invalid phone number format")
            return
        try:
            await self._bot.send_buttons(
                normalized,
                body_text=_INTRO_BODY,
                buttons=[_ONBOARDING_START_BUTTON, _ONBOARDING_INFO_BUTTON],
            )
        except Exception:
            _logger.exception("onboarding: failed to send introduction to %s", phone)

    async def send_explanation(self, phone: str) -> None:
        """Send the "איך זה עובד?" explanation with the connect button."""
        normalized = self._normalize_phone(phone)
        if normalized is None:
            _logger.warning("onboarding: invalid phone number format")
            return
        try:
            await self._bot.send_buttons(
                normalized,
                body_text=_INFO_BODY,
                buttons=[_ONBOARDING_START_BUTTON],
            )
        except Exception:
            _logger.exception("onboarding: failed to send explanation to %s", phone)

    async def start_onboarding(self, phone: str) -> None:
        """Start onboarding for a user who has explicitly consented.

        Idempotent: if onboarding is already pending, re-send OTP instructions
        instead of creating a new instance.

        The instance creation (which can take 1-2 minutes) runs in the
        background — we send an immediate "please wait" message, then
        fire off the provisioning + OTP as a background task.
        """
        normalized = self._normalize_phone(phone)
        if normalized is None:
            _logger.warning("onboarding: invalid phone number format")
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
            _logger.exception("onboarding: failed to create Green instance")
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
        _logger.info(
            "onboarding: instance created id=%s token_len=%d",
            created.ref.provider_connection_id,
            len(api_token),
        )
        ready = await self._wait_for_instance_ready(
            created.ref.provider_connection_id,
            api_token,
        )
        if not ready:
            _logger.error("onboarding: instance not ready")
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
            _logger.exception("onboarding: failed to get OTP")
            await self._user_repo.update_onboarding_status(user_id, "failed")
            await self._bot.send_text(
                phone,
                "מצטער, לא הצלחתי לקבל את קוד האימות. נסה שוב מאוחר יותר.",
            )
            return

        # Send OTP + instructions.
        message = _OTP_INSTRUCTIONS.format(code=code)
        await self._bot.send_text(phone, message)
        _logger.info("onboarding: OTP sent")

        # Poll for authorization — Green API may not fire stateInstanceChanged
        # when the state changes to 'authorized' via OTP. Poll as a fallback.
        await self._poll_for_authorization(
            user_id,
            phone,
            created.ref.provider_connection_id,
            api_token,
        )

    async def _poll_for_authorization(
        self,
        user_id: str,
        phone: str,
        id_instance: str,
        api_token: str,
    ) -> None:
        """Poll getStateInstance until 'authorized', then complete onboarding.

        Green API's stateInstanceChanged webhook fires during instance
        creation (state=None) but may not fire again when the user enters
        the OTP and the state changes to 'authorized'. This poll is a
        reliable fallback that also works if the webhook is delayed.

        If the webhook does arrive first, :meth:`handle_connection_established`
        is idempotent (updates status + sends welcome).
        """
        import asyncio

        # Poll every 10s for up to 5 minutes (30 attempts).
        for attempt in range(30):
            await asyncio.sleep(10.0)
            try:
                state = await self._green_client.get_state_instance(
                    id_instance,
                    api_token,
                )
                if state == "authorized":
                    _logger.info(
                        "onboarding: instance %s authorized (attempt=%d)",
                        id_instance,
                        attempt + 1,
                    )
                    connection = await self._connection_repo.get_by_user(user_id)
                    if connection is None:
                        _logger.warning(
                            "onboarding: authorized instance has no connection row "
                            "for user %s",
                            user_id,
                        )
                    else:
                        await self._connection_repo.update_status(
                            connection.ref,
                            ConnectionStatus.CONNECTED,
                            "authorized",
                        )
                    await self.handle_connection_established(user_id, phone)
                    return
            except Exception:  # noqa: BLE001, S110
                # 401 can happen transiently — keep polling.
                pass

        _logger.warning(
            "onboarding: authorization poll timed out for %s (user=%s)",
            phone,
            user_id,
        )
        try:
            await self._bot.send_text(phone, _OTP_TIMED_OUT)
        except Exception:
            _logger.exception(
                "onboarding: failed to send timeout message to %s", phone
            )

    async def _wait_for_instance_ready(
        self,
        id_instance: str,
        api_token: str,
    ) -> bool:
        """Poll getStateInstance until it returns ``notAuthorized``.

        Green API creates the instance asynchronously. States:
        - ``None`` — instance still being created (getStateInstance returns null)
        - ``starting`` — instance is initializing, not ready for pairing
        - ``notAuthorized`` — ready for pairing (QR or OTP)
        - ``authorized`` — already paired

        We poll until ``notAuthorized`` (or ``authorized`` if re-pairing).
        401 errors are expected during creation — the API token isn't
        valid until the instance is fully created.

        Returns ``True`` if the instance is ready for pairing, ``False`` on timeout.
        """
        import asyncio

        for attempt in range(self._poll_max_attempts):
            try:
                state = await self._green_client.get_state_instance(
                    id_instance,
                    api_token,
                )
                if state in ("notAuthorized", "authorized"):
                    _logger.info(
                        "onboarding: instance %s ready (state=%s, attempt=%d)",
                        id_instance,
                        state,
                        attempt + 1,
                    )
                    return True
                _logger.info(
                    "onboarding: instance %s not ready (state=%s, attempt=%d)",
                    id_instance,
                    state,
                    attempt + 1,
                )
            except Exception as exc:  # noqa: BLE001
                # 401 is expected during creation — the token isn't valid yet.
                _logger.info(
                    "onboarding: getStateInstance failed (attempt=%d): %s",
                    attempt + 1,
                    exc,
                )
            await asyncio.sleep(self._poll_interval)

        return False

    async def handle_resend_request(self, phone: str) -> bool:
        """Handle a user sending 'קוד' to re-request the OTP.

        Works for any known user — Green API is the source of truth for
        whether an OTP is needed. If the instance is already authorized,
        the user is told they're already connected and the DB is updated.

        Returns ``True`` if handled (user known, connection found), ``False``
        if not (unknown user, no connection row).
        """
        normalized = self._normalize_phone(phone)
        if normalized is None:
            return False

        existing = await self._user_repo.get_by_phone(normalized)
        if existing is None:
            return False
        user_id, _onboarding_status, _name = existing

        return await self._resend_otp(user_id, normalized)

    async def handle_connection_established(
        self,
        user_id: str,
        phone: str,
    ) -> None:
        """Called when Green API confirms the connection (stateInstanceChanged).

        Updates onboarding status and sends the welcome message.
        Idempotent: if onboarding is already 'active' (user already sent
        their name via the poll race), skip the welcome message.
        """
        # Check current state — skip if already active.
        existing = await self._user_repo.get_by_phone(phone)
        if existing is not None:
            _uid, onboarding_status, _name = existing
            if onboarding_status in ("connected", "active"):
                _logger.info(
                    "onboarding: already %s for user %s, skipping welcome",
                    onboarding_status,
                    user_id,
                )
                return

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

    async def handle_disconnect_notification(self, user_id: str) -> None:
        """Notify a user that their WhatsApp connection disconnected.

        Called by the Green webhook dispatcher on a CONNECTED →
        PAIRING_REQUIRED transition. Only sends if the user is
        ``connected`` or ``active`` — pending users are still in
        onboarding (the poll handles their case), and failed users
        already know.
        """
        phone = await self._lookup_phone(user_id)
        if phone is None:
            _logger.warning(
                "onboarding: no phone for disconnect notification %s", user_id
            )
            return

        existing = await self._user_repo.get_by_phone(phone)
        if existing is None:
            return
        _uid, onboarding_status, _name = existing
        if onboarding_status not in ("connected", "active"):
            return

        try:
            await self._bot.send_text(phone, _DISCONNECTED)
        except Exception:
            _logger.exception(
                "onboarding: failed to send disconnect notification to %s", phone
            )

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
        _logger.info("onboarding: name set for user %s", user_id)
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

    async def _resend_otp(self, user_id: str, phone: str) -> bool:
        """Re-request the OTP for an existing connection.

        Checks Green API state first: if the instance is already
        ``authorized``, tells the user they're already connected and
        updates the DB to ``connected`` (if stale). Otherwise issues a
        fresh OTP.

        Returns ``True`` if handled, ``False`` if no connection row.
        """
        conn = await self._connection_repo.get_by_user(user_id)
        if conn is None:
            _logger.warning("onboarding: no connection for user %s", user_id)
            return False

        api_token = conn.credentials.data.decode("utf-8")

        # Check Green API state — it's the source of truth.
        try:
            state = await self._green_client.get_state_instance(
                conn.ref.provider_connection_id,
                api_token,
            )
        except Exception:
            _logger.exception("onboarding: failed to check instance state")
            state = None  # proceed to attempt OTP anyway

        if state == "authorized":
            _logger.info("onboarding: instance already authorized for %s", phone)
            # Update DB if stale.
            existing = await self._user_repo.get_by_phone(phone)
            if existing is not None:
                _uid, onboarding_status, _name = existing
                if onboarding_status not in ("connected", "active"):
                    await self._user_repo.update_onboarding_status(user_id, "connected")
                    _logger.info(
                        "onboarding: updated stale status %s → connected for %s",
                        onboarding_status, phone,
                    )
            await self._bot.send_text(
                phone,
                "אתה כבר מחובר 👍 אין צורך בקוד חדש.",
            )
            return True

        phone_int = int(phone.lstrip("+"))
        try:
            code = await self._green_client.get_authorization_code(
                conn.ref.provider_connection_id,
                api_token,
                phone_int,
            )
        except Exception:
            _logger.exception("onboarding: failed to re-send OTP")
            await self._bot.send_text(
                phone,
                "לא הצלחתי לקבל קוד חדש. ודא שהמכשיר אינו מחובר כבר.",
            )
            return True

        message = _OTP_INSTRUCTIONS.format(code=code)
        await self._bot.send_text(phone, message)
        return True

    @staticmethod
    def _normalize_phone(phone: str) -> str | None:
        """Normalize to E.164. Returns None if invalid."""
        try:
            return normalize_phone_e164(phone)
        except PhoneParseError:
            return None
