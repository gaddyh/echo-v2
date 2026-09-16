"""Canonical read model for waiting-for-me items.

The :class:`WaitingForMeView` is the single shape consumed by every surface
that lists actionable waiting items:

* the waiting-list mini web app (``WaitingListActionService.list_items``)
* the morning digest (``DigestWorker``)
* the WhatsApp bot cards (``FeedbackHandler._handle_view_details``)

No surface resolves ``chat_name → contact.display_name → message.sender_name
→ phone`` itself. That cascade lives once in :class:`ContactNameResolver`.

``_phone_from_chat_id`` is defined here and re-exported; the duplicated
copies that previously lived in ``waiting_list_service.py``,
``digest_worker.py``, and ``feedback_handler.py`` have been removed in
favour of this single helper.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from echo_v2.domain.chat import Message
from echo_v2.persistence.chat_repositories import (
    ChatStateRepository,
    MessageRepository,
)
from echo_v2.persistence.contacts import ContactRecord, ContactRepository

__all__ = [
    "ContactNameResolver",
    "WaitingForMeView",
    "phone_from_chat_id",
]

# Max preview length for customer messages (truncated server-side).
MAX_PREVIEW_CHARS = 120


@dataclass(frozen=True)
class WaitingForMeView:
    """One actionable waiting item, fully resolved for display.

    Attributes:
        id: The surrogate UUID of the ``waiting_for_me_active`` row. Used
            as the item handle in action requests and callback ids.
        owner: The user_id who owns this waiting item.
        contact_name: Resolved contact name, or ``None`` if unknown.
        summary: One-sentence Hebrew summary from the LLM analysis.
            ``None`` if the analysis predates summaries or the result is
            missing.
        last_message: Truncated last inbound message text (max 120 chars
            plus a trailing ``"…"`` when truncated), or ``None`` if
            media-only or no text.
        waiting_since: When the waiting state originally started.
        version: The ``target_version`` the client should send back in
            action requests for concurrency control.
        is_starred: Whether the contact (by phone) is starred. Used as
            an optional sort signal (starred contacts first).
    """

    id: str
    owner: str
    contact_name: str | None
    summary: str | None
    last_message: str | None
    waiting_since: datetime
    version: int
    is_starred: bool = False


def phone_from_chat_id(chat_id: str) -> str:
    """Extract the phone number from a WhatsApp chat ID.

    ``972501234567@c.us`` → ``972501234567``
    ``group-123@g.us`` → ``group-123``
    """
    return chat_id.split("@")[0] if "@" in chat_id else chat_id


def truncate_preview(text: str | None) -> str | None:
    """Truncate a message text to :data:`MAX_PREVIEW_CHARS` + ``"…"``."""
    if not text:
        return None
    if len(text) <= MAX_PREVIEW_CHARS:
        return text
    return text[:MAX_PREVIEW_CHARS] + "…"


class ContactNameResolver:
    """Resolve a contact display name for a chat using a single cascade.

    Resolution priority:

    1. ``chats.chat_name`` (from Green API ``senderData.chatName``)
    2. ``contacts.display_name`` (from the contacts table)
    3. ``message.chat_name`` or ``message.sender_name`` (from the latest
       inbound message)
    4. ``None`` (caller decides whether to fall back to the phone)

    The resolver accepts pre-fetched data (``contact`` and ``msg``) so the
    view builder can batch-fetch contact metadata once and avoid
    per-item ``find_by_phone`` calls.
    """

    def __init__(
        self,
        *,
        chat_state_repo: ChatStateRepository,
        message_repo: MessageRepository,
        contact_repo: ContactRepository,
    ) -> None:
        self._chat_state_repo = chat_state_repo
        self._message_repo = message_repo
        self._contact_repo = contact_repo

    async def resolve_name(
        self,
        user_id: str,
        chat_id: str,
        *,
        contact: ContactRecord | None = None,
        msg: Message | None = None,
    ) -> str | None:
        """Resolve the contact name for a chat.

        Args:
            contact: Optional pre-fetched :class:`ContactRecord` for the
                chat's phone. When provided, avoids a ``find_by_phone``
                call.
            msg: Optional pre-fetched latest inbound message. When
                provided, avoids a ``get_latest_inbound`` call. Must
                expose ``chat_name`` and ``sender_name`` attributes.
        """
        chat = await self._chat_state_repo.get(user_id, chat_id)
        chat_name = chat.chat_name if chat else None

        if not chat_name:
            if contact is None:
                phone = phone_from_chat_id(chat_id)
                contact = await self._contact_repo.find_by_phone(user_id, phone)
            chat_name = contact.display_name if contact else None

        if not chat_name and msg is None:
            msg = await self._message_repo.get_latest_inbound(
                user_id=user_id, chat_id=chat_id
            )
        if not chat_name and msg is not None:
            chat_name = msg.chat_name or msg.sender_name

        return chat_name

    async def get_latest_inbound(
        self, user_id: str, chat_id: str
    ) -> Message | None:
        """Return the latest inbound message for a chat, or ``None``."""
        return await self._message_repo.get_latest_inbound(
            user_id=user_id, chat_id=chat_id
        )
