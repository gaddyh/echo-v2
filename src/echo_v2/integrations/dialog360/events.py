"""360dialog webhook JSON → canonical :class:`BotEvent`.

The adapter is pure and synchronous: no I/O, no user lookup. It extracts
the sender's phone number and message content from the Meta Cloud API /
360dialog webhook format.

The 360dialog webhook payload structure (WhatsApp Cloud API format):

```
entry[].changes[].value.messages[]   — incoming messages
entry[].changes[].value.contacts[]   — sender metadata
entry[].changes[].value.statuses[]   — delivery status updates (ignored)
```

Supported message types:
* ``text`` → :class:`BotEventType.TEXT` with the body.
* ``contacts`` → :class:`BotEventType.CONTACT` with parsed phone + name.
* Other types → ``None`` (logged at INFO).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from echo_v2.ports.bot import BotContact, BotEvent, BotEventType

__all__ = ["Dialog360EventAdapter"]

_logger = logging.getLogger("echo_v2.dialog360.events")


class Dialog360EventAdapter:
    """Implements :class:`BotEventAdapter` for 360dialog webhooks."""

    def parse(self, payload: dict[str, Any]) -> BotEvent | None:
        if not isinstance(payload, dict):
            return None

        value = _first_change_value(payload)
        if value is None:
            return None

        messages = value.get("messages")
        if not isinstance(messages, list) or not messages:
            # Status updates and other non-message webhooks.
            return None

        message = messages[0]
        if not isinstance(message, dict):
            return None

        # Sender phone: prefer contacts[].wa_id, fall back to message.from.
        contacts = value.get("contacts") or []
        contact = contacts[0] if contacts and isinstance(contacts[0], dict) else {}
        user_phone = (
            contact.get("wa_id")
            or message.get("from")
            or ""
        )
        if not user_phone:
            return None

        msg_id = str(message.get("id", ""))
        if not msg_id:
            return None

        msg_type = message.get("type")
        timestamp = _timestamp(message.get("timestamp"))

        if msg_type == "text":
            body = message.get("text", {})
            text = body.get("body", "") if isinstance(body, dict) else ""
            if not text:
                return None
            return BotEvent(
                event_id=msg_id,
                user_phone=str(user_phone),
                type=BotEventType.TEXT,
                text=str(text),
                timestamp=timestamp,
            )

        if msg_type == "contacts":
            shared = message.get("contacts") or []
            if not shared or not isinstance(shared[0], dict):
                return None
            parsed = _parse_contact(shared[0])
            if parsed is None:
                return None
            return BotEvent(
                event_id=msg_id,
                user_phone=str(user_phone),
                type=BotEventType.CONTACT,
                contact=parsed,
                timestamp=timestamp,
            )

        if msg_type == "button":
            # Quick Reply button reply. The button text is in
            # message.button.text. We surface it as a TEXT event so
            # the application can match on the button text.
            button = message.get("button", {})
            text = button.get("text", "") if isinstance(button, dict) else ""
            if not text:
                return None
            return BotEvent(
                event_id=msg_id,
                user_phone=str(user_phone),
                type=BotEventType.TEXT,
                text=str(text),
                timestamp=timestamp,
            )

        if msg_type == "interactive":
            # Interactive message reply (button or list).
            interactive = message.get("interactive", {})
            if not isinstance(interactive, dict):
                return None

            # Button reply: interactive.type == "button_reply"
            if interactive.get("type") == "button_reply":
                button_reply = interactive.get("button_reply", {})
                button_id = button_reply.get("id", "") if isinstance(button_reply, dict) else ""
                title = button_reply.get("title", "") if isinstance(button_reply, dict) else ""
                if not button_id:
                    return None
                return BotEvent(
                    event_id=msg_id,
                    user_phone=str(user_phone),
                    type=BotEventType.BUTTON_REPLY,
                    text=str(title) if title else None,
                    button_id=str(button_id),
                    timestamp=timestamp,
                )

            # List reply: interactive.type == "list_reply"
            if interactive.get("type") == "list_reply":
                list_reply = interactive.get("list_reply", {})
                list_id = list_reply.get("id", "") if isinstance(list_reply, dict) else ""
                title = list_reply.get("title", "") if isinstance(list_reply, dict) else ""
                if not list_id:
                    return None
                return BotEvent(
                    event_id=msg_id,
                    user_phone=str(user_phone),
                    type=BotEventType.LIST_REPLY,
                    text=str(title) if title else None,
                    list_id=str(list_id),
                    timestamp=timestamp,
                )

            _logger.info("provider=dialog360 event=unknown interactive_type=%s", interactive.get("type"))
            return None

        _logger.info("provider=dialog360 event=unknown message_type=%s", msg_type)
        return None


def _parse_contact(contact_obj: dict[str, Any]) -> BotContact | None:
    """Extract phone + display name from a 360dialog contact message."""
    name_obj = contact_obj.get("name") or {}
    if isinstance(name_obj, dict):
        name = (
            name_obj.get("formatted_name")
            or " ".join(
                filter(None, [name_obj.get("first_name"), name_obj.get("last_name")])
            )
            or ""
        )
    else:
        name = ""

    phones = contact_obj.get("phones") or []
    phone = None
    for phone_obj in phones:
        if not isinstance(phone_obj, dict):
            continue
        # wa_id is the WhatsApp ID (bare number, no + or spaces).
        if phone_obj.get("wa_id"):
            phone = str(phone_obj["wa_id"]).strip()
            break
        if not phone and phone_obj.get("phone"):
            phone = str(phone_obj["phone"]).strip()

    if not phone:
        return None
    return BotContact(phone=phone, name=name)


def _first_change_value(data: dict[str, Any]) -> dict[str, Any] | None:
    entries = data.get("entry") or []
    if not isinstance(entries, list) or not entries:
        return None
    changes = entries[0].get("changes") or []
    if not isinstance(changes, list) or not changes:
        return None
    value = changes[0].get("value")
    if not isinstance(value, dict):
        return None
    return value


def _timestamp(raw: Any) -> datetime | None:
    """Convert a UNIX timestamp string to timezone-aware UTC."""
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except (ValueError, TypeError):
        return None
