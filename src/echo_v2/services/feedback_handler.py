"""FeedbackHandler — handles bot callbacks for the feedback flyloop.

This is the bot-side handler that:

1. Recognizes the "צפה בפרטים" template button tap → sends an interactive
   list of current waiting items (max 10).
2. Recognizes a list item selection → sends a 3-button feedback message
   for that item.
3. Recognizes a feedback button tap → records the action + optionally
   asks for feedback on correctness.
4. Recognizes "פספסתי" → starts the miss-reporting flow.

Callback ID format (structured, opaque to the user):

- Template button: text = "צפה בפרטים"
- List item:        ``wfm_item:{chat_id}:{target_version}``
- Feedback button:  ``wfm_feedback:{action}:{chat_id}:{target_version}``
- Mute confirm:     ``wfm_mute:{chat_id}``
- Mute decline:      ``wfm_nomute:{chat_id}``
- Feedback on resolve: ``wfm_verdict:{verdict}:{chat_id}:{target_version}``
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

# Feedback buttons (Hebrew, max 20 chars each).
_BUTTON_ACKNOWLEDGE = "מטפל עכשיו"
_BUTTON_SNOOZE = "הזכר לי מחר"
_BUTTON_RESOLVE = "הסר מהרשימה"

# Post-resolve feedback buttons.
_BUTTON_FALSE_POSITIVE = "כן, זו טעות"
_BUTTON_CORRECT = "לא, פשוט סיימתי"

# Mute confirmation buttons.
_BUTTON_MUTE_PERMANENT = "כן, השתק"
_BUTTON_NO_MUTE = "לא, זה חד-פעמי"

# Miss reporting.
_MISS_COMMAND = "פספסתי"

# Max items in the interactive list.
MAX_LIST_ITEMS = 10


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
        # 1. Template button tap: "צפה בפרטים"
        if (
            event.type is BotEventType.TEXT
            and event.text
            and event.text.strip() == _VIEW_DETAILS_BUTTON
        ):
            return await self._handle_view_details(event)

        # 2. List item selection: wfm_item:{chat_id}:{version}
        if (
            event.type is BotEventType.LIST_REPLY
            and event.list_id
            and event.list_id.startswith("wfm_item:")
        ):
            return await self._handle_list_item(event)

        # 3. Feedback button: wfm_feedback:{action}:{chat_id}:{version}
        if event.type is BotEventType.BUTTON_REPLY and event.button_id:
            if event.button_id.startswith("wfm_feedback:"):
                return await self._handle_feedback_button(event)
            if event.button_id.startswith("wfm_verdict:"):
                return await self._handle_verdict_button(event)
            if event.button_id.startswith("wfm_mute:"):
                return await self._handle_mute_confirm(event)
            if event.button_id.startswith("wfm_nomute:"):
                return True  # User declined mute — no action needed.

        # 4. Miss reporting: "פספסתי"
        if (
            event.type is BotEventType.TEXT
            and event.text
            and event.text.strip() == _MISS_COMMAND
        ):
            return await self._handle_miss_report(event)

        return False

    async def _handle_view_details(self, event: BotEvent) -> bool:
        """Send an interactive list of current waiting items."""
        user_info = await self._user_resolver.resolve(event.user_phone)
        if user_info is None:
            return False

        user_id = user_info[0]
        now = datetime.now(timezone.utc)

        # Get current, non-snoozed, non-muted active items.
        all_active = await self._active_repo.list_all_for_user(user_id=user_id)
        items = []
        for active in all_active:
            # Version check.
            chat = await self._chat_state_repo.get(user_id, active.chat_id)
            if chat is None or chat.activity_version != active.target_version:
                continue
            # Snooze check.
            if active.snoozed_until is not None and active.snoozed_until > now:
                continue
            # Mute check.
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

        # Sort by waiting_since ascending (oldest first), max 10.
        items.sort(key=lambda a: a.waiting_since)
        items = items[:MAX_LIST_ITEMS]

        # Build list rows.
        rows = []
        for active in items:
            name = await self._resolve_name(user_id, active.chat_id)
            last_text = await self._get_last_text(user_id, active.chat_id)
            row_id = f"wfm_item:{active.chat_id}:{active.target_version}"
            title = name or _phone_from_chat_id(active.chat_id)
            description = (last_text or "שלח/ה הודעה")[:72]
            rows.append({
                "id": row_id,
                "title": title,
                "description": description,
            })

        sections = [{"title": "שיחות שמחכות לך", "rows": rows}]
        body = f"יש לך {len(items)} שיחות שמחכות לתגובה שלך:"

        await self._bot.send_interactive_list(
            event.user_phone,
            body_text=body,
            button_text="בחר שיחה",
            sections=sections,
        )
        _logger.info("feedback: sent list to %s with %d items", user_id, len(items))
        return True

    async def _handle_list_item(self, event: BotEvent) -> bool:
        """Send a 3-button feedback message for the selected item."""
        user_info = await self._user_resolver.resolve(event.user_phone)
        if user_info is None:
            return False

        user_id = user_info[0]
        # Parse: wfm_item:{chat_id}:{target_version}
        parts = event.list_id.split(":", 2)
        if len(parts) < 3:
            return False
        chat_id = parts[1]
        try:
            target_version = int(parts[2])
        except ValueError:
            return False

        # Validate the active item exists and is current.
        active = await self._active_repo.get(user_id=user_id, chat_id=chat_id)
        if active is None or active.target_version != target_version:
            await self._bot.send_text(
                event.user_phone,
                "הפריט הזה כבר אינו עדכני.",
            )
            return True

        # Build the feedback message.
        name = await self._resolve_name(user_id, chat_id)
        last_text = await self._get_last_text(user_id, chat_id)
        display_name = name or _phone_from_chat_id(chat_id)
        body = f'{display_name}: "{last_text or "שלח/ה הודעה"}"'

        buttons = [
            {"id": f"wfm_feedback:acknowledge:{chat_id}:{target_version}", "title": _BUTTON_ACKNOWLEDGE},
            {"id": f"wfm_feedback:snooze:{chat_id}:{target_version}", "title": _BUTTON_SNOOZE},
            {"id": f"wfm_feedback:resolve:{chat_id}:{target_version}", "title": _BUTTON_RESOLVE},
        ]

        await self._bot.send_buttons(
            event.user_phone,
            body_text=body,
            buttons=buttons,
        )
        return True

    async def _handle_feedback_button(self, event: BotEvent) -> bool:
        """Handle a feedback button tap — record action + update state."""
        user_info = await self._user_resolver.resolve(event.user_phone)
        if user_info is None:
            return False

        user_id = user_info[0]
        # Parse: wfm_feedback:{action}:{chat_id}:{target_version}
        parts = event.button_id.split(":", 4)
        if len(parts) < 4:
            return False
        action_str = parts[1]
        chat_id = parts[2]
        try:
            target_version = int(parts[3])
        except ValueError:
            return False

        active_id = f"{user_id}:{chat_id}"

        if action_str == "acknowledge":
            await self._feedback_service.handle_acknowledge(
                user_id=user_id,
                chat_id=chat_id,
                active_id=active_id,
                target_version=target_version,
                provider_event_id=event.event_id,
            )
            await self._bot.send_text(event.user_phone, "👍 סימנתי כמטופל.")

        elif action_str == "snooze":
            await self._feedback_service.handle_snooze(
                user_id=user_id,
                chat_id=chat_id,
                active_id=active_id,
                target_version=target_version,
                provider_event_id=event.event_id,
            )
            await self._bot.send_text(event.user_phone, "⏰ אזכיר לך שוב מחר.")

        elif action_str == "resolve":
            await self._feedback_service.handle_resolve(
                user_id=user_id,
                chat_id=chat_id,
                active_id=active_id,
                target_version=target_version,
                provider_event_id=event.event_id,
            )
            # Ask if Echo was wrong.
            body = "האם Echo טעה כשסימן שהשיחה מחכה לך?"
            buttons = [
                {"id": f"wfm_verdict:false_positive:{chat_id}:{target_version}", "title": _BUTTON_FALSE_POSITIVE},
                {"id": f"wfm_verdict:correct:{chat_id}:{target_version}", "title": _BUTTON_CORRECT},
            ]
            await self._bot.send_buttons(
                event.user_phone,
                body_text=body,
                buttons=buttons,
            )

        else:
            _logger.warning("feedback: unknown action %s", action_str)
            return False

        return True

    async def _handle_verdict_button(self, event: BotEvent) -> bool:
        """Handle the post-resolve verdict (was Echo correct?)."""
        user_info = await self._user_resolver.resolve(event.user_phone)
        if user_info is None:
            return False

        user_id = user_info[0]
        # Parse: wfm_verdict:{verdict}:{chat_id}:{target_version}
        parts = event.button_id.split(":", 4)
        if len(parts) < 4:
            return False
        verdict_str = parts[1]
        chat_id = parts[2]
        try:
            target_version = int(parts[3])
        except ValueError:
            return False

        if verdict_str == "false_positive":
            await self._feedback_service.record_feedback(
                user_id=user_id,
                chat_id=chat_id,
                verdict=FeedbackVerdict.FALSE_POSITIVE,
                target_version=target_version,
                provider_event_id=event.event_id,
            )
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
                await self._bot.send_text(event.user_phone, "תודה על המשוב! אני אלמד מזה.")
        elif verdict_str == "correct":
            await self._feedback_service.record_feedback(
                user_id=user_id,
                chat_id=chat_id,
                verdict=FeedbackVerdict.CORRECT,
                target_version=target_version,
                provider_event_id=event.event_id,
            )
            await self._bot.send_text(event.user_phone, "👍 תודה על המשוב!")

        return True

    async def _handle_mute_confirm(self, event: BotEvent) -> bool:
        """Handle permanent mute confirmation."""
        user_info = await self._user_resolver.resolve(event.user_phone)
        if user_info is None:
            return False

        user_id = user_info[0]
        # Parse: wfm_mute:{chat_id}
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
            chat_id="*",  # No specific chat yet — user needs to specify.
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


def _phone_from_chat_id(chat_id: str) -> str:
    """Extract phone number from a WhatsApp chat ID."""
    return chat_id.split("@")[0] if "@" in chat_id else chat_id
