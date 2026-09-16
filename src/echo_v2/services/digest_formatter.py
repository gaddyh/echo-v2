"""DigestItem — one item in the interactive waiting-list reply.

The :class:`DigestItem` dataclass is used by
:class:`echo_v2.services.digest_reply.DigestReplyService` to build the
interactive list sent after the user taps the digest button.

The former :class:`DigestFormatter` and :class:`DigestTemplateParams`
have been removed — the digest worker now inlines the trivial
``str(count)`` formatting and no longer needs a dedicated formatter.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["DigestItem"]


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
