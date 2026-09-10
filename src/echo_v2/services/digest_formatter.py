"""DigestFormatter — builds the digest text from active waiting chats.

Takes a list of (active_state, contact_name, last_message_text) tuples
and formats them into a single WhatsApp message for the Echo Business Bot.

Sorting: by ``waiting_since`` ascending (oldest first).
Limit: 20 items, with a trailing "ועוד N שיחות..." if more.
"""

from __future__ import annotations

from dataclasses import dataclass

from echo_v2.domain.waiting_for_me import WaitingForMeActive

__all__ = ["DigestFormatter", "DigestItem"]

MAX_ITEMS = 20


@dataclass(frozen=True)
class DigestItem:
    """One item in the digest — a single waiting chat.

    Attributes:
        active: The active waiting state.
        contact_name: Resolved contact name, or ``None`` if unknown.
        last_message_text: The last inbound message text, or ``None``
            if the message was media-only or had no text.
    """

    active: WaitingForMeActive
    contact_name: str | None
    last_message_text: str | None


class DigestFormatter:
    """Format active waiting chats into a digest message.

    The format is::

        בוקר טוב 👋

        🔴 N מחכים לך:

        דנה: "יש מצב לחמישי?"
        יוסי: "תשלח לי את ההצעה?"

    If there are more than ``MAX_ITEMS`` chats, a trailing line is added::

        ועוד N שיחות...
    """

    def format(self, items: list[DigestItem]) -> str:
        if not items:
            return ""  # caller should not call with empty list

        # Sort by waiting_since ascending (oldest first).
        sorted_items = sorted(items, key=lambda i: i.active.waiting_since)

        count = len(sorted_items)
        lines: list[str] = ["בוקר טוב 👋", ""]

        if count == 1:
            lines.append("🔴 1 מחכה לך:")
        else:
            lines.append(f"🔴 {count} מחכים לך:")
        lines.append("")

        shown = sorted_items[:MAX_ITEMS]
        for item in shown:
            name = item.contact_name or _phone_from_chat_id(item.active.chat_id)
            if item.last_message_text:
                lines.append(f'{name}: "{item.last_message_text}"')
            else:
                lines.append(f'{name}: "שלח/ה הודעה שמחכה להתייחסותך"')

        remaining = count - MAX_ITEMS
        if remaining > 0:
            lines.append("")
            lines.append(f"ועוד {remaining} שיחות...")

        return "\n".join(lines)


def _phone_from_chat_id(chat_id: str) -> str:
    """Extract phone number from a WhatsApp chat ID.

    ``972501234567@c.us`` → ``972501234567``
    ``group-123@g.us`` → ``group-123``
    """
    return chat_id.split("@")[0] if "@" in chat_id else chat_id
