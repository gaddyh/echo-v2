"""Notify the Echo owner when someone joins the waitlist.

Sends a WhatsApp message from the Echo Business Bot (360dialog) to the
owner's phone on each *new* signup. Duplicates are silent no-ops in the
repository, so the notifier is only invoked when ``add`` returns ``True``.

The owner phone is configured via ``ECHO_OWNER_PHONE`` (E.164 or local
Israeli number — normalized the same way as signup phones). If the env
var is unset, the notifier is a no-op (useful for tests and previews).
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from echo_v2.integrations.dialog360.client import Dialog360Client
from echo_v2.persistence.identity import PhoneParseError, normalize_phone_e164

__all__ = ["Dialog360WaitlistNotifier", "WaitlistNotifier"]

_logger = logging.getLogger("echo_v2.services.waitlist_notifier")

_WTP_LABELS = {
    "free": "חינם",
    "under_30": "עד 30 ₪",
    "30_70": "30–70 ₪",
    "70_120": "70–120 ₪",
    "120_plus": "120+ ₪",
}


@runtime_checkable
class WaitlistNotifier(Protocol):
    """Send the Echo owner a WhatsApp notification on a new signup."""

    async def notify(
        self,
        *,
        name: str,
        phone: str,
        willingness_to_pay: str | None = None,
    ) -> None: ...


class Dialog360WaitlistNotifier:
    """Notify the owner via the Echo Business Bot (360dialog)."""

    def __init__(self, *, bot: Dialog360Client, owner_phone: str) -> None:
        self._bot = bot
        try:
            self._owner_phone = normalize_phone_e164(owner_phone)
        except PhoneParseError as exc:
            raise ValueError(
                f"ECHO_OWNER_PHONE is not a valid phone number: {exc}"
            ) from exc

    async def notify(
        self,
        *,
        name: str,
        phone: str,
        willingness_to_pay: str | None = None,
    ) -> None:
        wtp_label = _WTP_LABELS.get(willingness_to_pay or "", "—")
        text = (
            f"הצטרף חדש לרשימת ההמתנה:\n"
            f"שם: {name}\n"
            f"טלפון: {phone}\n"
            f"WTP: {wtp_label}"
        )
        _logger.info(
            "waitlist: notifying owner %s about new signup %s (%s)",
            self._owner_phone, name, phone,
        )
        try:
            msg_id = await self._bot.send_text(self._owner_phone, text)
            _logger.info("waitlist: notified owner, msg_id=%s", msg_id)
        except Exception:
            _logger.exception("waitlist: failed to notify owner")
