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
    """

    event_id: str
    user_phone: str
    type: BotEventType
    text: str | None = None
    contact: BotContact | None = None
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
