"""DigestFormatter — builds template parameters from active waiting chats.

Produces the two body parameters for the ``morning_waiting_digest``
WhatsApp template:

* {{1}} — user's first name (provided by the caller)
* {{2}} — count of waiting chats

The template body is::

    בוקר טוב {{1}} 👋

    Echo מצא {{2}} שיחות שאולי מחכות לתגובה שלך.

Header: "סיכום הבוקר של Echo"
Footer: "Echo — לא מפספסים שיחה חשובה"
Button: Quick Reply "צפה בשיחות"

The template is a navigation-only gateway — no conversation content is
exposed. The full interactive list is sent only after the user taps
the button, inside the 24-hour customer service window.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["DigestFormatter", "DigestItem", "DigestTemplateParams"]


@dataclass(frozen=True)
class DigestItem:
    """One item in the digest — a single waiting chat.

    Attributes:
        active: The active waiting state.
        contact_name: Resolved contact name, or ``None`` if unknown.
        last_message_text: The last inbound message text, or ``None``
            if the message was media-only or had no text.
    """

    active: object
    contact_name: str | None
    last_message_text: str | None


@dataclass(frozen=True)
class DigestTemplateParams:
    """The two body parameters for the ``morning_waiting_digest`` template.

    Attributes:
        first_name: {{1}} — the user's first name.
        count: {{2}} — the number of waiting chats (as a string).
    """

    first_name: str
    count: str


class DigestFormatter:
    """Format active waiting chats into template body parameters.

    Only produces the count — no conversation content is exposed in the
    template. The interactive list is sent after the user taps the button.
    """

    def format(self, items: list[DigestItem], *, first_name: str) -> DigestTemplateParams:
        """Format items into template parameters.

        Args:
            items: The digest items (must be non-empty).
            first_name: The user's first name for {{1}}.

        Returns the two body parameters for the template.
        """
        return DigestTemplateParams(
            first_name=first_name,
            count=str(len(items)),
        )
