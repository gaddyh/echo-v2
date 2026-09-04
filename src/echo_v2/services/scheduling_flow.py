"""SchedulingFlowService — multi-turn scheduling flow state machine.

Orchestrates the 3-step scheduling conversation:

1. User sends a **vCard/contact** → bot acks, stores recipient in context.
2. User sends **message text** → bot asks "when to send to {name}?"
3. User sends **time expression** → parser resolves it to UTC, creates a
   ``ScheduledAction``, bot confirms.

The service is the glue between:
* The bot channel (``BotChannel``) — sends confirmations to the user.
* The conversation state repo — tracks per-user flow state.
* The time parser — converts the time expression to UTC.
* The scheduling service — creates the ``ScheduledAction``.
* A user resolver — maps the bot sender's phone number to a ``user_id``.

Edge cases handled:
* vCard in ``AWAITING_MESSAGE`` → replace recipient, restart from step 2.
* Text in ``IDLE`` → "Send me a contact first."
* Time in the past → "That time has already passed."
* User sends "cancel" / "בטל" → abort to ``IDLE``.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from echo_v2.domain.conversation import SchedulingFlowContext, SchedulingFlowState
from echo_v2.domain.scheduling import ScheduledActionType
from echo_v2.persistence.conversation_state import ConversationStateRepository
from echo_v2.ports.bot import BotChannel, BotEvent, BotEventType
from echo_v2.services.scheduling import SchedulingService
from echo_v2.services.time_parser import TimeParseError, TimeParser

__all__ = ["SchedulingFlowService", "UserResolver"]

_logger = logging.getLogger("echo_v2.services.scheduling_flow")

# Cancel keywords (Hebrew + English).
_CANCEL_KEYWORDS = {"cancel", "בטל", "ביטול", "stop"}


@runtime_checkable
class UserResolver(Protocol):
    """Resolve a bot sender's phone number to a user_id + timezone.

    The application layer implements this against the users table.
    Returns ``None`` if the phone number is not a registered Echo user.
    """

    async def resolve(self, phone: str) -> tuple[str, str] | None:
        """Return ``(user_id, timezone)`` or ``None`` if unknown."""
        ...


class SchedulingFlowService:
    """State machine for the multi-turn scheduling flow."""

    def __init__(
        self,
        bot: BotChannel,
        state_repo: ConversationStateRepository,
        scheduling_service: SchedulingService,
        time_parser: TimeParser,
        user_resolver: UserResolver,
    ) -> None:
        self._bot = bot
        self._state_repo = state_repo
        self._scheduling = scheduling_service
        self._time_parser = time_parser
        self._user_resolver = user_resolver

    async def handle(self, event: BotEvent) -> None:
        """Process an incoming bot event through the flow state machine."""
        # Resolve the sender to a user.
        user_info = await self._user_resolver.resolve(event.user_phone)
        if user_info is None:
            await self._bot.send_text(
                event.user_phone,
                "I don't know you yet. Please connect your WhatsApp first.",
            )
            return

        user_id, user_timezone = user_info
        ctx = await self._state_repo.get(user_id)

        # Check for cancel at any state.
        if event.type is BotEventType.TEXT and _is_cancel(event.text):
            await self._cancel_flow(ctx, event.user_phone)
            return

        if event.type is BotEventType.CONTACT:
            await self._handle_contact(ctx, event, event.user_phone)
            return

        if event.type is BotEventType.TEXT:
            await self._handle_text(ctx, event, event.user_phone, user_timezone)
            return

    async def _handle_contact(
        self,
        ctx: SchedulingFlowContext,
        event: BotEvent,
        user_phone: str,
    ) -> None:
        """Step 1: user sent a vCard — store recipient, ask for message."""
        contact = event.contact
        if contact is None:
            return

        # Convert phone to Green API chat_id format.
        chat_id = _phone_to_chat_id(contact.phone)

        new_ctx = SchedulingFlowContext(
            user_id=ctx.user_id,
            state=SchedulingFlowState.AWAITING_MESSAGE,
            recipient_phone=chat_id,
            recipient_name=contact.name or contact.phone,
        )
        await self._state_repo.save(new_ctx)

        name = contact.name or contact.phone
        await self._bot.send_text(
            user_phone,
            f"✅ קיבלתי את {name}. מה לשלוח?",
        )

    async def _handle_text(
        self,
        ctx: SchedulingFlowContext,
        event: BotEvent,
        user_phone: str,
        user_timezone: str,
    ) -> None:
        """Handle text based on current flow state."""
        text = (event.text or "").strip()
        if not text:
            return

        if ctx.state is SchedulingFlowState.IDLE:
            await self._bot.send_text(
                user_phone,
                "שלח לי איש קשר כדי להתחיל. 📇",
            )
            return

        if ctx.state is SchedulingFlowState.AWAITING_MESSAGE:
            # Step 2: store the message, ask for time.
            new_ctx = SchedulingFlowContext(
                user_id=ctx.user_id,
                state=SchedulingFlowState.AWAITING_TIME,
                recipient_phone=ctx.recipient_phone,
                recipient_name=ctx.recipient_name,
                message=text,
            )
            await self._state_repo.save(new_ctx)

            name = ctx.recipient_name or "the contact"
            await self._bot.send_text(
                user_phone,
                f"מתי לשלוח ל{name}?",
            )
            return

        if ctx.state is SchedulingFlowState.AWAITING_TIME:
            await self._handle_time(ctx, text, user_phone, user_timezone)
            return

    async def _handle_time(
        self,
        ctx: SchedulingFlowContext,
        text: str,
        user_phone: str,
        user_timezone: str,
    ) -> None:
        """Step 3: parse time, create ScheduledAction, confirm."""
        try:
            execute_at = await self._time_parser.parse(
                text,
                user_timezone=user_timezone,
            )
        except TimeParseError as exc:
            await self._bot.send_text(
                user_phone,
                f"לא הצלחתי להבין את הזמן. נסה שוב (למשל: מחר ב-8, בעוד שעה, 20:00).\n"
                f"({exc})",
            )
            return

        # Create the scheduled action.
        await self._scheduling.create(
            user_id=ctx.user_id,
            type=ScheduledActionType.SEND_WHATSAPP_MESSAGE,
            execute_at_utc=execute_at,
            timezone_name=user_timezone,
            payload={
                "chat_id": ctx.recipient_phone,
                "message": ctx.message,
            },
        )

        # Clear the flow state.
        await self._state_repo.delete(ctx.user_id)

        # Confirm to the user.
        name = ctx.recipient_name or "the contact"
        local_time = execute_at.astimezone(_tz(user_timezone))
        time_str = local_time.strftime("%d/%m %H:%M")
        await self._bot.send_text(
            user_phone,
            f"✅ מתוזמן ל{name} ב-{time_str}:\n"
            f'"{ctx.message}"',
        )

    async def _cancel_flow(
        self,
        ctx: SchedulingFlowContext,
        user_phone: str,
    ) -> None:
        """Abort the current flow and reset to IDLE."""
        await self._state_repo.delete(ctx.user_id)
        await self._bot.send_text(user_phone, "בוטל. ❌")


def _is_cancel(text: str | None) -> bool:
    if not text:
        return False
    return text.strip().lower() in _CANCEL_KEYWORDS


def _phone_to_chat_id(phone: str) -> str:
    """Convert a phone number to Green API chat_id format."""
    cleaned = (
        phone.replace("+", "")
        .replace(" ", "")
        .replace("-", "")
        .strip()
    )
    if "@" not in cleaned:
        cleaned = f"{cleaned}@c.us"
    return cleaned


def _tz(name: str):
    from zoneinfo import ZoneInfo
    return ZoneInfo(name)
