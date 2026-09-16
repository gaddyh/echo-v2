"""OnboardingService — OTP-based WhatsApp onboarding flow.

Simplified flow (name collected before provisioning):

1. Unknown user messages the Echo bot → intro + consent buttons.
2. User consents ("חברו אותי") → create user row (pending), ask for name.
3. User sends name (or skips) → store name, start provisioning.
4. Background: create Green instance, single lifecycle poll.
5. Poll reaches ``notAuthorized`` → send OTP + instructions (once).
6. User enters the 8-digit code in WhatsApp.
7. Poll reaches ``authorized`` → user becomes ``active``, send welcome.
8. Timeout/failure → user remains ``pending`` or becomes ``failed``.

Onboarding states: ``pending`` → ``active`` (or ``failed``).
The ``connected`` state is gone — connection status is owned by the
``WhatsAppConnection`` (PROVISIONING → PAIRING_REQUIRED → CONNECTED).

Idempotency:
- If onboarding is already ``pending``, don't create a second instance.
  Re-send the OTP if the user asks.
- If onboarding is ``active``, skip.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from dataclasses import dataclass
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

__all__ = ["OnboardingContext", "OnboardingService", "UserRepository"]

_logger = logging.getLogger("echo_v2.services.onboarding")

_DEFAULT_TIMEZONE = "Asia/Jerusalem"
_DEFAULT_NAME = "חבר"

# Name prompt sent after consent (before provisioning).
_NAME_PROMPT = "איך אפנה אליך? (שלח את השם שלך)"

# Name confirmation sent after name is stored, before provisioning starts.
_NAME_CONFIRMATION = (
    "נחמד להכיר אותך, {name}! 🎉\n\n"
    "מחבר אותך ל-Echo... זה יכול לקחת דקה-שתיים. רגע ותקבל את הקוד. ⏳"
)

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

# Welcome message sent after authorization (name already collected).
_WELCOME_MESSAGE = (
    "היי {name}! 👋\n\n"
    "אני Echo. מעכשיו אני עוקב אחרי השיחות שלך "
    "ומזכיר לך מה מחכה לתשובה.\n\n"
    "כל בוקר תקבל סיכום של השיחות שמחכות לך."
)

_ALREADY_ONBOARDING = (
    "אני כבר מכין את החיבור שלך. שלח 'קוד' כדי לקבל את הקוד מחדש."
)

# Sent when the authorization poll times out (user didn't enter OTP).
_OTP_TIMED_OUT = 'לא התחברת בזמן. שלח "קוד" כדי לקבל קוד חדש.'

_DISCONNECTED = (
    "החיבור של Echo ל־WhatsApp התנתק.\n"
    "שלח קוד כדי להתחבר מחדש."
)

_RESEND_KEYWORD = "קוד"

# --- Consent-first introduction ---------------------------------------------
_INTRO_BODY = (
    "היי, אני Echo 👋\n\n"
    "אני עוזר לך לזהות שיחות ב־WhatsApp שמחכות לתשובה, "
    "ושולח לך סיכום יומי.\n\n"
    "כדי לעשות זאת, נחבר את חשבון ה־WhatsApp שלך כמכשיר מקושר. "
    "החיבור מאפשר ל־Echo לקרוא את השיחות כדי לזהות מה ממתין לך; "
    "Echo שולח הודעות רק כשאתה מבקש ממנו.\n\n"
    "רוצה להתחבר?"
)

_INFO_BODY = (
    "Echo מתחבר ל־WhatsApp שלך כמו מכשיר מקושר נוסף — בלי להחליף אותך.\n\n"
    "הוא קורא את השיחות כדי לזהות מה מחכה לתגובה, ושולח לך סיכום יומי. "
    "Echo שולח הודעות משמך רק כשאתה מבקש ממנו תזכורת.\n\n"
    "החשבון שלך נשאר שלך לגמרי — אפשר לנתק את Echo בכל רגע מהגדרות WhatsApp."
)

_ONBOARDING_START_BUTTON = {"id": "onboarding:start", "title": "חברו אותי"}
_ONBOARDING_INFO_BUTTON = {"id": "onboarding:info", "title": "איך זה עובד?"}

_CONSENT_PHRASE = "חברו אותי"


# --- OnboardingContext (application/router context) ------------------------


@dataclass(frozen=True)
class OnboardingContext:
    """Resolved once at the onboarding service boundary, passed down.

    Application/router context — not a domain model. Encapsulates the
    user + connection state needed by onboarding handlers so each
    method doesn't do its own lookup.
    """

    user_id: str
    phone: str
    onboarding_status: str | None  # pending, active, failed
    first_name: str | None
    connection: StoredConnection | None


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
        """Update the user's first_name. Does NOT change onboarding_status."""
        ...

    async def get_phone_by_id(self, user_id: str) -> str | None:
        """Return the phone for a user_id, or None."""
        ...


class OnboardingService:
    """Orchestrates the OTP-based WhatsApp onboarding flow.

    Dependencies:
    * ``bot`` — sends messages to the user.
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

    # --- Context resolution (once per interaction) ---------------------------

    async def resolve_context(self, phone: str) -> OnboardingContext | None:
        """Resolve user + connection state in one lookup.

        Returns ``None`` if the phone is invalid or the user doesn't exist.
        """
        normalized = self._normalize_phone(phone)
        if normalized is None:
            return None
        existing = await self._user_repo.get_by_phone(normalized)
        if existing is None:
            return None
        user_id, onboarding_status, first_name = existing
        connection = await self._connection_repo.get_by_user(user_id)
        return OnboardingContext(
            user_id=user_id,
            phone=normalized,
            onboarding_status=onboarding_status,
            first_name=first_name,
            connection=connection,
        )

    # --- Unknown user entry (consent / intro) --------------------------------

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
            await self.send_introduction(phone)
            return

        if event.type is BotEventType.TEXT and event.text:
            if event.text.strip() == _CONSENT_PHRASE:
                await self.start_onboarding(phone)
                return
            await self.send_introduction(phone)
            return

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

    # --- Consent → create user + ask name -----------------------------------

    async def start_onboarding(self, phone: str) -> None:
        """Start onboarding for a user who has explicitly consented.

        Creates the user row (``pending``) and asks for their name.
        Provisioning starts after the name is received (or skipped).

        Idempotent: if onboarding is already pending, re-send instructions
        or ask for name (if name not yet provided).
        """
        normalized = self._normalize_phone(phone)
        if normalized is None:
            _logger.warning("onboarding: invalid phone number format")
            return

        existing = await self._user_repo.get_by_phone(normalized)
        if existing is not None:
            user_id, onboarding_status, name = existing
            if onboarding_status == "pending":
                if name is None:
                    # Consented but hasn't sent name yet — re-ask.
                    await self._bot.send_text(normalized, _NAME_PROMPT)
                else:
                    # Already provisioning — re-send OTP if connection exists.
                    await self._resend_otp(user_id, normalized)
                return
            if onboarding_status == "active":
                return
            # ``failed`` — let them try again by creating a new instance.

        # Create the user (pending, no name yet).
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

        # Ask for name.
        await self._bot.send_text(normalized, _NAME_PROMPT)

    # --- Name response → store name + start provisioning --------------------

    async def handle_name_response(self, phone: str, name: str) -> bool:
        """Handle a text message from a pending user who hasn't set a name.

        If the user is ``pending`` and has no name, treat the message as
        their name, store it, and start provisioning in the background.

        Returns ``True`` if this was handled as a name response.
        """
        ctx = await self.resolve_context(phone)
        if ctx is None:
            return False
        if ctx.onboarding_status != "pending":
            return False
        if ctx.first_name is not None:
            # Already has a name — provisioning in progress. Don't eat text.
            return False

        clean_name = name.strip()[:50]
        if not clean_name:
            return False

        # Store the name (still pending — not active until authorized).
        await self._user_repo.update_first_name(ctx.user_id, clean_name)

        # Send name confirmation + "please wait".
        await self._bot.send_text(
            ctx.phone,
            _NAME_CONFIRMATION.format(name=clean_name),
        )

        # Fire off provisioning + lifecycle poll in the background.
        asyncio.create_task(
            self._provision_and_poll(ctx.user_id, ctx.phone)
        )

        _logger.info("onboarding: name set for user %s, provisioning started", ctx.user_id)
        return True

    # --- Background: provision + single lifecycle poll ----------------------

    async def _provision_and_poll(
        self,
        user_id: str,
        phone: str,
    ) -> None:
        """Create Green instance + single lifecycle poll until authorized.

        Runs in the background. The poll tracks the instance through its
        lifecycle in one loop:

        - ``None`` / ``starting`` → keep polling (instance being created)
        - ``notAuthorized`` → send OTP once, keep polling
        - ``authorized`` → user becomes ``active``, send welcome, stop
        - timeout → user becomes ``failed``, send timeout message

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

        api_token = created.credentials.data.decode("utf-8")
        _logger.info(
            "onboarding: instance created id=%s token_len=%d",
            created.ref.provider_connection_id,
            len(api_token),
        )

        # Single lifecycle poll.
        await self._poll_until_authorized(
            user_id=user_id,
            phone=phone,
            id_instance=created.ref.provider_connection_id,
            api_token=api_token,
        )

    async def _poll_until_authorized(
        self,
        user_id: str,
        phone: str,
        id_instance: str,
        api_token: str,
    ) -> None:
        """Poll getStateInstance through the instance lifecycle in one loop.

        States:
        - ``None`` / ``starting`` → instance still being created, keep polling
        - ``notAuthorized`` → ready for pairing, send OTP once, keep polling
        - ``authorized`` → user paired, complete onboarding, stop
        - timeout → depends on whether the instance ever became ready:
          - if we never saw ``notAuthorized``/``authorized`` (instance stuck
            in ``starting``/``None``) → user becomes ``failed`` (provisioning
            itself failed)
          - if we saw ``notAuthorized`` (OTP was sent) but never reached
            ``authorized`` → user remains ``pending`` (user just needs to
            enter the code; can retry via 'קוד')

        The OTP is sent exactly once, the first time we see ``notAuthorized``.
        """
        otp_sent = False
        saw_ready = False  # saw notAuthorized or authorized at least once

        for attempt in range(self._poll_max_attempts):
            try:
                state = await self._green_client.get_state_instance(
                    id_instance,
                    api_token,
                )
                _logger.info(
                    "onboarding: poll attempt=%d state=%s instance=%s",
                    attempt + 1,
                    state,
                    id_instance,
                )

                if state == "authorized":
                    _logger.info(
                        "onboarding: instance %s authorized (attempt=%d)",
                        id_instance,
                        attempt + 1,
                    )
                    saw_ready = True
                    # Update connection status.
                    connection = await self._connection_repo.get_by_user(user_id)
                    if connection is not None:
                        await self._connection_repo.update_status(
                            connection.ref,
                            ConnectionStatus.CONNECTED,
                            "authorized",
                        )
                    # Complete onboarding.
                    await self.handle_connection_established(user_id, phone)
                    return

                if state == "notAuthorized":
                    saw_ready = True
                    if not otp_sent:
                        # Instance is ready for pairing — send OTP.
                        phone_int = int(phone.lstrip("+"))
                        try:
                            code = await self._green_client.get_authorization_code(
                                id_instance,
                                api_token,
                                phone_int,
                            )
                        except Exception:
                            _logger.exception("onboarding: failed to get OTP")
                            await self._user_repo.update_onboarding_status(
                                user_id, "failed"
                            )
                            await self._bot.send_text(
                                phone,
                                "מצטער, לא הצלחתי לקבל את קוד האימות. נסה שוב מאוחר יותר.",
                            )
                            return
                        message = _OTP_INSTRUCTIONS.format(code=code)
                        await self._bot.send_text(phone, message)
                        otp_sent = True
                        _logger.info("onboarding: OTP sent (attempt=%d)", attempt + 1)

            except Exception as exc:  # noqa: BLE001
                # 401 can happen transiently during creation — keep polling.
                _logger.info(
                    "onboarding: getStateInstance failed (attempt=%d): %s",
                    attempt + 1,
                    exc,
                )

            await asyncio.sleep(self._poll_interval)

        # Timeout.
        _logger.warning(
            "onboarding: authorization poll timed out for %s (user=%s, saw_ready=%s)",
            phone,
            user_id,
            saw_ready,
        )
        if saw_ready:
            # Instance became ready (OTP was sent) but user didn't authorize.
            # Keep them pending so they can retry via 'קוד'.
            try:
                await self._bot.send_text(phone, _OTP_TIMED_OUT)
            except Exception:
                _logger.exception(
                    "onboarding: failed to send timeout message to %s", phone
                )
        else:
            # Instance never became ready — provisioning failed.
            await self._user_repo.update_onboarding_status(user_id, "failed")
            try:
                await self._bot.send_text(
                    phone,
                    "מצטער, לא הצלחתי ליצור את החיבור. נסה שוב מאוחר יותר.",
                )
            except Exception:
                _logger.exception(
                    "onboarding: failed to send failure message to %s", phone
                )

    # --- OTP resend ("קוד" command) -----------------------------------------

    async def handle_resend_request(self, phone: str) -> bool:
        """Handle a user sending 'קוד' to re-request the OTP.

        Works for any known user — Green API is the source of truth for
        whether an OTP is needed. If the instance is already authorized,
        the user is told they're already connected and the DB is updated.

        Returns ``True`` if handled (user known, connection found), ``False``
        if not (unknown user, no connection row).
        """
        ctx = await self.resolve_context(phone)
        if ctx is None:
            return False
        return await self._resend_otp(ctx.user_id, ctx.phone)

    # --- Connection established (webhook or poll) ----------------------------

    async def handle_connection_established(
        self,
        user_id: str,
        phone: str,
    ) -> None:
        """Called when Green API confirms the connection (authorized).

        Updates onboarding status to ``active`` and sends the welcome message.
        Idempotent: if onboarding is already ``active``, skip.
        """
        existing = await self._user_repo.get_by_phone(phone)
        if existing is not None:
            _uid, onboarding_status, name = existing
            if onboarding_status == "active":
                _logger.info(
                    "onboarding: already active for user %s, skipping welcome",
                    user_id,
                )
                return

        await self._user_repo.update_onboarding_status(user_id, "active")

        # Use stored name, or default.
        display_name = name if name else _DEFAULT_NAME
        await self._bot.send_text(phone, _WELCOME_MESSAGE.format(name=display_name))
        _logger.info("onboarding: connection established for user %s", user_id)

    async def handle_connection_established_by_id(
        self,
        user_id: str,
    ) -> None:
        """Called when Green API confirms the connection, resolving phone by user_id."""
        phone = await self._lookup_phone(user_id)
        if phone is None:
            _logger.warning("onboarding: no phone found for user %s", user_id)
            return
        await self.handle_connection_established(user_id, phone)

    async def handle_disconnect_notification(self, user_id: str) -> None:
        """Notify a user that their WhatsApp connection disconnected.

        Called by the Green webhook dispatcher on a CONNECTED →
        PAIRING_REQUIRED transition. Only sends if the user is ``active``.
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
        if onboarding_status != "active":
            return

        try:
            await self._bot.send_text(phone, _DISCONNECTED)
        except Exception:
            _logger.exception(
                "onboarding: failed to send disconnect notification to %s", phone
            )

    # --- Onboarding check (for flow registry) -------------------------------

    async def is_onboarding(self, phone: str) -> bool:
        """Check if the user is in the onboarding flow (pending only).

        ``active`` users are fully onboarded. ``failed`` users are not
        in the flow (they can retry via 'קוד' if a connection exists).
        """
        normalized = self._normalize_phone(phone)
        if normalized is None:
            return False
        existing = await self._user_repo.get_by_phone(normalized)
        if existing is None:
            return False
        _user_id, onboarding_status, _name = existing
        return onboarding_status == "pending"

    # --- Public command methods (called by BotCommandRouter) ----------------

    async def handle_onboarding_code(self, phone: str) -> bool:
        """Handle 'קוד' command — resend OTP for a known user."""
        return await self.handle_resend_request(phone)

    async def handle_onboarding_start(self, phone: str) -> None:
        """Handle 'חברו אותי' command — start onboarding."""
        await self.start_onboarding(phone)

    async def handle_onboarding_info(self, phone: str) -> None:
        """Handle 'איך זה עובד?' command — send explanation."""
        await self.send_explanation(phone)

    # --- Internal helpers ---------------------------------------------------

    async def _resend_otp(self, user_id: str, phone: str) -> bool:
        """Re-request the OTP for an existing connection.

        Checks Green API state first: if the instance is already
        ``authorized``, tells the user they're already connected and
        updates the DB to ``active`` (if stale). Otherwise issues a
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
                if onboarding_status != "active":
                    await self._user_repo.update_onboarding_status(user_id, "active")
                    _logger.info(
                        "onboarding: updated stale status %s → active for %s",
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

    async def _lookup_phone(self, user_id: str) -> str | None:
        """Look up a user's phone by user_id via the user repo."""
        if hasattr(self._user_repo, "get_phone_by_id"):
            return await self._user_repo.get_phone_by_id(user_id)
        return None

    @staticmethod
    def _normalize_phone(phone: str) -> str | None:
        """Normalize to E.164. Returns None if invalid."""
        try:
            return normalize_phone_e164(phone)
        except PhoneParseError:
            return None
