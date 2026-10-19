"""Print the Echo Guard WhatsApp Click-to-Chat invitation."""

from __future__ import annotations

import os
from urllib.parse import quote

from echo_v2.persistence.identity import PhoneParseError, normalize_phone_e164

_JOIN_MESSAGE = "היי, אני רוצה להצטרף לאקו גארד"


def build_click_to_chat_url(phone: str) -> str:
    """Build a WhatsApp Click-to-Chat URL for the Guard invitation."""
    try:
        normalized = normalize_phone_e164(phone)
    except PhoneParseError as exc:
        raise ValueError(f"invalid ECHO_BOT_PHONE: {exc}") from exc
    return f"https://wa.me/{normalized.lstrip('+')}?text={quote(_JOIN_MESSAGE)}"


def main() -> None:
    phone = os.environ.get("ECHO_BOT_PHONE", "")
    if not phone:
        raise SystemExit("ECHO_BOT_PHONE is required")
    url = build_click_to_chat_url(phone)
    print("הצטרפות ל-Echo Guard:")
    print(url)


if __name__ == "__main__":
    main()
