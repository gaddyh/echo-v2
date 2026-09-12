"""FeedbackHandler — handles bot callbacks for the feedback flyloop.

3-button card UX — the user answers one question:
    "מה אני רוצה לעשות עם זה?"

Card (3 buttons):
    טופל        → resolve + CORRECT feedback (identification was right, user handled it)
    להזכיר לי   → snooze (remind me later)
    לא צריד    → opens dismiss submenu

Dismiss submenu (2 buttons):
    לא מחכים לי       → resolve + FALSE_POSITIVE feedback (Echo was wrong)
    לא מעניין (שיחכו) → resolve (no feedback, user chooses not to handle)

Feedback is implicit — a side effect of the action, not a separate question.
The user doesn't think in terms of "feedback" vs "action"; they think:
now, later, or don't need this.

Callback ID format (opaque, uses surrogate IDs):

- Card buttons:    ``action:{active_id}:{action_type}``
  where action_type is: handled, snooze, dismiss
- Dismiss submenu: ``dismiss:{active_id}:{reason}``
  where reason is: not_waiting, not_interested

The ``active_id`` is the surrogate UUID of the ``waiting_for_me_active``
row. No chat_id or phone is exposed in callback IDs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from langsmith import traceable

from echo_v2.domain.feedback import HandlingOutcome
from echo_v2.observability.sanitizers import (
    safe_feedback_handle_inputs,
    safe_feedback_handle_output,
)
from echo_v2.persistence.chat_repositories import (
    ChatStateRepository,
    MessageRepository,
    WaitingForMeActiveRepository,
    WaitingForMeResultRepository,
)
from echo_v2.persistence.contacts import ContactRepository
from echo_v2.persistence.feedback_repositories import (
    ChatMuteRepository,
)
from echo_v2.ports.bot import BotChannel, BotEvent, BotEventType
from echo_v2.services.feedback_service import (
    WaitingForMeActionService,
    WaitingForMeFeedbackService,
)

__all__ = ["FeedbackHandler"]

_logger = logging.getLogger("echo_v2.services.feedback_handler")

# Template button text (Hebrew).
_VIEW_DETAILS_BUTTON = "צפה בשיחות"

# Text the user sends after finishing the waiting-list review (prefilled
# in the mini app's back-to-WhatsApp link). Recognized before the
# scheduling flow so the bot replies nicely instead of "send a contact".
_LIST_DONE_TEXT = "סיימתי לעבור על רשימת ההמתנה"
_LIST_DONE_REPLY = "👍 תודה שטיפלת ברשימה! אם תצטרך, פשוט שלח סיכום חדש."

# Card buttons (3).
_BUTTON_HANDLED = "טופל"
_BUTTON_SNOOZE = "להזכיר לי"
_BUTTON_DISMISS = "לא צריד"

# Dismiss submenu buttons (2).
_BUTTON_NOT_WAITING = "לא מחכים לי"
_BUTTON_NOT_INTERESTED = "לא מעניין (שיחכו)"

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
        action_service: The :class:`WaitingForMeActionService` for actions.
        feedback_service: The :class:`WaitingForMeFeedbackService` for feedback.
        active_repo: The :class:`WaitingForMeActiveRepository`.
        result_repo: The :class:`WaitingForMeResultRepository`.
        chat_state_repo: The :class:`ChatStateRepository` for version checks.
        message_repo: The :class:`MessageRepository` for latest inbound text.
        contact_repo: The :class:`ContactRepository` for name resolution.
        mute_repo: The :class:`ChatMuteRepository` for mute checks.
        user_resolver: Callable that maps phone → (user_id, status, first_name).
    """

    def __init__(
        self,
        *,
        bot: BotChannel,
        action_service: WaitingForMeActionService,
        feedback_service: WaitingForMeFeedbackService,
        active_repo: WaitingForMeActiveRepository,
        result_repo: WaitingForMeResultRepository,
        chat_state_repo: ChatStateRepository,
        message_repo: MessageRepository,
        contact_repo: ContactRepository,
        mute_repo: ChatMuteRepository,
        user_resolver,
        token_service=None,
        base_url: str = "",
    ) -> None:
        self._bot = bot
        self._action_service = action_service
        self._feedback_service = feedback_service
        self._active_repo = active_repo
        self._result_repo = result_repo
        self._chat_state_repo = chat_state_repo
        self._message_repo = message_repo
        self._contact_repo = contact_repo
        self._mute_repo = mute_repo
        self._user_resolver = user_resolver
        self._token_service = token_service
        self._base_url = base_url

    @traceable(
        name="wfm.feedback.handle",
        process_inputs=safe_feedback_handle_inputs,
        process_outputs=safe_feedback_handle_output,
    )
    async def handle(self, event: BotEvent) -> bool:
        """Check if this is a feedback-related event and handle it.

        Returns ``True`` if handled, ``False`` if the event should be
        passed to the next handler.
        """
        # 0. User finished the waiting-list review (prefilled text from
        # the mini app's back-to-WhatsApp link). Reply nicely and stop.
        if (
            event.type is BotEventType.TEXT
            and event.text
            and _LIST_DONE_TEXT in event.text
        ):
            await self._bot.send_text(event.user_phone, _LIST_DONE_REPLY)
            return True

        # 0b. User requests a new digest ("סיכום חדש" / "סיכום חדש בבקשה").
        if (
            event.type is BotEventType.TEXT
            and event.text
            and "סיכום חדש" in event.text
            and self._token_service is not None
        ):
            return await self._handle_digest_request(event)

        # 1. Template button tap: "צפה בשיחות" (with or without brackets)
        if (
            event.type is BotEventType.TEXT
            and event.text
            and event.text.strip().strip("[]") == _VIEW_DETAILS_BUTTON
        ):
            return await self._handle_view_details(event)

        # 2. Button replies — route by prefix.
        if event.type is BotEventType.BUTTON_REPLY and event.button_id:
            if event.button_id.startswith("action:"):
                return await self._handle_action(event)
            if event.button_id.startswith("dismiss:"):
                return await self._handle_dismiss(event)

        return False

    async def _handle_view_details(self, event: BotEvent) -> bool:
        """Send up to 5 individual cards, each with 3 buttons."""
        user_info = await self._user_resolver.resolve(event.user_phone)
        if user_info is None:
            return False

        user_id = user_info[0]
        now = datetime.now(timezone.utc)

        # Get current, non-snoozed, non-muted, non-acknowledged active items.
        all_active = await self._active_repo.list_all_for_user(user_id=user_id)
        items = []
        for active in all_active:
            chat = await self._chat_state_repo.get(user_id, active.chat_id)
            if chat is None or chat.activity_version != active.target_version:
                continue
            if active.acknowledged_at is not None:
                continue  # acknowledged items are excluded
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
                {"id": f"action:{active.id}:handled", "title": _BUTTON_HANDLED},
                {"id": f"action:{active.id}:snooze", "title": _BUTTON_SNOOZE},
                {"id": f"action:{active.id}:dismiss", "title": _BUTTON_DISMISS},
            ]

            await self._bot.send_buttons(
                event.user_phone,
                body_text=body,
                buttons=buttons,
            )

        _logger.info("feedback: sent %d cards to %s", total, user_id)
        return True

    async def _handle_digest_request(self, event: BotEvent) -> bool:
        """Handle 'סיכום חדש' — send a regular text with the waiting-list link.

        Since the user just messaged us, we're in the 24-hour window and
        can send a free-text message (no template needed).
        """
        user_info = await self._user_resolver.resolve(event.user_phone)
        if user_info is None:
            return False

        user_id = user_info[0]
        now = datetime.now(timezone.utc)

        # Count active, non-snoozed, non-muted items.
        all_active = await self._active_repo.list_all_for_user(user_id=user_id)
        count = 0
        for active in all_active:
            chat = await self._chat_state_repo.get(user_id, active.chat_id)
            if chat is None or chat.activity_version != active.target_version:
                continue
            if active.acknowledged_at is not None:
                continue
            if active.snoozed_until is not None and active.snoozed_until > now:
                continue
            if await self._mute_repo.is_muted(
                user_id=user_id, chat_id=active.chat_id, now=now
            ):
                continue
            count += 1

        if count == 0:
            await self._bot.send_text(
                event.user_phone,
                "אין כרגע שיחות שמחכות לטיפול. 👍",
            )
            return True

        # Issue a waiting-list token and build the link.
        try:
            _session_id, raw_token = await self._token_service.issue(user_id)
        except Exception:
            _logger.exception("failed to issue token for user %s", user_id)
            await self._bot.send_text(
                event.user_phone,
                "אירעה שגיאה. נסה שוב.",
            )
            return True

        link = f"{self._base_url}/q/{raw_token}"
        text = f"יש לך {count} שיחות שמחכות לטיפול.\n{link}"
        await self._bot.send_text(event.user_phone, text)
        return True

    async def _handle_action(self, event: BotEvent) -> bool:
        """Execute an action (handled/snooze/dismiss)."""
        user_info = await self._user_resolver.resolve(event.user_phone)
        if user_info is None:
            return False

        user_id = user_info[0]
        # Parse: action:{active_id}:{action_type}
        parts = event.button_id.split(":", 2)
        if len(parts) < 3:
            return False
        active_id = parts[1]
        action_type = parts[2]

        # Get the target_version from the active item.
        target_version = await self._get_version_for_active(active_id)

        if action_type == "handled":
            outcome = await self._action_service.handled(
                user_id=user_id,
                active_id=active_id,
                target_version=target_version,
                provider_message_id=event.event_id,
            )
            await self._send_action_response(event.user_phone, outcome, "handled")

        elif action_type == "snooze" or action_type.startswith("snooze:"):
            # Parse optional preset: "snooze:1h", "snooze:tomorrow", etc.
            preset = None
            if ":" in action_type:
                preset = action_type.split(":", 1)[1]
            outcome = await self._action_service.snooze(
                user_id=user_id,
                active_id=active_id,
                target_version=target_version,
                provider_message_id=event.event_id,
                snooze_preset=preset,
            )
            await self._send_action_response(event.user_phone, outcome, "snooze")

        elif action_type == "dismiss":
            # Send the dismiss submenu.
            await self._send_dismiss_menu(event.user_phone, user_id, active_id)

        else:
            _logger.warning("feedback: unknown action type %s", action_type)
            return False

        return True

    async def _handle_dismiss(self, event: BotEvent) -> bool:
        """Handle dismiss submenu selection (not_waiting / not_interested)."""
        user_info = await self._user_resolver.resolve(event.user_phone)
        if user_info is None:
            return False

        user_id = user_info[0]
        # Parse: dismiss:{active_id}:{reason}
        parts = event.button_id.split(":", 2)
        if len(parts) < 3:
            return False
        active_id = parts[1]
        reason = parts[2]

        target_version = await self._get_version_for_active(active_id)

        if reason == "not_waiting":
            outcome = await self._action_service.dismiss_not_waiting(
                user_id=user_id,
                active_id=active_id,
                target_version=target_version,
                provider_message_id=event.event_id,
            )
            await self._send_action_response(event.user_phone, outcome, "dismiss_not_waiting")

        elif reason == "not_interested":
            outcome = await self._action_service.dismiss_not_interested(
                user_id=user_id,
                active_id=active_id,
                target_version=target_version,
                provider_message_id=event.event_id,
            )
            await self._send_action_response(event.user_phone, outcome, "dismiss_not_interested")

        else:
            _logger.warning("feedback: unknown dismiss reason %s", reason)
            return False

        return True

    async def _send_dismiss_menu(
        self,
        phone: str,
        user_id: str,
        active_id: str,
    ) -> None:
        """Send the dismiss submenu (2 buttons)."""
        body = "למה לא צריך?"

        buttons = [
            {"id": f"dismiss:{active_id}:not_waiting", "title": _BUTTON_NOT_WAITING},
            {"id": f"dismiss:{active_id}:not_interested", "title": _BUTTON_NOT_INTERESTED},
        ]

        await self._bot.send_buttons(
            phone,
            body_text=body,
            buttons=buttons,
        )

    async def _get_version_for_active(self, active_id: str) -> int:
        """Get the target_version for an active item by ID."""
        active = await self._active_repo.get_by_id(active_id)
        return active.target_version if active else 0

    async def _send_action_response(
        self,
        phone: str,
        outcome: HandlingOutcome,
        action_type: str,
    ) -> None:
        """Send the appropriate response based on the handling outcome."""
        if outcome == HandlingOutcome.APPLIED:
            if action_type == "handled":
                await self._bot.send_text(phone, "👍 סימנתי שטופל.")
            elif action_type == "snooze":
                await self._bot.send_text(phone, "⏰ אזכיר לך שוב מחר.")
            elif action_type == "dismiss_not_waiting":
                await self._bot.send_text(phone, "תודה על המשוב! אני אלמד מזה.")
            elif action_type == "dismiss_not_interested":
                await self._bot.send_text(phone, "✅ הוסר מהרשימה.")
        elif outcome == HandlingOutcome.DUPLICATE:
            # Already processed — don't send a duplicate message.
            _logger.info("action: duplicate callback for %s, skipping", action_type)
        else:
            # STALE or NOT_FOUND
            await self._bot.send_text(phone, _STALE_MESSAGE)

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
    """Extract phone number from a WhatsApp chat ID.

    ``972501234567@c.us`` → ``972501234567``
    ``group-123@g.us`` → ``group-123``
    """
    return chat_id.split("@")[0] if "@" in chat_id else chat_id
