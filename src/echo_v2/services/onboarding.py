"""OnboardingService — WhatsApp onboarding flow (QR-first, OTP fallback).

Click-driven flow (name collected before pairing; QR only on user click):

1. Unknown user messages the Echo bot → intro + consent buttons.
2. User consents ("חברו אותי") → create user row (pending), ask for name.
3. User sends name → store name, send _NAME_CONFIRMATION with [חבר אותי] button.
   (NO auto-provisioning — user must explicitly click to start pairing.)
4. User clicks "חבר אותי" (onboarding:connect) → start_pairing():
   - Pool hit (available instance): claim → save PAIRING_REQUIRED → finalize
     → refill → fetch + send QR immediately → poll for authorization.
   - Pool miss (or no pool): send "preparing..." → background task creates
     instance, saves PROVISIONING, waits until notAuthorized, updates to
     PAIRING_REQUIRED, sends "ready" + [הצג QR] button.
5. User clicks "הצג QR" (onboarding:show_qr) → fetch + send QR → poll.
6. Poll reaches ``authorized`` → user becomes ``active``, send welcome.
7. Timeout/failure → user becomes ``failed``.

**Invariant**: A QR is never fetched/sent except as the direct consequence
of a recent user click. This holds on re-entry too: ``start_onboarding``
re-sends the appropriate button (not a QR) for pending users.

Onboarding states: ``pending`` → ``active`` (or ``failed``).

Idempotency:
- If onboarding is already ``pending``, don't create a second instance.
  Re-send the appropriate button ([חבר אותי] or [הצג QR]) or "still preparing".
- If onboarding is ``active``, skip.

Commands:
- 'qr' → re-send the QR image (default pairing method).
- 'קוד' → re-send the OTP code (fallback pairing method).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from echo_v2.integrations.green.provisioner import GreenProvisioner
from echo_v2.persistence.identity import PhoneParseError, normalize_phone_e164
from echo_v2.persistence.whatsapp_connections import (
    StoredConnection,
    WhatsAppConnectionRepository,
)
from echo_v2.ports.bot import BotChannel, BotEvent, BotEventType
from echo_v2.ports.whatsapp import (
    ConnectionConfig,
    ConnectionRef,
    ConnectionStatus,
    PairingOutcome,
    ProviderCredentials,
    WhatsAppEventSubscription,
)

if TYPE_CHECKING:
    from echo_v2.integrations.green.client import GreenClient
    from echo_v2.services.green_instance_pool import GreenInstancePool

__all__ = ["OnboardingContext", "OnboardingService", "UserRepository"]

_logger = logging.getLogger("echo_v2.services.onboarding")

_DEFAULT_TIMEZONE = "Asia/Jerusalem"
_DEFAULT_NAME = "חבר"

# Name prompt sent after consent (before provisioning).
_NAME_PROMPT = "איך אפנה אליך? (שלח את השם שלך)"

# Name confirmation sent after name is stored, before user clicks to pair.
# Contains a [חבר אותי] button — NO auto-provisioning.
_NAME_CONFIRMATION = (
    "נחמד להכיר אותך, {name}! 🎉\n\n"
    "החיבור ל־Echo כמעט מוכן.\n"
    "כשתהיה מוכן לפתוח את WhatsApp ולסרוק QR, לחץ על הכפתור למטה."
)

# Sent when pool miss — instance is being created in the background.
_CREATING_INSTANCE = "מכין חיבור חדש בשבילך, רגע שנייה... ⏳"

# Sent when user re-clicks "חבר אותי" while instance is still being prepared.
_STILL_PREPARING = "עדיין מכין את החיבור... רגע ⏳"

# Sent after background creation completes — user clicks to get QR.
_READY_FOR_QR = "החיבור מוכן! 🎉\nלחץ כדי לקבל את קוד ה-QR."

# Button sent with _NAME_CONFIRMATION (start pairing).
_CONNECT_BUTTON = {"id": "onboarding:connect", "title": "חבר אותי"}

# Button sent with _READY_FOR_QR (fetch QR after background creation).
_SHOW_QR_BUTTON = {"id": "onboarding:show_qr", "title": "הצג QR"}

# Onboarding instructions sent to the user (OTP fallback path).
_OTP_INSTRUCTIONS = (
    "הקוד שלך: {code}\n\n"
    "כדי לחבר את WhatsApp:\n"
    "1. פתח את WhatsApp בטלפון\n"
    "2. הגדרות ← מכשירים מקושרים ← קשר מכשיר\n"
    "3. בחר 'קשר עם מספר טלפון'\n"
    "4. הזן את הקוד: {code}\n\n"
    "הקוד בתוקף למשך כ-2.5 דקות."
)

# Caption sent with the QR image (default pairing path).
_QR_CAPTION = (
    "סרוק את ה-QR כדי לחבר את WhatsApp:\n"
    "1. פתח את WhatsApp בטלפון\n"
    "2. הגדרות ← מכשירים מקושרים ← קשר מכשיר\n"
    "3. סרוק את ה-QR הזה\n\n"
    "לא מצליח לסרוק? שלח 'קוד' כדי לקבל קוד טלפון."
)

# Sent when QR fetch fails and we fall back to OTP.
_QR_FAILED_FALLBACK = (
    "לא הצלחתי לשלוח את ה-QR. שולח קוד טלפון במקום — הזן אותו ב-WhatsApp."
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
    """Orchestrates the WhatsApp onboarding flow (QR-first, OTP fallback).

    Dependencies:
    * ``bot`` — sends messages (text + image) to the user.
    * ``user_repo`` — creates and updates users.
    * ``connection_repo`` — stores the Green API connection.
    * ``provisioner`` — creates Green instances, configures webhooks, and
      fetches pairing QRs (``get_pairing_qr``).
    * ``green_client`` — calls ``getAuthorizationCode`` for the OTP fallback
      and ``getStateInstance`` for the ready poll.
    * ``webhook_base_url`` — the public URL Green should send webhooks to.
    * ``pool`` — optional :class:`GreenInstancePool` for fast onboarding.
      When ``None`` (or pool size 0), onboarding creates instances on demand.
    """

    def __init__(
        self,
        *,
        bot: BotChannel,
        user_repo: UserRepository,
        connection_repo: WhatsAppConnectionRepository,
        provisioner: GreenProvisioner,
        green_client: GreenClient,  # GreenClient — avoid circular import
        webhook_base_url: str,
        poll_interval: float = 5.0,
        poll_max_attempts: int = 60,
        pool: GreenInstancePool | None = None,
    ) -> None:
        self._bot = bot
        self._user_repo = user_repo
        self._connection_repo = connection_repo
        self._provisioner = provisioner
        self._green_client = green_client
        self._webhook_base_url = webhook_base_url.rstrip("/")
        self._poll_interval = poll_interval
        self._poll_max_attempts = poll_max_attempts
        self._pool = pool
        # Background instance-preparation tasks (pool miss path), keyed by
        # user_id. Prevents double-click from creating two instances.
        self._prepare_tasks: dict[str, asyncio.Task[None]] = {}
        # Authorization poll tasks, keyed by user_id. Ensures at most one poll
        # per user even on repeated "הצג QR" clicks.
        self._poll_tasks: dict[str, asyncio.Task[None]] = {}

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
        Pairing starts only after the user clicks [חבר אותי] (see
        :meth:`start_pairing`).

        Idempotent: if onboarding is already pending, re-send the
        appropriate button (NOT a QR — the QR invariant must hold on
        re-entry):
        - No name yet → re-ask for name.
        - Name set, no connection → re-send _NAME_CONFIRMATION + [חבר אותי].
        - Connection PROVISIONING → "still preparing".
        - Connection PAIRING_REQUIRED → [הצג QR].
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
                    # Name set — re-send the appropriate button, NOT a QR.
                    conn = await self._connection_repo.get_by_user(user_id)
                    if conn is None:
                        await self._bot.send_buttons(
                            normalized,
                            body_text=_NAME_CONFIRMATION.format(name=name),
                            buttons=[_CONNECT_BUTTON],
                        )
                    elif conn.status == ConnectionStatus.PROVISIONING:
                        await self._bot.send_text(normalized, _STILL_PREPARING)
                    elif conn.status == ConnectionStatus.PAIRING_REQUIRED:
                        await self._bot.send_buttons(
                            normalized,
                            body_text=_READY_FOR_QR,
                            buttons=[_SHOW_QR_BUTTON],
                        )
                    else:
                        # CONNECTED or other — re-send QR (user is known).
                        await self._resend_qr(user_id, normalized)
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
            # Already has a name — pairing in progress. Don't eat text.
            return False

        clean_name = name.strip()[:50]
        if not clean_name:
            return False

        # Store the name (still pending — not active until authorized).
        await self._user_repo.update_first_name(ctx.user_id, clean_name)

        # Send name confirmation + [חבר אותי] button.
        # NO auto-provisioning — user must explicitly click to start pairing.
        await self._bot.send_buttons(
            ctx.phone,
            body_text=_NAME_CONFIRMATION.format(name=clean_name),
            buttons=[_CONNECT_BUTTON],
        )

        _logger.info("onboarding: name set for user %s, awaiting connect click", ctx.user_id)
        return True

    # --- Pairing entry: user clicks [חבר אותי] ------------------------------

    async def start_pairing(self, user_id: str, phone: str) -> None:
        """Start pairing after the user clicks [חבר אותי].

        Pool hit: claim → save PAIRING_REQUIRED → finalize → refill →
        fetch + send QR immediately → poll for authorization.

        Pool miss: send "preparing..." → background task creates instance,
        saves PROVISIONING, waits until notAuthorized, updates to
        PAIRING_REQUIRED, sends "ready" + [הצג QR] button. The webhook
        returns immediately (non-blocking).

        If a connection already exists (re-click), re-send QR or "still
        preparing" depending on connection status.
        """
        existing = await self._connection_repo.get_by_user(user_id)
        if existing is not None:
            # Connection already exists — re-send QR or "still preparing".
            if existing.status == ConnectionStatus.PROVISIONING:
                await self._bot.send_text(phone, _STILL_PREPARING)
            else:
                await self._send_qr_and_poll(user_id, phone, existing)
            return

        pooled = await self._pool.claim(user_id) if self._pool else None

        if pooled is not None:
            # Pool hit — instance ready (notAuthorized), send QR immediately.
            await self._save_connection(
                user_id,
                pooled.ref,
                pooled.credentials,
                pooled.webhook_token_hash,
                ConnectionStatus.PAIRING_REQUIRED,
            )
            if self._pool is not None:
                await self._pool.finalize_claim(pooled.pool_row_id)
                self._pool.request_refill()
            conn = await self._connection_repo.get_by_user(user_id)
            if conn is not None:
                await self._send_qr_and_poll(user_id, phone, conn)
        else:
            # Pool miss — async preparation, don't block the webhook.
            existing_task = self._prepare_tasks.get(user_id)
            if existing_task is not None and not existing_task.done():
                await self._bot.send_text(phone, _STILL_PREPARING)
                return
            await self._bot.send_text(phone, _CREATING_INSTANCE)
            self._prepare_tasks[user_id] = asyncio.create_task(
                self._prepare_instance(user_id, phone)
            )

    async def _prepare_instance(self, user_id: str, phone: str) -> None:
        """Background task: create a fresh instance for a pool-miss user.

        Steps:
        1. createInstance via provisioner.
        2. Save connection as PROVISIONING immediately.
        3. Wait until notAuthorized (ready poll).
        4. Update to PAIRING_REQUIRED.
        5. Send "ready" + [הצג QR] button.

        On failure: mark user ``failed`` + send failure message.
        """
        try:
            webhook_token = secrets.token_urlsafe(32)
            webhook_url = f"{self._webhook_base_url}/webhooks/whatsapp/green"
            config = ConnectionConfig(
                webhook_url=webhook_url,
                webhook_token=webhook_token,
                subscriptions=WhatsAppEventSubscription(),
            )
            created = await self._provisioner.create_connection(config)
            token_hash = hashlib.sha256(webhook_token.encode("utf-8")).digest()

            # Save immediately as PROVISIONING (reduces double-create window).
            await self._save_connection(
                user_id,
                created.ref,
                created.credentials,
                token_hash,
                ConnectionStatus.PROVISIONING,
            )
            if self._pool:
                self._pool.request_refill()

            # Wait until notAuthorized (ready for pairing).
            api_token = created.credentials.data.decode("utf-8")
            ready = await self._wait_until_ready(
                created.ref.provider_connection_id, api_token,
            )
            if not ready:
                await self._user_repo.update_onboarding_status(user_id, "failed")
                await self._bot.send_text(
                    phone,
                    "מצטער, לא הצלחתי ליצור את החיבור. נסה שוב מאוחר יותר.",
                )
                return

            # Update to PAIRING_REQUIRED and ask user to click for QR.
            await self._connection_repo.update_status(
                created.ref,
                ConnectionStatus.PAIRING_REQUIRED,
                "notAuthorized",
            )
            await self._bot.send_buttons(
                phone, body_text=_READY_FOR_QR, buttons=[_SHOW_QR_BUTTON],
            )
            _logger.info(
                "onboarding: instance ready for user %s, awaiting show_qr click",
                user_id,
            )
        except Exception:
            _logger.exception("onboarding: _prepare_instance failed for %s", user_id)
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
        finally:
            self._prepare_tasks.pop(user_id, None)

    async def show_pairing_qr(self, user_id: str, phone: str) -> None:
        """Handle [הצג QR] click — fetch + send QR for an existing connection.

        If no connection exists (shouldn't happen), falls back to
        :meth:`start_pairing`. If the connection is still PROVISIONING,
        sends "still preparing".
        """
        conn = await self._connection_repo.get_by_user(user_id)
        if conn is None:
            await self.start_pairing(user_id, phone)
            return
        if conn.status == ConnectionStatus.PROVISIONING:
            await self._bot.send_text(phone, _STILL_PREPARING)
            return
        await self._send_qr_and_poll(user_id, phone, conn)

    async def _send_qr_and_poll(
        self,
        user_id: str,
        phone: str,
        conn: StoredConnection,
    ) -> None:
        """Fetch + send QR, then ensure one auth-poll task is running.

        The QR was sent by this method (as a direct consequence of a user
        click), so the poll is started with ``pairing_already_sent=True``
        to avoid sending a duplicate QR when it sees ``notAuthorized``.
        """
        api_token = conn.credentials.data.decode("utf-8")
        sent = await self._send_pairing_qr(
            user_id, phone, conn.ref, api_token,
        )
        if not sent:
            return  # QR failed and OTP fallback also failed.
        self._ensure_poll_task(
            user_id, phone, conn.ref.provider_connection_id, api_token,
            pairing_already_sent=True,
        )

    def _ensure_poll_task(
        self,
        user_id: str,
        phone: str,
        id_instance: str,
        api_token: str,
        *,
        pairing_already_sent: bool = False,
    ) -> None:
        """Ensure at most one auth-poll task per user.

        If a poll is already running for this user, don't start a second.
        The completion handler (:meth:`handle_connection_established`) is
        idempotent, but avoiding duplicate polls is cleaner.
        """
        existing = self._poll_tasks.get(user_id)
        if existing is not None and not existing.done():
            return
        self._poll_tasks[user_id] = asyncio.create_task(
            self._poll_until_authorized(
                user_id=user_id,
                phone=phone,
                id_instance=id_instance,
                api_token=api_token,
                pairing_already_sent=pairing_already_sent,
            )
        )

    async def _wait_until_ready(
        self, id_instance: str, api_token: str,
    ) -> bool:
        """Poll getStateInstance until ``notAuthorized``.

        Returns ``True`` on ``notAuthorized``. Returns ``False`` on timeout
        or ``authorized`` (an authorized instance is not a fresh slot).

        A 401 immediately after ``createInstance`` is a Green propagation
        delay (the instance exists but Green's auth hasn't propagated yet),
        NOT a permanent auth error. So we keep polling through all exceptions
        until the timeout.
        """
        for _ in range(self._poll_max_attempts):
            try:
                state = await self._green_client.get_state_instance(
                    id_instance, api_token,
                )
                if state == "notAuthorized":
                    return True
                if state == "authorized":
                    _logger.warning(
                        "onboarding: instance %s authorized during warmup "
                        "(not a fresh slot)", id_instance,
                    )
                    return False
            except Exception:
                _logger.debug(
                    "onboarding: transient getStateInstance error for %s, keep polling",
                    id_instance,
                    exc_info=True,
                )
            await asyncio.sleep(self._poll_interval)
        return False

    async def _save_connection(
        self,
        user_id: str,
        ref: ConnectionRef,
        credentials: ProviderCredentials,
        webhook_token_hash: bytes,
        status: ConnectionStatus,
    ) -> None:
        """Persist a connection with the given status."""
        conn = StoredConnection(
            user_id=user_id,
            ref=ref,
            credentials=credentials,
            webhook_token_hash=webhook_token_hash,
            status=status,
        )
        await self._connection_repo.save(conn)

    async def _poll_until_authorized(
        self,
        user_id: str,
        phone: str,
        id_instance: str,
        api_token: str,
        *,
        pairing_already_sent: bool = False,
    ) -> None:
        """Poll getStateInstance through the instance lifecycle in one loop.

        States:
        - ``None`` / ``starting`` → instance still being created, keep polling
        - ``notAuthorized`` → ready for pairing, send QR once, keep polling
        - ``authorized`` → user paired, complete onboarding, stop
        - timeout → depends on whether the instance ever became ready:
          - if we never saw ``notAuthorized``/``authorized`` (instance stuck
            in ``starting``/``None``) → user becomes ``failed`` (provisioning
            itself failed)
          - if we saw ``notAuthorized`` (QR/OTP was sent) but never reached
            ``authorized`` → user remains ``pending`` (user just needs to
            scan/enter the code; can retry via 'qr' or 'קוד')

        The QR is sent exactly once, the first time we see ``notAuthorized``.
        If the QR fetch fails, we fall back to OTP.

        When ``pairing_already_sent=True`` (called from ``_send_qr_and_poll``
        after an explicit user click), the QR/OTP was already sent before the
        poll started — don't send another one.
        """
        pairing_sent = pairing_already_sent
        saw_ready = pairing_already_sent  # already ready if we sent QR

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
                    if not pairing_sent:
                        # Instance is ready for pairing — send QR (default).
                        conn_ref = ConnectionRef(
                            provider="green",
                            provider_connection_id=id_instance,
                        )
                        sent = await self._send_pairing_qr(
                            user_id, phone, conn_ref, api_token,
                        )
                        pairing_sent = True
                        if not sent:
                            # QR failed and OTP fallback also failed.
                            return
                        _logger.info(
                            "onboarding: pairing sent (attempt=%d)", attempt + 1,
                        )

            except Exception as exc:  # noqa: BLE001
                # Transient (network, 429, 5xx, 401 propagation delay) — keep polling.
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
            # Instance became ready (QR/OTP was sent) but user didn't authorize.
            # Keep them pending so they can retry via 'qr' or 'קוד'.
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

    async def handle_onboarding_qr(self, phone: str) -> bool:
        """Handle 'qr' command — resend QR image for a known user."""
        ctx = await self.resolve_context(phone)
        if ctx is None:
            return False
        return await self._resend_qr(ctx.user_id, ctx.phone)

    async def handle_onboarding_start(self, phone: str) -> None:
        """Handle 'חברו אותי' command — start onboarding."""
        await self.start_onboarding(phone)

    async def handle_onboarding_connect(self, phone: str) -> None:
        """Handle [חבר אותי] button — start pairing.

        Guards against stale/forged callbacks: if the user has no name yet,
        re-ask for the name instead of starting pairing.
        """
        ctx = await self.resolve_context(phone)
        if ctx is None:
            # Unknown user — start fresh onboarding (will ask for name).
            await self.start_onboarding(phone)
            return
        if ctx.onboarding_status == "active":
            return
        if not ctx.first_name:
            # Stale/forged callback — don't bypass the name step.
            await self._bot.send_text(ctx.phone, _NAME_PROMPT)
            return
        await self.start_pairing(ctx.user_id, ctx.phone)

    async def handle_onboarding_show_qr(self, phone: str) -> None:
        """Handle [הצג QR] button — fetch + send QR for an existing connection."""
        ctx = await self.resolve_context(phone)
        if ctx is None:
            return
        if ctx.onboarding_status == "active":
            return
        await self.show_pairing_qr(ctx.user_id, ctx.phone)

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

    async def _send_pairing_qr(
        self,
        user_id: str,
        phone: str,
        conn_ref: ConnectionRef,
        api_token: str,
    ) -> bool:
        """Fetch a pairing QR from Green and send it as an image.

        Falls back to OTP if the QR fetch fails, returns a non-QR outcome
        (e.g. ``PASSKEY_REQUIRED`` / ``TIMEOUT``), or the bot can't send
        the image. Returns ``True`` if a pairing artifact (QR or OTP) was
        sent, ``False`` if everything failed (onboarding is marked failed
        by the caller).
        """
        try:
            raw = await self._green_client.get_qr_ws(
                conn_ref.provider_connection_id,
                api_token,
            )
        except Exception:
            _logger.exception("onboarding: failed to fetch QR")
            return await self._fallback_to_otp(user_id, phone, conn_ref, api_token)

        outcome = _qr_outcome(raw)

        if outcome is PairingOutcome.QR_READY:
            image_b64 = raw.get("message") or ""
            try:
                image_bytes = base64.b64decode(image_b64)
            except Exception:
                _logger.exception("onboarding: failed to decode QR image")
                return await self._fallback_to_otp(user_id, phone, conn_ref, api_token)
            try:
                await self._bot.send_image(
                    phone,
                    image_bytes=image_bytes,
                    mime_type="image/png",
                    caption=_QR_CAPTION,
                )
            except Exception:
                _logger.exception("onboarding: failed to send QR image")
                return await self._fallback_to_otp(user_id, phone, conn_ref, api_token)
            _logger.info("onboarding: QR sent to %s", phone)
            return True

        if outcome is PairingOutcome.ALREADY_AUTHORIZED:
            _logger.info("onboarding: QR already authorized for %s", phone)
            await self.handle_connection_established(user_id, phone)
            return True

        # PASSKEY_REQUIRED / TIMEOUT / error → fall back to OTP.
        _logger.info(
            "onboarding: QR outcome %s, falling back to OTP for %s",
            outcome, phone,
        )
        return await self._fallback_to_otp(user_id, phone, conn_ref, api_token)

    async def _fallback_to_otp(
        self,
        user_id: str,
        phone: str,
        conn_ref: ConnectionRef,
        api_token: str,
    ) -> bool:
        """Send an OTP as a fallback when QR is unavailable.

        Returns ``True`` if the OTP was sent, ``False`` if it failed (the
        caller marks the user ``failed``).
        """
        phone_int = int(phone.lstrip("+"))
        try:
            code = await self._green_client.get_authorization_code(
                conn_ref.provider_connection_id,
                api_token,
                phone_int,
            )
        except Exception:
            _logger.exception("onboarding: OTP fallback failed for %s", phone)
            await self._user_repo.update_onboarding_status(user_id, "failed")
            await self._bot.send_text(
                phone,
                "מצטער, לא הצלחתי לקבל את קוד האימות. נסה שוב מאוחר יותר.",
            )
            return False
        try:
            await self._bot.send_text(phone, _QR_FAILED_FALLBACK)
        except Exception:
            _logger.exception("onboarding: failed to send QR-failed notice")
        message = _OTP_INSTRUCTIONS.format(code=code)
        await self._bot.send_text(phone, message)
        _logger.info("onboarding: OTP fallback sent to %s", phone)
        return True

    async def _resend_qr(self, user_id: str, phone: str) -> bool:
        """Re-request the QR for an existing connection.

        Checks Green API state first: if the instance is already
        ``authorized``, tells the user they're already connected and
        updates the DB to ``active`` (if stale). Otherwise fetches a
        fresh QR and sends it. Falls back to OTP if the QR fetch fails.

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
            state = None  # proceed to attempt QR anyway

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
                "אתה כבר מחובר 👍 אין צורך ב-QR חדש.",
            )
            return True

        return await self._send_pairing_qr(user_id, phone, conn.ref, api_token)

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


def _qr_outcome(raw: dict[str, object]) -> PairingOutcome:
    """Map a raw Green QR WebSocket event to a PairingOutcome."""
    etype = raw.get("type")
    if etype == "qrCode":
        return PairingOutcome.QR_READY
    if etype == "alreadyLogged":
        return PairingOutcome.ALREADY_AUTHORIZED
    if etype == "passkeyRequired":
        return PairingOutcome.PASSKEY_REQUIRED
    return PairingOutcome.TIMEOUT
