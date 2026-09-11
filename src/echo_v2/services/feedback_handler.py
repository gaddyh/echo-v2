"""FeedbackHandler — handles bot callbacks for the feedback flyloop.

This is the bot-side handler that:

1. Recognizes the "צפה בשיחות" template button tap → sends up to 5
   individual cards, each with 2 gateway buttons (מה לעשות / משוב ל־Echo).
2. Action submenu (מה לעשות) → 3 buttons: מטפל עכשיו / הזכר לי מחר / הסר.
3. Feedback submenu (משוב ל־Echo) → 3 buttons: כן / לא / לא בטוח.
4. Action callbacks validate staleness; stale → reject with message.
5. Feedback callbacks always stored (even if stale).
6. Recognizes "פספסתי" → records false_negative.

Callback ID format (structured, opaque to the user):

- Template button: text = "צפה בשיחות"
- Action menu:     ``menu_action:{chat_id}:{target_version}``
- Feedback menu:   ``menu_feedback:{chat_id}:{target_version}``
- Action:          ``action:{chat_id}:{target_version}:{action_type}``
- Feedback:        ``feedback:{chat_id}:{target_version}:{verdict}``
- Mute confirm:    ``wfm_mute:{chat_id}``
- Mute decline:     ``wfm_nomute:{chat_id}``
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from echo_v2.domain.feedback import FeedbackVerdict
from echo_v2.persistence.chat_repositories import (
    ChatStateRepository,
    MessageRepository,
    WaitingForMeActiveRepository,
)
from echo_v2.persistence.contacts import ContactRepository
from echo_v2.persistence.feedback_repositories import ChatMuteRepository
from echo_v2.ports.bot import BotChannel, BotEvent, BotEventType
from echo_v2.services.feedback_service import FeedbackService

__all__ = ["FeedbackHandler"]

_logger = logging.getLogger("echo_v2.services.feedback_handler")

# Template button text (Hebrew).
_VIEW_DETAILS_BUTTON = "צפה בשיחות"

# Card gateway buttons.
_BUTTON_ACTION_MENU = "מה לעשות"
_BUTTON_FEEDBACK_MENU = "משוב ל־Echo"

# Action submenu buttons.
_BUTTON_ACKNOWLEDGE = "מטפל עכשיו"
_BUTTON_SNOOZE = "הזכר לי מחר"
_BUTTON_RESOLVE = "הסר"

# Feedback submenu buttons.
_BUTTON_CORRECT = "כן"
_BUTTON_FALSE_POSITIVE = "לא"
_BUTTON_UNCERTAIN = "לא בטוח"

# Mute confirmation buttons.
_BUTTON_MUTE_PERMANENT = "כן, השתק"
_BUTTON_NO_MUTE = "לא, זה חד-פעמי"

# Miss reporting.
_MISS_COMMAND = "פספסתי"

# Max cards to send.
MAX_CARDS = 5

# Stale message.
_STALE_MESSAGE = "השיחה השתנתה מאז הסיכום."


class FeedbackHandler:
    """Handle bot callbacks for the feedback flyloop.

    This is a "pre-handler" in the webhook — runs before the scheduling
    flow service. Returns ``True`` if it handled the event, ``False`` if
    the event should be passed to the next handler.

    Args:
        bot: The :class:`BotChannel` to send messages through.
        feedback_service: The :class:`FeedbackService` for actions + feedback.
        active_repo: The :class:`WaitingForMeActiveRepository`.
        chat_state_repo: The :class:`ChatStateRepository` for version checks.
        message_repo: The :class:`MessageRepository` for latest inbound text.
        contact_repo: The :class:`ContactRepository` for name resolution.
        mute_repo: The :class:`ChatMuteRepository` for mute checks.
        user_resolver: Callable that maps phone → user_id or ``None``.
    """

    def __init__(
        self,
        *,
        bot: BotChannel,
        feedback_service: FeedbackService,
        active_repo: WaitingForMeActiveRepository,
        chat_state_repo: ChatStateRepository,
        message_repo: MessageRepository,
        contact_repo: ContactRepository,
        mute_repo: ChatMuteRepository,
        user_resolver,
    ) -> None:
        self._bot = bot
        self._feedback_service = feedback_service
        self._active_repo = active_repo
        self._chat_state_repo = chat_state_repo
        self._message_repo = message_repo
        self._contact_repo = contact_repo
        self._mute_repo = mute_repo
        self._user_resolver = user_resolver

    async def handle(self, event: BotEvent) -> bool:
        """Check if this is a feedback-related event and handle it.

        Returns ``True`` if handled, ``False`` if the event should be
        passed to the next handler.
        """
        # 1. Template button tap: "צפה בשיחות"
        if (
            event.type is BotEventType.TEXT
            and event.text
            and event.text.strip() == _VIEW_DETAILS_BUTTON
        ):
            return await self._handle_view_details(event)

        # 2. Button replies — route by prefix.
        if event.type is BotEventType.BUTTON_REPLY and event.button_id:
            if event.button_id.startswith("menu_action:"):
                return await self._handle_action_menu(event)
            if event.button_id.startswith("menu_feedback:"):
                return await self._handle_feedback_menu(event)
            if event.button_id.startswith("action:"):
                return await self._handle_action(event)
            if event.button_id.startswith("feedback:"):
                return await self._handle_feedback(event)
            if event.button_id.startswith("wfm_mute:"):
                return await self._handle_mute_confirm(event)
            if event.button_id.startswith("wfm_nomute:"):
                return True  # User declined mute — no action needed.

        # 3. Miss reporting: "פספסתי"
        if (
            event.type is BotEventType.TEXT
            and event.text
            and event.text.strip() == _MISS_COMMAND
        ):
            return await self._handle_miss_report(event)

        return False

    async def _handle_view_details(self, event: BotEvent) -> bool:
        """Send up to 5 individual cards, each with 2 gateway buttons."""
        user_info = await self._user_resolver.resolve(event.user_phone)
        if user_info is None:
            return False

        user_id = user_info[0]
        now = datetime.now(timezone.utc)

        # Get current, non-snoozed, non-muted active items.
        all_active = await self._active_repo.list_all_for_user(user_id=user_id)
        items = []
        for active in all_active:
            chat = await self._chat_state_repo.get(user_id, active.chat_id)
            if chat is None or chat.activity_version != active.target_version:
                continue
            if active.snoozed_until is not None and active.snoozed_until > now:
                continue
            if await self._mute_repo.is_muted(
                user_id=user_id, chat_id=active.chat_id, now=now
            ):
                continue
            items.append(active)

        if not items:
            await self._bot.send_text(
                event.user_phone,
                "אין כרגע שיחות שמחכות לטיפול. 👍",
            )
            return True

        # Sort by waiting_since ascending (oldest first), max 5.
        items.sort(key=lambda a: a.waiting_since)
        items = items[:MAX_CARDS]
        total = len(items)

        for i, active in enumerate(items, 1):
            name = await self._resolve_name(user_id, active.chat_id)
            last_text = await self._get_last_text(user_id, active.chat_id)
            display_name = name or _phone_from_chat_id(active.chat_id)
            body = f"{i} מתוך {total}\n\n{display_name}\n\"{last_text or 'שלח/ה הודעה'}\""

            buttons = [
                {
                    "id": f"menu_action:{active.chat_id}:{active.target_version}",
                    "title": _BUTTON_ACTION_MENU,
                },
                {
                    "id": f"menu_feedback:{active.chat_id}:{active.target_version}",
                    "title": _BUTTON_FEEDBACK_MENU,
                },
            ]

            await self._bot.send_buttons(
                event.user_phone,
                body_text=body,
                buttons=buttons,
            )

        _logger.info("feedback: sent %d cards to %s", total, user_id)
        return True

    async def _handle_action_menu(self, event: BotEvent) -> bool:
        """Send the action submenu (3 buttons) for a specific item."""
        user_info = await self._user_resolver.resolve(event.user_phone)
        if user_info is None:
            return False

        user_id = user_info[0]
        chat_id, target_version = _parse_menu_id(event.button_id, "menu_action")
        if chat_id is None:
            return False

        # Validate the active item exists and is current.
        active = await self._active_repo.get(user_id=user_id, chat_id=chat_id)
        if active is None or active.target_version != target_version:
            await self._bot.send_text(event.user_phone, _STALE_MESSAGE)
            return True

        name = await self._resolve_name(user_id, chat_id)
        display_name = name or _phone_from_chat_id(chat_id)
        body = f"מה לעשות עם השיחה של {display_name}?"

        buttons = [
            {"id": f"action:{chat_id}:{target_version}:acknowledge", "title": _BUTTON_ACKNOWLEDGE},
            {"id": f"action:{chat_id}:{target_version}:snooze", "title": _BUTTON_SNOOZE},
            {"id": f"action:{chat_id}:{target_version}:resolve", "title": _BUTTON_RESOLVE},
        ]

        await self._bot.send_buttons(
            event.user_phone,
            body_text=body,
            buttons=buttons,
        )
        return True

    async def _handle_feedback_menu(self, event: BotEvent) -> bool:
        """Send the feedback submenu (3 buttons) for a specific item."""
        user_info = await self._user_resolver.resolve(event.user_phone)
        if user_info is None:
            return False

        chat_id, target_version = _parse_menu_id(event.button_id, "menu_feedback")
        if chat_id is None:
            return False

        # Feedback can be stored even for stale items, but we still show
        # the menu. The question is about the analysis result, not the
        # current state.
        body = "האם Echo זיהה נכון שהשיחה מחכה לך?"

        buttons = [
            {"id": f"feedback:{chat_id}:{target_version}:correct", "title": _BUTTON_CORRECT},
            {"id": f"feedback:{chat_id}:{target_version}:false_positive", "title": _BUTTON_FALSE_POSITIVE},
            {"id": f"feedback:{chat_id}:{target_version}:uncertain", "title": _BUTTON_UNCERTAIN},
        ]

        await self._bot.send_buttons(
            event.user_phone,
            body_text=body,
            buttons=buttons,
        )
        return True

    async def _handle_action(self, event: BotEvent) -> bool:
        """Execute an action (acknowledge/snooze/resolve)."""
        user_info = await self._user_resolver.resolve(event.user_phone)
        if user_info is None:
            return False

        user_id = user_info[0]
        # Parse: action:{chat_id}:{target_version}:{action_type}
        parts = event.button_id.split(":", 4)
        if len(parts) < 4:
            return False
        chat_id = parts[1]
        try:
            target_version = int(parts[2])
        except ValueError:
            return False
        action_type = parts[3]

        active_id = f"{user_id}:{chat_id}"

        if action_type == "acknowledge":
            result = await self._feedback_service.handle_acknowledge(
                user_id=user_id,
                chat_id=chat_id,
                active_id=active_id,
                target_version=target_version,
                provider_event_id=event.event_id,
            )
            if result:
                await self._bot.send_text(event.user_phone, "👍 סימנתי כמטופל.")
            else:
                await self._bot.send_text(event.user_phone, _STALE_MESSAGE)

        elif action_type == "snooze":
            result = await self._feedback_service.handle_snooze(
                user_id=user_id,
                chat_id=chat_id,
                active_id=active_id,
                target_version=target_version,
                provider_event_id=event.event_id,
            )
            if result:
                await self._bot.send_text(event.user_phone, "⏰ אזכיר לך שוב מחר.")
            else:
                await self._bot.send_text(event.user_phone, _STALE_MESSAGE)

        elif action_type == "resolve":
            result = await self._feedback_service.handle_resolve(
                user_id=user_id,
                chat_id=chat_id,
                active_id=active_id,
                target_version=target_version,
                provider_event_id=event.event_id,
            )
            if result:
                await self._bot.send_text(
                    event.user_phone, "✅ הוסר מהרשימה."
                )
            else:
                await self._bot.send_text(event.user_phone, _STALE_MESSAGE)

        else:
            _logger.warning("feedback: unknown action type %s", action_type)
            return False

        return True

    async def _handle_feedback(self, event: BotEvent) -> bool:
        """Record a feedback verdict (correct/false_positive/uncertain)."""
        user_info = await self._user_resolver.resolve(event.user_phone)
        if user_info is None:
            return False

        user_id = user_info[0]
        # Parse: feedback:{chat_id}:{target_version}:{verdict}
        parts = event.button_id.split(":", 4)
        if len(parts) < 4:
            return False
        chat_id = parts[1]
        try:
            target_version = int(parts[2])
        except ValueError:
            return False
        verdict_str = parts[3]

        verdict_map = {
            "correct": FeedbackVerdict.CORRECT,
            "false_positive": FeedbackVerdict.FALSE_POSITIVE,
            "uncertain": FeedbackVerdict.UNCERTAIN,
        }
        verdict = verdict_map.get(verdict_str)
        if verdict is None:
            _logger.warning("feedback: unknown verdict %s", verdict_str)
            return False

        # Feedback is always stored — even for stale items.
        await self._feedback_service.record_feedback(
            user_id=user_id,
            chat_id=chat_id,
            verdict=verdict,
            target_version=target_version,
            provider_event_id=event.event_id,
        )

        if verdict == FeedbackVerdict.FALSE_POSITIVE:
            # Check if we should offer permanent mute.
            if await self._feedback_service.should_offer_mute(
                user_id=user_id, chat_id=chat_id
            ):
                body = "נראה ששיחה זו לא רלוונטית שוב ושוב. להשתיק אותה לתמיד?"
                buttons = [
                    {"id": f"wfm_mute:{chat_id}", "title": _BUTTON_MUTE_PERMANENT},
                    {"id": f"wfm_nomute:{chat_id}", "title": _BUTTON_NO_MUTE},
                ]
                await self._bot.send_buttons(
                    event.user_phone,
                    body_text=body,
                    buttons=buttons,
                )
            else:
                await self._bot.send_text(
                    event.user_phone, "תודה על המשוב! אני אלמד מזה."
                )
        elif verdict == FeedbackVerdict.CORRECT:
            await self._bot.send_text(event.user_phone, "👍 תודה על המשוב!")
        else:
            await self._bot.send_text(
                event.user_phone, "תודה! אשתדל להשתפר בעתיד."
            )

        return True

    async def _handle_mute_confirm(self, event: BotEvent) -> bool:
        """Handle permanent mute confirmation."""
        user_info = await self._user_resolver.resolve(event.user_phone)
        if user_info is None:
            return False

        user_id = user_info[0]
        parts = event.button_id.split(":", 2)
        if len(parts) < 2:
            return False
        chat_id = parts[1]

        await self._feedback_service.handle_mute_chat(
            user_id=user_id,
            chat_id=chat_id,
            permanent=True,
            provider_event_id=event.event_id,
        )
        await self._bot.send_text(event.user_phone, "🔇 השיחה הושתקה. לא אציג אותה שוב.")
        return True

    async def _handle_miss_report(self, event: BotEvent) -> bool:
        """Handle 'פספסתי' — record a false negative."""
        user_info = await self._user_resolver.resolve(event.user_phone)
        if user_info is None:
            return False

        user_id = user_info[0]
        await self._feedback_service.record_feedback(
            user_id=user_id,
            chat_id="*",
            verdict=FeedbackVerdict.FALSE_NEGATIVE,
            provider_event_id=event.event_id,
        )
        await self._bot.send_text(
            event.user_phone,
            "תודה שדיווחת! אני אלמד מזה לפעם הבאה. 🙏",
        )
        _logger.info("feedback: miss report from user %s", user_id)
        return True

    async def _resolve_name(self, user_id: str, chat_id: str) -> str | None:
        """Resolve contact name for a chat."""
        phone = _phone_from_chat_id(chat_id)
        chat = await self._chat_state_repo.get(user_id, chat_id)
        if chat and chat.chat_name:
            return chat.chat_name
        contact = await self._contact_repo.find_by_phone(user_id, phone)
        if contact:
            return contact.display_name
        msg = await self._message_repo.get_latest_inbound(
            user_id=user_id, chat_id=chat_id
        )
        if msg:
            return msg.chat_name or msg.sender_name
        return None

    async def _get_last_text(self, user_id: str, chat_id: str) -> str | None:
        """Get the latest inbound message text for a chat."""
        msg = await self._message_repo.get_latest_inbound(
            user_id=user_id, chat_id=chat_id
        )
        return msg.text if msg and msg.text else None


def _parse_menu_id(button_id: str, prefix: str) -> tuple[str | None, int | None]:
    """Parse menu_action:{chat_id}:{target_version} or menu_feedback:...

    Returns (chat_id, target_version) or (None, None) if malformed.
    """
    parts = button_id.split(":", 2)
    if len(parts) < 3:
        return None, None
    chat_id = parts[1]
    try:
        target_version = int(parts[2])
    except ValueError:
        return None, None
    return chat_id, target_version


def _phone_from_chat_id(chat_id: str) -> str:
    """Extract phone number from a WhatsApp chat ID.

    ``972501234567@c.us`` → ``972501234567``
    ``group-123@g.us`` → ``group-123``
    """
    return chat_id.split("@")[0] if "@" in chat_id else chat_id
