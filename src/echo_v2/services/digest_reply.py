"""DigestReplyService — handles the "הצג הכול" button reply.

When the user taps the Quick Reply button on the morning digest
template, WhatsApp sends a button reply with text "הצג הכול". This
service:

1. Recognizes the button text.
2. Resolves the user from the sender's phone.
3. Queries current active waiting chats (version-matched).
4. Builds the full free-form text (all items, no truncation).
5. Sends it via ``bot.send_text()`` — this is now inside the 24-hour
   service window because the user's button reply opened it.

The service is designed as a "pre-handler" in the webhook: it runs
before the :class:`SchedulingFlowService`. If it handles the event
(returns ``True``), the webhook skips the flow service. If it doesn't
recognize the text (returns ``False``), the webhook dispatches to the
flow service as before.
"""

from __future__ import annotations

import logging

from echo_v2.persistence.chat_repositories import (
    ChatStateRepository,
    MessageRepository,
    WaitingForMeActiveRepository,
)
from echo_v2.persistence.contacts import ContactRepository
from echo_v2.ports.bot import BotChannel, BotEvent
from echo_v2.services.digest_formatter import DigestItem

__all__ = ["DigestReplyService"]

_logger = logging.getLogger("echo_v2.services.digest_reply")

# The Quick Reply button text on the morning_waiting_digest template.
_SHOW_ALL_BUTTON = "הצג הכול"


class DigestReplyService:
    """Handle the "הצג הכול" button reply from the digest template.

    Args:
        bot: The :class:`BotChannel` to send the full list through.
        active_repo: The :class:`WaitingForMeActiveRepository`.
        chat_state_repo: The :class:`ChatStateRepository` for version checks.
        message_repo: The :class:`MessageRepository` for latest inbound text.
        contact_repo: The :class:`ContactRepository` for name resolution.
        user_resolver: Callable that maps phone → user_id or ``None``.
    """

    def __init__(
        self,
        *,
        bot: BotChannel,
        active_repo: WaitingForMeActiveRepository,
        chat_state_repo: ChatStateRepository,
        message_repo: MessageRepository,
        contact_repo: ContactRepository,
        user_resolver,
    ) -> None:
        self._bot = bot
        self._active_repo = active_repo
        self._chat_state_repo = chat_state_repo
        self._message_repo = message_repo
        self._contact_repo = contact_repo
        self._user_resolver = user_resolver

    async def handle(self, event: BotEvent) -> bool:
        """Check if this is a digest reply and handle it.

        Returns ``True`` if the event was handled (was the button reply),
        ``False`` if the event should be passed to the next handler.
        """
        if event.text is None:
            return False
        if event.text.strip() != _SHOW_ALL_BUTTON:
            return False

        # Resolve the user.
        user_info = await self._user_resolver.resolve(event.user_phone)
        if user_info is None:
            await self._bot.send_text(
                event.user_phone,
                "I don't know you yet. Please connect your WhatsApp first.",
            )
            return True
        user_id = user_info[0]

        # Query current active waiting chats with version match.
        active_states = await self._get_current_active(user_id)

        if not active_states:
            await self._bot.send_text(
                event.user_phone,
                "אין כרגע שיחות שמחכות לטיפול. 👍",
            )
            return True

        # Build digest items (no truncation — full list).
        items = await self._build_items(user_id, active_states)
        text = self._format_full_list(items)

        await self._bot.send_text(event.user_phone, text)
        _logger.info(
            "digest reply sent to user %s: %d items", user_id, len(active_states)
        )
        return True

    async def _get_current_active(self, user_id: str):
        """Get active states where target_version matches chats.activity_version."""
        from echo_v2.domain.waiting_for_me import WaitingForMeActive

        all_active = await self._active_repo.list_all_for_user(user_id=user_id)
        current: list[WaitingForMeActive] = []
        for active in all_active:
            chat = await self._chat_state_repo.get(user_id, active.chat_id)
            if chat is not None and chat.activity_version == active.target_version:
                current.append(active)
        return current

    async def _build_items(self, user_id: str, active_states) -> list[DigestItem]:
        """Build DigestItems with contact names and last message text.

        Name resolution priority:
        1. chats.chat_name (from Green API senderData.chatName)
        2. contacts.display_name (from contacts table)
        3. message.chat_name or sender_name (from the latest message)
        4. phone number from chat_id (fallback)
        """
        items: list[DigestItem] = []
        for active in active_states:
            phone = _phone_from_chat_id(active.chat_id)

            # Try chat_name from chat state first.
            chat = await self._chat_state_repo.get(user_id, active.chat_id)
            chat_name = chat.chat_name if chat else None

            # Fall back to contact lookup.
            if not chat_name:
                contact = await self._contact_repo.find_by_phone(user_id, phone)
                chat_name = contact.display_name if contact else None

            msg = await self._message_repo.get_latest_inbound(
                user_id=user_id,
                chat_id=active.chat_id,
            )
            last_text = msg.text if msg and msg.text else None

            # Fall back to message-level names.
            if not chat_name and msg:
                chat_name = msg.chat_name or msg.sender_name

            items.append(DigestItem(
                active=active,
                contact_name=chat_name,
                last_message_text=last_text,
            ))
        return items

    @staticmethod
    def _format_full_list(items: list[DigestItem]) -> str:
        """Format the full list as free-form text (no truncation).

        Unlike the template digest which is limited to 20 items, the
        full list includes everything. Sorted by waiting_since ascending.
        """
        sorted_items = sorted(items, key=lambda i: i.active.waiting_since)
        lines: list[str] = ["📋 הרשימה המלאה:", ""]

        for item in sorted_items:
            name = item.contact_name or _phone_from_chat_id(item.active.chat_id)
            if item.last_message_text:
                lines.append(f'{name}: "{item.last_message_text}"')
            else:
                lines.append(f'{name}: "שלח/ה הודעה שמחכה להתייחסותך"')

        lines.append("")
        lines.append('כדי לטפל, פשוט חזור לשיחה הרלוונטית. ✌️')

        return "\n".join(lines)


def _phone_from_chat_id(chat_id: str) -> str:
    """Extract phone number from a WhatsApp chat ID."""
    return chat_id.split("@")[0] if "@" in chat_id else chat_id
