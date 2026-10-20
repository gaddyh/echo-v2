"""OnboardingService — WhatsApp onboarding flow (QR-first, OTP fallback).

Click-driven flow (name collected before pairing; QR only on user click):

1. Unknown user messages the Echo bot → intro + consent buttons.
2. User consents ("חברו אותי") → create user row (pending), ask for name.
3. User sends name → store name. If the pool has an available instance,
   claim it and send the QR immediately (skip the connect button). If the
   pool is empty (or no pool), send _NAME_CONFIRMATION with [חבר אותי]
   button — pairing starts only after the user explicitly clicks.
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
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree

from echo_v2.domain.guard import GuardianChildLink
from echo_v2.domain.pipeline_event import PipelineEvent
from echo_v2.observability.privacy import correlation_id
from echo_v2.persistence.guard_repositories import GuardianChildLinkRepository
from echo_v2.persistence.identity import PhoneParseError, normalize_phone_e164
from echo_v2.persistence.pipeline_events import PipelineEventRepository
from echo_v2.persistence.whatsapp_connections import (
    StoredConnection,
    WhatsAppConnectionRepository,
)
from echo_v2.ports.bot import BotChannel, BotContact, BotEvent, BotEventType
from echo_v2.ports.whatsapp import (
    ConnectionConfig,
    ConnectionRef,
    ConnectionStatus,
    PairingOutcome,
    ProviderCredentials,
    WhatsAppEventSubscription,
    WhatsAppProvisioner,
)

if TYPE_CHECKING:
    from echo_v2.integrations.green.client import GreenClient
    from echo_v2.persistence.green_instance_pool import PooledInstance
    from echo_v2.services.green_instance_pool import GreenInstancePool

__all__ = ["OnboardingContext", "OnboardingService", "UserRepository"]

_logger = logging.getLogger("echo_v2.services.onboarding")


def _phone_log_value(phone: str | None) -> str | None:
    """Return a searchable phone value only when temporary debug logging is enabled."""
    if phone is None:
        return None
    if os.environ.get("ECHO_DEBUG_PHONE_LOGGING", "false").lower() in (
        "1", "true", "yes"
    ):
        return phone
    return f"hash:{_safe_correlation_id(phone)}"


def _safe_correlation_id(value: str) -> str:
    try:
        return correlation_id(value)
    except RuntimeError:
        return "unavailable"


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
_GUARD_QR_BUTTON_BODY = "ההוראות מוכנות. כשתהיו ליד שני הטלפונים, לחצו כדי להציג את ה־QR."

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

_GUARD_JOIN_PHRASE = "היי, אני רוצה להצטרף לאקו גארד"
_GUARD_NAME_PROMPT = "ברוכים הבאים לאקו גארד 💚\n\nאיך קוראים לך?"
_GUARD_CONTACT_PROMPT = (
    "נעים להכיר, {name}!\n\n"
    "עכשיו נצרף את הילד או הילדה שלך.\n\n"
    "לחצי על + ← איש קשר ובחרי את איש הקשר של הילד/ה."
)
_PARENT_CONTACT_PROMPT = "כדי לחבר את הילד/ה, שלח/י עכשיו את איש הקשר שלו/ה ב-WhatsApp."
_GUARD_SCAN_INSTRUCTIONS = (
    "מעולה, נשאר רק לחבר את הוואטסאפ של {name} 💚\n\n"
    "בטלפון של {name}:\n"
    "1. פתחו WhatsApp.\n"
    "2. היכנסו למכשירים מקושרים.\n"
    "3. לחצו על קישור מכשיר.\n"
    "4. סרקו את קוד ה־QR שנציג לכם.\n"
    "את הקוד פותחים בטלפון של ההורה וסורקים באמצעות הטלפון של הילד."
)
_GUARD_AUTHORIZATION_BODY = (
    "לפני החיבור, חשוב לאשר שאת/ה ההורה או האפוטרופוס של הילד/ה.\n\n"
    "Guard מנתח את שיחות ה-WhatsApp של הילד/ה כדי לזהות סימני מצוקה, "
    "בריונות, לחץ או פגיעה אפשרית. הוא שולח לך רק התראות כשנדרש לשים לב — "
    "לא את כל השיחות.\n\n"
    "האם לאשר אחריות הורית ולהפעיל את Guard?"
)
_GUARD_AUTH_BUTTON = {"id": "onboarding:authorize", "title": "אני מאשר/ת"}
_CHILD_CONNECT_BODY = "האישור התקבל. לחץ/י על Connect כדי לחבר את WhatsApp של הילד/ה."
_PARENT_SUCCESS = "החיבור הושלם בהצלחה. מספר WhatsApp של {name} אומת ו-Guard פעיל."
_PHONE_MISMATCH = "מספר ה-WhatsApp שחובר לא תואם לאיש הקשר ששלחת. לא הפעלתי את Guard."
_RECONNECT_INSTRUCTIONS = (
    "כדי לחבר מחדש את WhatsApp של {name}:\n"
    "פתח/י WhatsApp בטלפון של הילד/ה → הגדרות → מכשירים מקושרים → קשר מכשיר."
)
_RECONNECT_PREPARING = "אנחנו מכינים את קוד החיבור של {name}. נסו ללחוץ שוב על \"הצג QR\" בעוד כמה רגעים."
_RECONNECT_ALREADY_ACTIVE = "החיבור של {name} כבר חודש ו-Echo Guard פעיל."
_RECONNECT_UNVERIFIED = "נראה שחובר חשבון WhatsApp, אבל מספר הילד/ה עדיין לא אומת ו-Guard עדיין לא פעיל. אם חובר מספר שגוי, יש לנתק אותו ממכשירים מקושרים ולנסות שוב."
_RECONNECT_BUTTON = {"id": "guard:reconnect", "title": "חיבור מחדש"}

def _reconnect_qr_button(request_id: str) -> dict[str, str]:
    return {"id": f"guard:reconnect_qr:{request_id}", "title": "הצג QR"}
_PARENT_IDENTITY_RE = re.compile(
    r"^\s*אני\s+(?P<name>[\u0590-\u05FFA-Za-z][\u0590-\u05FFA-Za-z' -]{0,48}?)(?:\s+אמא\s+של|\s+אבא\s+של|\s+הורה\s+של)\s+.+$"
)

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


class ReconnectRequestRepository(Protocol):
    async def list_open_reconnect_requests(self, guardian_user_id: str) -> list[dict[str, object]]: ...

    async def list_open_reconnect_requests_for_child(self, child_user_id: str) -> list[dict[str, object]]: ...

    async def get_reconnect_request(self, request_id: str) -> dict[str, object] | None: ...

    async def close_open_reconnect_requests(self, child_user_id: str) -> None: ...

    async def complete_verified_reconnect(self, child_user_id: str) -> None: ...

    async def mark_verified_child_connection(self, child_user_id: str) -> None: ...


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
        provisioner: WhatsAppProvisioner,
        green_client: GreenClient | None = None,
        webhook_base_url: str,
        poll_interval: float = 5.0,
        poll_max_attempts: int = 60,
        pool: GreenInstancePool | None = None,
        guardian_child_links: GuardianChildLinkRepository | None = None,
        reconnect_requests: ReconnectRequestRepository | None = None,
        pipeline_events: PipelineEventRepository | None = None,
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
        self._guardian_child_links = guardian_child_links
        self._reconnect_requests = reconnect_requests
        self._pipeline_events = pipeline_events
        self._pending_contacts: dict[str, BotContact] = {}
        # Background instance-preparation tasks (pool miss path), keyed by
        # user_id. Prevents double-click from creating two instances.
        self._prepare_tasks: dict[str, asyncio.Task[None]] = {}
        # Authorization poll tasks, keyed by user_id. Ensures at most one poll
        # per user even on repeated "הצג QR" clicks.
        self._poll_tasks: dict[str, asyncio.Task[None]] = {}

    def _require_green_client(self) -> GreenClient:
        if self._green_client is None:
            raise RuntimeError("Green client is required for Green onboarding paths")
        return self._green_client

    def _trace_funnel_event(self, event: str, user_id: str | None = None) -> None:
        """Record an onboarding funnel event without blocking the webhook."""
        @traceable(name=f"wfm.onboarding.{event}")
        async def emit() -> None:
            run_tree = get_current_run_tree()
            if run_tree is not None and user_id is not None:
                run_tree.add_metadata({"user_id_hash": _safe_correlation_id(user_id)})

        asyncio.create_task(emit())

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

    async def _register_guardian(self, phone: str, name: str) -> None:
        """Register a bot-only guardian and ask for the child's contact."""
        normalized = self._normalize_phone(phone)
        if normalized is None:
            return
        existing = await self._user_repo.get_by_phone(normalized)
        if existing is None:
            user_id = await self._user_repo.create_user(
                normalized,
                timezone=_DEFAULT_TIMEZONE,
                first_name=name[:50],
                onboarding_status="active",
            )
        else:
            user_id, _status, _old_name = existing
            await self._user_repo.update_first_name(user_id, name[:50])
            await self._user_repo.update_onboarding_status(user_id, "active")
        await self._bot.send_text(normalized, _PARENT_CONTACT_PROMPT)

    async def _start_guardian_onboarding(self, phone: str) -> None:
        """Create or resume the persistent Guard guardian-name step."""
        normalized = self._normalize_phone(phone)
        if normalized is None:
            return
        existing = await self._user_repo.get_by_phone(normalized)
        if existing is None:
            await self._user_repo.create_user(
                normalized,
                timezone=_DEFAULT_TIMEZONE,
                first_name=None,
                onboarding_status="awaiting_guardian_name",
            )
            await self._bot.send_text(normalized, _GUARD_NAME_PROMPT)
            return

        user_id, status, name = existing
        if status == "awaiting_guardian_name":
            await self._bot.send_text(normalized, _GUARD_NAME_PROMPT)
        elif status == "active":
            link = await self._pending_for_guardian(user_id)
            if link is None:
                await self._bot.send_text(normalized, _PARENT_CONTACT_PROMPT)
            elif link.guardian_authorized_at is None:
                await self._bot.send_buttons(
                    normalized,
                    body_text=_GUARD_AUTHORIZATION_BODY,
                    buttons=[_GUARD_AUTH_BUTTON],
                )
            else:
                await self._send_guard_scan_instructions(link, normalized)
                await self._bot.send_buttons(
                    normalized,
                    body_text=_GUARD_QR_BUTTON_BODY,
                    buttons=[_SHOW_QR_BUTTON],
                )
        elif status == "pending" and name is None:
            await self._bot.send_text(normalized, _NAME_PROMPT)
        else:
            await self._bot.send_text(normalized, _PARENT_CONTACT_PROMPT)

    async def _start_child_flow(self, ctx: OnboardingContext, contact: BotContact) -> None:
        child_phone = self._normalize_phone(contact.phone)
        if child_phone is None or child_phone == ctx.phone:
            await self._bot.send_text(ctx.phone, "לא הצלחתי לזהות איש קשר תקין של ילד/ה. נסה/י שוב.")
            return
        if not contact.name.strip():
            self._pending_contacts[ctx.user_id] = contact
            await self._bot.send_text(ctx.phone, "איך קוראים לילד/ה? שלח/י את השם.")
            return
        await self._create_child_from_contact(ctx, contact, contact.name)

    async def _create_child_from_contact(
        self,
        ctx: OnboardingContext,
        contact: BotContact,
        child_name: str | None = None,
    ) -> None:
        if self._guardian_child_links is None:
            return
        child_phone = self._normalize_phone(contact.phone)
        if child_phone is None:
            await self._bot.send_text(ctx.phone, "לא הצלחתי לזהות איש קשר תקין של ילד/ה. נסה/י שוב.")
            return
        name = (child_name or contact.name).strip()[:50]
        if not name:
            await self._bot.send_text(ctx.phone, "צריך שם לילד/ה כדי להמשיך.")
            return
        existing = await self._user_repo.get_by_phone(child_phone)
        if existing is None:
            child_id = await self._user_repo.create_user(
                child_phone,
                timezone=_DEFAULT_TIMEZONE,
                first_name=name,
                onboarding_status="active",
            )
        else:
            child_id, _status, old_name = existing
            if not old_name:
                await self._user_repo.update_first_name(child_id, name)
        link = await self._guardian_child_links.upsert_pending(
            guardian_user_id=ctx.user_id,
            child_user_id=child_id,
            now=datetime.now(timezone.utc),
        )
        self._pending_contacts.pop(ctx.user_id, None)
        if link.status.value == "active":
            await self._bot.send_text(ctx.phone, f"{name} כבר מחובר/ת ל-Guard.")
            return
        await self._bot.send_buttons(
            ctx.phone,
            body_text=_GUARD_AUTHORIZATION_BODY,
            buttons=[_GUARD_AUTH_BUTTON],
        )

    async def _pending_for_guardian(
        self, guardian_id: str
    ) -> GuardianChildLink | None:
        if self._guardian_child_links is None:
            return None
        pending = await self._guardian_child_links.get_pending_for_guardian(guardian_id)
        authorized = [link for link in pending if link.guardian_authorized_at is not None]
        return (authorized or pending)[-1] if (authorized or pending) else None

    async def _send_guard_scan_instructions(
        self, link: GuardianChildLink, guardian_phone: str
    ) -> None:
        """Explain linked-device scanning before starting the QR window."""
        child_phone = await self._lookup_phone(link.child_user_id)
        child_name = None
        if child_phone is not None:
            child = await self._user_repo.get_by_phone(child_phone)
            child_name = child[2] if child is not None else None
        await self._bot.send_text(
            guardian_phone,
            _GUARD_SCAN_INSTRUCTIONS.format(name=child_name or "הילד/ה"),
        )

    async def handle_onboarding_authorize(self, phone: str) -> None:
        """Record guardian authorization and expose the QR action."""
        if self._guardian_child_links is None:
            return
        ctx = await self.resolve_context(phone)
        if ctx is None:
            return
        link = await self._pending_for_guardian(ctx.user_id)
        if link is None:
            await self._bot.send_text(ctx.phone, _PARENT_CONTACT_PROMPT)
            return
        if link.guardian_authorized_at is None:
            await self._guardian_child_links.authorize_guardian(
                guardian_user_id=ctx.user_id,
                child_user_id=link.child_user_id,
                now=datetime.now(timezone.utc),
            )
        await self._send_guard_scan_instructions(link, ctx.phone)
        await self._bot.send_buttons(
            ctx.phone,
            body_text=_GUARD_QR_BUTTON_BODY,
            buttons=[_SHOW_QR_BUTTON],
        )

    # --- Unknown user entry (consent / intro) --------------------------------

    async def handle_parent_event(self, event: BotEvent) -> bool:
        """Handle parent registration and child-contact events.

        Returns ``True`` when the parent-child flow owns the event. This is
        intentionally separate from the legacy single-user onboarding path.
        """
        if self._guardian_child_links is None:
            return False
        ctx = await self.resolve_context(event.user_phone)
        if ctx is None or ctx.onboarding_status != "active":
            return False
        if event.type is BotEventType.CONTACT and event.contact is not None:
            await self._start_child_flow(ctx, event.contact)
            return True
        if event.type is BotEventType.TEXT and event.text:
            if event.text.strip() == _GUARD_JOIN_PHRASE:
                await self._start_guardian_onboarding(ctx.phone)
                return True
            pending = getattr(self, "_pending_contacts", {}).get(ctx.user_id)
            if pending is not None:
                await self._create_child_from_contact(ctx, pending, event.text)
                return True
        return False

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
        phone_hash = _safe_correlation_id(phone)
        _logger.info(
            "onboarding unknown event type=%s phone_hash=%s button_id=%s",
            event.type.value,
            phone_hash,
            event.button_id,
        )

        if event.type is BotEventType.TEXT and event.text:
            text = event.text.strip()
            if text == _GUARD_JOIN_PHRASE and self._guardian_child_links is not None:
                await self._start_guardian_onboarding(phone)
                return
            match = _PARENT_IDENTITY_RE.match(text)
            if match is not None and self._guardian_child_links is not None:
                await self._register_guardian(phone, match.group("name").strip())
                return

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
            self._trace_funnel_event("intro_shown")
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

        phone_hash = _safe_correlation_id(normalized)
        _logger.info("onboarding start phone_hash=%s", phone_hash)
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

        self._trace_funnel_event("consent", user_id)
        # Ask for name.
        await self._bot.send_text(normalized, _NAME_PROMPT)

    # --- Name response → store name + start provisioning --------------------

    async def handle_name_response(self, phone: str, name: str) -> bool:
        """Handle a text message from a user in a name-collection step.

        Guard guardians use ``awaiting_guardian_name`` and become active after
        their name is stored; legacy users use ``pending`` and continue to the
        existing pairing flow.

        Returns ``True`` if this was handled as a name response.
        """
        ctx = await self.resolve_context(phone)
        if ctx is None:
            return False
        if name.strip() == _GUARD_JOIN_PHRASE:
            if ctx.onboarding_status == "awaiting_guardian_name":
                await self._bot.send_text(ctx.phone, _GUARD_NAME_PROMPT)
                return True
            if ctx.onboarding_status == "active" and self._guardian_child_links is not None:
                await self._start_guardian_onboarding(ctx.phone)
                return True
        if ctx.onboarding_status == "awaiting_guardian_name":
            clean_name = name.strip()[:50]
            if not clean_name:
                await self._bot.send_text(ctx.phone, _GUARD_NAME_PROMPT)
                return True
            await self._user_repo.update_first_name(ctx.user_id, clean_name)
            await self._user_repo.update_onboarding_status(ctx.user_id, "active")
            await self._bot.send_text(
                ctx.phone, _GUARD_CONTACT_PROMPT.format(name=clean_name)
            )
            return True
        if ctx.onboarding_status != "pending":
            return False
        if ctx.first_name is not None:
            # Already has a name — pairing in progress. Don't eat text.
            return False

        clean_name = name.strip()[:50]
        if not clean_name:
            return False

        # Store the name (still pending — not active until authorized).
        _logger.info(
            "onboarding name received user_hash=%s name_length=%d",
            _safe_correlation_id(ctx.user_id),
            len(clean_name),
        )
        await self._user_repo.update_first_name(ctx.user_id, clean_name)
        self._trace_funnel_event("name_entered", ctx.user_id)

        # Pool hit: claim the instance and send the QR immediately, skipping
        # the [חבר אותי] button. Pool miss (or no pool): send the connect
        # button so the user explicitly starts pairing — the pool may have
        # been refilled by the time they click.
        if await self._claim_pool_and_send_qr(ctx.user_id, ctx.phone):
            _logger.info(
                "onboarding: name set for user %s, pool hit — QR sent",
                ctx.user_id,
            )
        else:
            await self._bot.send_buttons(
                ctx.phone,
                body_text=_NAME_CONFIRMATION.format(name=clean_name),
                buttons=[_CONNECT_BUTTON],
            )
            _logger.info(
                "onboarding: name set for user %s, awaiting connect click",
                ctx.user_id,
            )
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
        user_hash = _safe_correlation_id(user_id)
        self._trace_funnel_event("pairing_start", user_id)
        existing = await self._connection_repo.get_by_user(user_id)
        _logger.info(
            "onboarding pairing start user_hash=%s existing=%s provider=%s",
            user_hash,
            existing is not None,
            existing.ref.provider if existing is not None else None,
        )
        if existing is not None:
            # Connection already exists — re-send QR or "still preparing".
            if existing.status == ConnectionStatus.PROVISIONING:
                await self._bot.send_text(phone, _STILL_PREPARING)
            else:
                await self._send_qr_and_poll(user_id, phone, existing)
            return

        pooled = await self._pool.claim(user_id) if self._pool else None

        if pooled is not None:
            self._trace_funnel_event("pool_hit", user_id)
            # Pool hit — instance ready (notAuthorized), send QR immediately.
            await self._claim_pool_and_send_qr(user_id, phone, pooled)
        else:
            self._trace_funnel_event("pool_miss", user_id)
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
            _logger.info("onboarding provisioning started user_hash=%s", _safe_correlation_id(user_id))
            webhook_token = secrets.token_urlsafe(32)
            webhook_url = f"{self._webhook_base_url}/webhooks/whatsapp/green"
            config = ConnectionConfig(
                webhook_url=webhook_url,
                webhook_token=webhook_token,
                subscriptions=WhatsAppEventSubscription(),
            )
            created = await self._provisioner.create_connection(config)
            _logger.info(
                "onboarding provisioning created user_hash=%s provider=%s connection_hash=%s",
                _safe_correlation_id(user_id),
                created.ref.provider,
                _safe_correlation_id(created.ref.provider_connection_id),
            )
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

            # Wait until the provider reports that pairing can begin.
            ready = await self._wait_until_ready(created.ref)
            if not ready:
                await self._user_repo.update_onboarding_status(user_id, "failed")
                await self._bot.send_text(
                    phone,
                    "מצטער, לא הצלחתי ליצור את החיבור. נסה שוב מאוחר יותר.",
                )
                return

            # Update to PAIRING_REQUIRED. Baileys can deliver the QR in the
            # same connect action; Green retains its explicit show-QR button.
            await self._connection_repo.update_status(
                created.ref,
                ConnectionStatus.PAIRING_REQUIRED,
                "notAuthorized",
            )
            if created.ref.provider == "baileys":
                connection = await self._connection_repo.get_by_user(user_id)
                if connection is None:
                    raise RuntimeError("created Baileys connection was not persisted")
                await self._send_provider_qr_and_poll(user_id, phone, connection)
                _logger.info(
                    "onboarding: Baileys QR started after connect user_hash=%s",
                    _safe_correlation_id(user_id),
                )
            else:
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

    async def _record_reconnect_event(
        self, name: str, *, child_id: str, request_id: str | None = None,
        outcome: str = "recorded",
    ) -> None:
        child_hash = _safe_correlation_id(child_id)
        _logger.info(
            "reconnect event=%s request_id=%s child_hash=%s outcome=%s",
            name, request_id, child_hash, outcome,
        )
        if self._pipeline_events is not None:
            try:
                await self._pipeline_events.record(PipelineEvent(
                    event_name=name, pipeline="guard_reconnect", status=outcome,
                    event_id=request_id, user_id_hash=child_hash,
                    metadata={"request_id": request_id} if request_id is not None else {},
                ))
            except Exception:
                _logger.exception("reconnect event persistence failed event=%s", name)

    async def handle_guard_reconnect(
        self, phone: str, request_id: str | None = None
    ) -> None:
        if self._reconnect_requests is None:
            return
        ctx = await self.resolve_context(phone)
        if ctx is None:
            return
        request: dict[str, object] | None = None
        if request_id is not None:
            candidate = await self._reconnect_requests.get_reconnect_request(request_id)
            if candidate is not None and str(candidate.get("guardian_user_id")) == ctx.user_id:
                request = candidate
        else:
            requests = await self._reconnect_requests.list_open_reconnect_requests(ctx.user_id)
            if requests:
                request = requests[0]
        if request is None:
            await self._record_reconnect_event(
                "reconnect.request_rejected", child_id=ctx.user_id, outcome="unknown_request",
            )
            await self._bot.send_text(phone, "החיבור כבר חודש או שהבקשה אינה זמינה.")
            return
        child_name = str(request.get("first_name") or "הילד/ה")
        child_id = str(request["child_user_id"])
        link = (
            await self._guardian_child_links.get_active_for_pair(
                guardian_user_id=ctx.user_id, child_user_id=child_id
            )
            if self._guardian_child_links is not None
            else None
        )
        conn = await self._connection_repo.get_by_user(child_id)
        if (
            link is None
            or conn is None
            or (
                conn.id is not None
                and request.get("connection_id") is not None
                and str(conn.id) != str(request.get("connection_id"))
            )
        ):
            await self._record_reconnect_event(
                "reconnect.request_rejected", child_id=child_id,
                request_id=str(request["id"]), outcome="not_authorized_or_connection_changed",
            )
            await self._bot.send_text(phone, "הבקשה אינה זמינה.")
            return
        if request.get("status") != "open":
            await self._record_reconnect_event(
                "reconnect.request_closed", child_id=child_id, request_id=str(request["id"]),
            )
            await self._bot.send_text(phone, _RECONNECT_ALREADY_ACTIVE.format(name=child_name))
            return
        if conn.status is ConnectionStatus.CONNECTED:
            await self._record_reconnect_event(
                "reconnect.account_unverified", child_id=child_id, request_id=str(request["id"]),
            )
            await self._bot.send_text(phone, _RECONNECT_UNVERIFIED)
            return
        reconnect_id = str(request["id"])
        await self._record_reconnect_event(
            "reconnect.parent_requested", child_id=child_id, request_id=reconnect_id,
        )
        await self._bot.send_buttons(
            phone,
            body_text=_RECONNECT_INSTRUCTIONS.format(name=child_name),
            buttons=[_reconnect_qr_button(reconnect_id)],
        )

    async def handle_guard_reconnect_qr(self, phone: str, request_id: str) -> None:
        if self._reconnect_requests is None:
            return
        ctx = await self.resolve_context(phone)
        request = await self._reconnect_requests.get_reconnect_request(request_id)
        if ctx is None or request is None or str(request.get("guardian_user_id")) != ctx.user_id:
            await self._bot.send_text(phone, "הבקשה אינה זמינה.")
            return
        child_id = str(request["child_user_id"])
        child_name = str(request.get("first_name") or "הילד/ה")
        link = (
            await self._guardian_child_links.get_active_for_pair(
                guardian_user_id=ctx.user_id, child_user_id=child_id
            )
            if self._guardian_child_links is not None
            else None
        )
        conn = await self._connection_repo.get_by_user(child_id)
        if link is None or conn is None or (
            conn.id is not None
            and request.get("connection_id") is not None
            and str(conn.id) != str(request.get("connection_id"))
        ):
            await self._record_reconnect_event(
                "reconnect.request_rejected", child_id=child_id,
                request_id=str(request["id"]), outcome="not_authorized_or_connection_changed",
            )
            await self._bot.send_text(phone, "הבקשה אינה זמינה.")
            return
        if request.get("status") != "open":
            await self._record_reconnect_event(
                "reconnect.request_closed", child_id=child_id, request_id=str(request["id"]),
            )
            await self._bot.send_text(phone, _RECONNECT_ALREADY_ACTIVE.format(name=child_name))
            return
        if conn.status is ConnectionStatus.CONNECTED:
            await self._record_reconnect_event(
                "reconnect.account_unverified", child_id=child_id, request_id=str(request["id"]),
            )
            await self._bot.send_text(phone, _RECONNECT_UNVERIFIED)
            return
        if conn is None:
            await self._bot.send_text(phone, "החיבור אינו זמין כרגע. נסו שוב מאוחר יותר.")
            return
        await self._record_reconnect_event(
            "reconnect.qr_requested", child_id=child_id, request_id=request_id,
        )
        try:
            result = await self._provisioner.get_pairing_qr(conn.ref)
        except Exception as exc:
            await self._record_reconnect_event(
                "reconnect.qr_failed", child_id=child_id,
                request_id=request_id, outcome=type(exc).__name__,
            )
            raise
        if result.outcome is PairingOutcome.ALREADY_AUTHORIZED:
            await self._record_reconnect_event(
                "reconnect.qr_already_authorized", child_id=child_id, request_id=request_id,
            )
            await self.handle_connection_established_by_id(child_id)
            return
        if result.outcome is PairingOutcome.PREPARING:
            await self._record_reconnect_event(
                "reconnect.qr_preparing", child_id=child_id, request_id=request_id,
            )
            await self._bot.send_text(phone, _RECONNECT_PREPARING.format(name=child_name))
            return
        if result.outcome is not PairingOutcome.QR_READY or result.qr is None:
            await self._record_reconnect_event(
                "reconnect.qr_unavailable", child_id=child_id, request_id=request_id,
            )
            await self._bot.send_text(phone, "לא הצלחתי להכין את קוד החיבור. נסו שוב מאוחר יותר.")
            return
        try:
            await self._bot.send_image(
                phone,
                image_bytes=base64.b64decode(result.qr.image_base64),
                mime_type="image/png",
                caption=_QR_CAPTION,
            )
        except Exception as exc:
            await self._record_reconnect_event(
                "reconnect.qr_delivery_failed", child_id=child_id,
                request_id=request_id, outcome=type(exc).__name__,
            )
            raise
        await self._record_reconnect_event(
            "reconnect.qr_sent", child_id=child_id, request_id=request_id,
        )
        self._ensure_provider_poll_task(child_id, phone, conn.ref)

    async def _claim_pool_and_send_qr(
        self,
        user_id: str,
        phone: str,
        pooled: PooledInstance | None = None,
    ) -> bool:
        """Claim a pool instance (if not passed in) and send the QR immediately.

        Returns ``True`` if a pool instance was claimed and the QR flow
        started. Returns ``False`` if the pool is missing or empty (pool
        miss) — the caller decides what to do (send the connect button, or
        start background creation).

        If ``pooled`` is already claimed (passed in by the caller), it is
        finalized here. This shared by :meth:`handle_name_response` (skip
        the connect button on pool hit) and :meth:`start_pairing`.
        """
        if pooled is None:
            if self._pool is None:
                return False
            pooled = await self._pool.claim(user_id)
            if pooled is None:
                return False

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
        return True

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
        if conn.ref.provider != "green":
            await self._send_provider_qr_and_poll(user_id, phone, conn)
            return
        api_token = conn.credentials.data.decode("utf-8")
        sent = await self._send_pairing_qr(
            user_id, phone, conn.ref, api_token,
        )
        if sent:
            self._trace_funnel_event("qr_shown", user_id)
        if not sent:
            return  # QR failed and OTP fallback also failed.
        self._ensure_poll_task(
            user_id, phone, conn.ref.provider_connection_id, api_token,
            pairing_already_sent=True,
        )

    async def _send_provider_qr_and_poll(
        self,
        user_id: str,
        phone: str,
        conn: StoredConnection,
    ) -> None:
        """Send a provider-neutral QR and poll provider-neutral status."""
        _logger.info(
            "onboarding QR requested user_hash=%s provider=%s connection_hash=%s",
            _safe_correlation_id(user_id),
            conn.ref.provider,
            _safe_correlation_id(conn.ref.provider_connection_id),
        )
        result = await self._provisioner.get_pairing_qr(conn.ref)
        if result.outcome is PairingOutcome.ALREADY_AUTHORIZED:
            await self.handle_connection_established(user_id, phone)
            return
        if result.outcome is PairingOutcome.PREPARING:
            await self._bot.send_text(phone, "אנחנו מכינים את קוד החיבור. נסה/י ללחוץ שוב על \"הצג QR\" בעוד כמה רגעים.")
            return
        if result.outcome is not PairingOutcome.QR_READY or result.qr is None:
            await self._user_repo.update_onboarding_status(user_id, "failed")
            await self._bot.send_text(
                phone,
                "מצטער, לא הצלחתי לקבל קוד QR. נסה שוב מאוחר יותר.",
            )
            return
        try:
            await self._bot.send_image(
                phone,
                image_bytes=base64.b64decode(result.qr.image_base64),
                mime_type="image/png",
                caption=_QR_CAPTION,
            )
        except Exception:
            _logger.exception("onboarding: failed to send Baileys QR")
            await self._user_repo.update_onboarding_status(user_id, "failed")
            return
        self._trace_funnel_event("qr_shown", user_id)
        _logger.info("onboarding QR sent user_hash=%s", _safe_correlation_id(user_id))
        self._ensure_provider_poll_task(user_id, phone, conn.ref)

    def _ensure_provider_poll_task(
        self,
        user_id: str,
        phone: str,
        connection: ConnectionRef,
    ) -> None:
        existing = self._poll_tasks.get(user_id)
        if existing is not None and not existing.done():
            return
        self._poll_tasks[user_id] = asyncio.create_task(
            self._poll_provider_status(user_id, phone, connection)
        )

    async def _poll_provider_status(
        self,
        user_id: str,
        phone: str,
        connection: ConnectionRef,
    ) -> None:
        for _ in range(self._poll_max_attempts):
            try:
                snapshot = await self._provisioner.get_status(connection)
                _logger.info(
                    "onboarding provider status user_hash=%s status=%s raw=%s",
                    _safe_correlation_id(user_id),
                    snapshot.status.value,
                    snapshot.provider_raw_status,
                )
                await self._connection_repo.update_status(
                    connection, snapshot.status, snapshot.provider_raw_status
                )
                if snapshot.status is ConnectionStatus.CONNECTED:
                    await self.handle_connection_established(user_id, phone)
                    return
                if snapshot.status in (
                    ConnectionStatus.PAIRING_REQUIRED,
                    ConnectionStatus.CONNECTING,
                    ConnectionStatus.PROVISIONING,
                ):
                    await asyncio.sleep(self._poll_interval)
                    continue
            except Exception:
                _logger.info("onboarding: provider status poll failed", exc_info=True)
            await asyncio.sleep(self._poll_interval)
        _logger.warning(
            "onboarding provider status poll timed out user_hash=%s",
            _safe_correlation_id(user_id),
        )
        await self._bot.send_text(phone, _OTP_TIMED_OUT)

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
        self,
        connection: ConnectionRef | str,
        api_token: str | None = None,
    ) -> bool:
        """Poll provider-neutral status until pairing is required or connected.

        The string/token form remains as a compatibility shim for existing
        Green-focused tests and callers; new providers use ``ConnectionRef``.
        """
        for _ in range(self._poll_max_attempts):
            try:
                if isinstance(connection, ConnectionRef):
                    snapshot = await self._provisioner.get_status(connection)
                    if snapshot.status in (
                        ConnectionStatus.PAIRING_REQUIRED,
                        ConnectionStatus.CONNECTED,
                    ):
                        return snapshot.status is ConnectionStatus.PAIRING_REQUIRED
                elif self._green_client is not None and api_token is not None:
                    state = await self._require_green_client().get_state_instance(
                        connection, api_token,
                    )
                    if state == "notAuthorized":
                        return True
                    if state == "authorized":
                        return False
            except Exception:
                _logger.debug("onboarding: transient readiness status error", exc_info=True)
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
                state = await self._require_green_client().get_state_instance(
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
                        if sent:
                            self._trace_funnel_event("qr_shown", user_id)
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

    async def _activate_child_connection(self, child_user_id: str) -> bool:
        """Verify the connected child phone and activate all guardian links."""
        if self._guardian_child_links is None:
            return False
        pending_links = await self._guardian_child_links.list_pending_for_child(child_user_id)
        link: GuardianChildLink | None = pending_links[0] if pending_links else None
        if link is None and self._reconnect_requests is not None:
            open_requests = await self._reconnect_requests.list_open_reconnect_requests_for_child(
                child_user_id
            )
            for request in open_requests:
                link = await self._guardian_child_links.get_active_for_pair(
                    guardian_user_id=str(request["guardian_user_id"]),
                    child_user_id=child_user_id,
                )
                if link is not None:
                    break
        if link is None or link.guardian_authorized_at is None:
            return False
        conn = await self._connection_repo.get_by_user(child_user_id)
        child_phone = await self._lookup_phone(child_user_id)
        normalized_child_phone = (
            self._normalize_phone(child_phone) if child_phone is not None else None
        )
        if conn is None or normalized_child_phone is None:
            return False
        account_phone: str | None = None
        if conn.ref.provider == "baileys":
            try:
                snapshot = await self._provisioner.get_status(conn.ref)
                account_phone = (
                    snapshot.phone_number if snapshot.status is ConnectionStatus.CONNECTED else None
                )
            except Exception:
                _logger.exception("onboarding: failed to verify connected child phone")
                if self._reconnect_requests is not None and await self._reconnect_requests.list_open_reconnect_requests_for_child(
                    child_user_id
                ):
                    raise
        else:
            get_account_phone = getattr(self._green_client, "get_account_phone", None)
            if get_account_phone is not None:
                try:
                    api_token = conn.credentials.data.decode("utf-8")
                    account_phone = await get_account_phone(
                        conn.ref.provider_connection_id, api_token
                    )
                except Exception:
                    _logger.exception("onboarding: failed to verify connected child phone")

        normalized_account_phone = (
            self._normalize_phone(account_phone) if account_phone is not None else None
        )
        if account_phone is None:
            verification_reason = "provider_phone_missing"
        elif normalized_account_phone is None:
            verification_reason = "provider_phone_invalid"
        elif normalized_account_phone != normalized_child_phone:
            verification_reason = "provider_phone_mismatch"
        else:
            verification_reason = "match"
        _logger.info(
            "onboarding child phone verification provider=%s reason=%s "
            "child_phone=%s account_phone=%s",
            conn.ref.provider,
            verification_reason,
            _phone_log_value(normalized_child_phone),
            _phone_log_value(normalized_account_phone),
        )
        if verification_reason != "match":
            _logger.warning(
                "onboarding child connection rejected provider=%s reason=%s "
                "child_phone=%s account_phone=%s connection_hash=%s",
                conn.ref.provider,
                verification_reason,
                _phone_log_value(normalized_child_phone),
                _phone_log_value(normalized_account_phone),
                _safe_correlation_id(conn.ref.provider_connection_id),
            )
            if self._reconnect_requests is not None:
                open_requests = await self._reconnect_requests.list_open_reconnect_requests_for_child(
                    child_user_id
                )
                for request in open_requests:
                    await self._record_reconnect_event(
                        "reconnect.account_rejected", child_id=child_user_id,
                        request_id=str(request["id"]), outcome=verification_reason,
                    )
            guardian_phone = await self._lookup_phone(link.guardian_user_id)
            if guardian_phone is not None:
                await self._bot.send_text(guardian_phone, _PHONE_MISMATCH)
            return True
        open_requests = (
            await self._reconnect_requests.list_open_reconnect_requests_for_child(child_user_id)
            if self._reconnect_requests is not None
            else []
        )
        if open_requests and self._reconnect_requests is not None:
            await self._reconnect_requests.complete_verified_reconnect(child_user_id)
            for request in open_requests:
                await self._record_reconnect_event(
                    "reconnect.verified", child_id=child_user_id,
                    request_id=str(request["id"]), outcome="completed",
                )
        else:
            now_utc = datetime.now(timezone.utc)
            for pending_link in pending_links:
                await self._guardian_child_links.activate(
                    guardian_user_id=pending_link.guardian_user_id,
                    child_user_id=child_user_id,
                    now=now_utc,
                )
            if conn.ref.provider == "baileys" and self._reconnect_requests is not None:
                await self._reconnect_requests.mark_verified_child_connection(child_user_id)
        child = await self._user_repo.get_by_phone(normalized_child_phone)
        child_name = child[2] if child and child[2] else normalized_child_phone
        guardian_ids = (
            [str(request["guardian_user_id"]) for request in open_requests]
            if open_requests
            else [pl.guardian_user_id for pl in pending_links] or ([link.guardian_user_id] if link else [])
        )
        for guardian_id in dict.fromkeys(guardian_ids):
            if open_requests and await self._guardian_child_links.get_active_for_pair(
                guardian_user_id=guardian_id, child_user_id=child_user_id
            ) is None:
                continue
            guardian_phone = await self._lookup_phone(guardian_id)
            if guardian_phone is not None:
                await self._bot.send_text(
                    guardian_phone, _PARENT_SUCCESS.format(name=child_name)
                )
        return True

    async def handle_connection_established(
        self,
        user_id: str,
        phone: str,
    ) -> None:
        """Complete either child-link activation or legacy onboarding."""
        if await self._activate_child_connection(user_id):
            return
        self._trace_funnel_event("authorized", user_id)
        _logger.info(
            "onboarding connection established user_hash=%s",
            _safe_correlation_id(user_id),
        )
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
        return onboarding_status in {"pending", "awaiting_guardian_name"}

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
        """Handle Connect for either legacy or parent-child onboarding."""
        if self._guardian_child_links is not None:
            parent_ctx = await self.resolve_context(phone)
            if parent_ctx is not None and parent_ctx.onboarding_status == "active":
                link = await self._pending_for_guardian(parent_ctx.user_id)
                if link is not None:
                    if link.guardian_authorized_at is None:
                        await self._bot.send_text(parent_ctx.phone, _GUARD_AUTHORIZATION_BODY)
                        return
                    await self._send_guard_scan_instructions(link, parent_ctx.phone)
                    await self._bot.send_buttons(
                        parent_ctx.phone,
                        body_text=_GUARD_QR_BUTTON_BODY,
                        buttons=[_SHOW_QR_BUTTON],
                    )
                    return
                await self._bot.send_text(parent_ctx.phone, _PARENT_CONTACT_PROMPT)
                return
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
        """Handle [הצג QR] for either legacy or child pairing."""
        ctx = await self.resolve_context(phone)
        if ctx is None:
            return
        if self._guardian_child_links is not None and ctx.onboarding_status == "active":
            link = await self._pending_for_guardian(ctx.user_id)
            if link is not None and link.guardian_authorized_at is not None:
                await self.show_pairing_qr(link.child_user_id, ctx.phone)
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
            state = await self._require_green_client().get_state_instance(
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
            code = await self._require_green_client().get_authorization_code(
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
            raw = await self._require_green_client().get_qr_ws(
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
            code = await self._require_green_client().get_authorization_code(
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

        if conn.ref.provider != "green":
            await self._send_provider_qr_and_poll(user_id, phone, conn)
            return True

        api_token = conn.credentials.data.decode("utf-8")

        # Check Green API state — it's the source of truth.
        try:
            state = await self._require_green_client().get_state_instance(
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
        """Normalize a user phone or provider WhatsApp JID to E.164.

        Baileys may report the connected account as a WhatsApp JID such as
        ``972501234567:0@s.whatsapp.net`` rather than a plain phone number.
        Strip the device and domain suffix before applying the canonical phone
        parser used by the users table.
        """
        raw_phone = phone.strip().split("@", 1)[0].split(":", 1)[0]
        try:
            return normalize_phone_e164(raw_phone)
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
