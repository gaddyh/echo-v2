"""Phone-number normalization for stable identity.

``users.phone_number`` is ``UNIQUE`` and stores canonical E.164 form
(e.g. ``+972546610653``). Without normalization, ``0546610653``,
``972546610653``, and ``+972546610653`` would become three separate users.
Normalization collapses them to one canonical value before insert, so the
``UNIQUE`` constraint correctly treats them as one row.

Numbers without an explicit country code are parsed against a default
region (``ECHO_DEFAULT_PHONE_REGION``, default ``IL`` for the MVP audience).
"""

from __future__ import annotations

import phonenumbers

__all__ = ["PhoneParseError", "normalize_phone_e164"]


class PhoneParseError(ValueError):
    """Raised when a phone number cannot be parsed into E.164 form."""


def normalize_phone_e164(raw: str, *, default_region: str = "IL") -> str:
    """Normalize ``raw`` to canonical E.164 (``+<country><national>``).

    Raises :class:`PhoneParseError` if the number is not a valid phone
    number. ``default_region`` is used only when ``raw`` lacks an explicit
    country code (ISO 3166-1 alpha-2, e.g. ``IL``).
    """
    try:
        parsed = phonenumbers.parse(raw, default_region)
    except phonenumbers.NumberParseException as exc:
        raise PhoneParseError(f"Could not parse phone number {raw!r}: {exc}") from exc

    if not phonenumbers.is_valid_number(parsed):
        raise PhoneParseError(f"Not a valid phone number: {raw!r}")

    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
