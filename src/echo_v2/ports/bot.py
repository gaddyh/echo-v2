"""Provider-neutral Echo Business Bot ports and types.

The Echo Business Bot is the conversational interface between the user and
Echo. It is a **separate channel** from the user's own WhatsApp (Green API):

* **Bot** (360dialog / Meta Cloud API) — the user chats with Echo here:
  sends commands, vCards, time replies; receives confirmations.
* **User's WhatsApp** (Green API) — Echo acts on the user's behalf here:
  sends scheduled messages from the user's own number.

This module defines the boundary between Echo's application code and any bot
transport (360dialog today, maybe Telegram later). Provider-specific
identifiers (D360 API keys, message IDs) never cross this boundary.

The event model mirrors the two-layer pattern from ``ports/whatsapp.py``:
bot events carry a ``user_phone`` (the sender's phone number), not a
``user_id`` — the application layer resolves the phone to a user.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

__all__ = [
    "BotChannel",
    "BotContact",
    "BotEvent",
    "BotEventAdapter",
    "BotEventType",
]


class BotEventType(Enum):
    """Type of incoming bot event."""

    TEXT = "text"
    CONTACT = "contact"
    BUTTON_REPLY = "button_reply"
    LIST_REPLY = "list_reply"


@dataclass(frozen=True)
class BotContact:
    """A shared contact (vCard) extracted from a bot message.

    ``phone`` is the contact's phone number in E.164 or raw form — the
    scheduling flow converts it to a Green API ``chat_id`` (``<phone>@c.us``).
    ``name`` is the display name for confirmation messages.
    """

    phone: str
    name: str


@dataclass(frozen=True)
class BotEvent:
    """An incoming event from the Echo Business Bot.

    ``user_phone`` is the sender's phone number (the Echo user), not the
    contact's phone. The application resolves ``user_phone`` → ``user_id``
    via the users table.

    For ``TEXT`` events, ``text`` carries the message body.
    For ``CONTACT`` events, ``contact`` carries the parsed vCard data.
    For ``BUTTON_REPLY`` events, ``button_id`` carries the structured
        callback ID (e.g. ``wfm_feedback:{active_id}:{version}:now``)
        and ``text`` carries the button's display text.
    For ``LIST_REPLY`` events, ``list_id`` carries the structured
        callback ID (e.g. ``wfm_item:{active_id}:{version}``) and ``text``
        carries the row's display text.
    """

    event_id: str
    user_phone: str
    type: BotEventType
    text: str | None = None
    contact: BotContact | None = None
    button_id: str | None = None
    list_id: str | None = None
    timestamp: datetime | None = None


@runtime_checkable
class BotEventAdapter(Protocol):
    """Parse a raw provider webhook payload into a :class:`BotEvent`.

    Pure and synchronous: no I/O, no user lookup. Returns ``None`` for
    payloads the adapter deliberately ignores (status updates, non-message
    webhooks, unknown types). Never raises on unknown shapes — returns
    ``None`` so an unexpected payload never breaks the webhook ingress.
    """

    def parse(self, payload: dict) -> BotEvent | None: ...


@runtime_checkable
class BotChannel(Protocol):
    """Send outbound messages to a user via the Echo Business Bot.

    ``user_phone`` is the recipient's phone number (the Echo user). The
    implementation normalizes it to the provider's required format.
    """

    async def send_text(self, user_phone: str, text: str) -> None: ...

    async def send_template(
        self,
        user_phone: str,
        template_name: str,
        language: str,
        body_params: list[str],
    ) -> str:
        """Send a template message. Returns the provider message ID."""
        ...

    async def send_interactive_list(
        self,
        user_phone: str,
        *,
        body_text: str,
        button_text: str,
        sections: list[dict],
    ) -> str:
        """Send an interactive list message. Returns the provider message ID.

        Args:
            user_phone: The recipient's phone number.
            body_text: The message body text (above the list button).
            button_text: The text on the list button (max 20 chars).
            sections: A list of section dicts, each with ``title`` and
                ``rows``. Each row is a dict with ``id``, ``title``, and
                optional ``description``. Max 10 rows total.
        """
        ...

    async def send_buttons(
        self,
        user_phone: str,
        *,
        body_text: str,
        buttons: list[dict],
    ) -> str:
        """Send an interactive button message. Returns the provider message ID.

        Args:
            user_phone: The recipient's phone number.
            body_text: The message body text (above the buttons).
            buttons: A list of button dicts, each with ``id`` and ``title``.
                Max 3 buttons.
        """
        ...
