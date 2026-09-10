"""DigestFormatter — builds template parameters from active waiting chats.

Produces the three body parameters for the ``morning_waiting_digest``
WhatsApp template:

* {{1}} — user's first name (provided by the caller)
* {{2}} — count of waiting chats
* {{3}} — the list of waiting chats (name + last message, sorted by
  ``waiting_since`` ascending, max 20 items)

The template body is::

    בוקר טוב {{1}} 👋

    יש לך {{2}} שיחות שמחכות לטיפול:

    {{3}}

    אפשר להשיב "הכול" כדי לקבל פירוט מלא.

Header: "סיכום הבוקר של Echo"
Footer: "Echo — לא מפספסים שיחה חשובה"
Button: Quick Reply "הצג הכול"
"""

from __future__ import annotations

from dataclasses import dataclass

from echo_v2.domain.waiting_for_me import WaitingForMeActive

__all__ = ["DigestFormatter", "DigestItem", "DigestTemplateParams"]

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


@dataclass(frozen=True)
class DigestTemplateParams:
    """The three body parameters for the ``morning_waiting_digest`` template.

    Attributes:
        first_name: {{1}} — the user's first name.
        count: {{2}} — the number of waiting chats (as a string).
        items_text: {{3}} — the formatted list of waiting chats.
    """

    first_name: str
    count: str
    items_text: str


class DigestFormatter:
    """Format active waiting chats into template body parameters.

    The items text ({{3}}) is built as::

        דנה: "יש מצב לחמישי?"
        יוסי: "תשלח לי את ההצעה?"

    If there are more than ``MAX_ITEMS`` chats, a trailing line is added::

        ועוד N שיחות...
    """

    def format(self, items: list[DigestItem], *, first_name: str) -> DigestTemplateParams:
        """Format items into template parameters.

        Args:
            items: The digest items (must be non-empty).
            first_name: The user's first name for {{1}}.

        Returns the three body parameters for the template.
        """
        # Sort by waiting_since ascending (oldest first).
        sorted_items = sorted(items, key=lambda i: i.active.waiting_since)

        count = len(sorted_items)
        lines: list[str] = []

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

        return DigestTemplateParams(
            first_name=first_name,
            count=str(count),
            items_text="\n".join(lines),
        )


def _phone_from_chat_id(chat_id: str) -> str:
    """Extract phone number from a WhatsApp chat ID.

    ``972501234567@c.us`` → ``972501234567``
    ``group-123@g.us`` → ``group-123``
    """
    return chat_id.split("@")[0] if "@" in chat_id else chat_id
