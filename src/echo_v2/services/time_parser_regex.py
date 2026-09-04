"""Time expression parser for the scheduling flow.

Parses natural-language time expressions (Hebrew and English) into a
timezone-aware UTC datetime. Uses deterministic pattern matching — no
LLM. The parser handles the common cases for the MVP audience (Israeli
users, Hebrew input):

Supported patterns:
* ``ב-8`` / ``ב8`` / ``at 8`` → today at 8:00 (or tomorrow if past)
* ``מחר ב-8`` / ``מחר ב8`` / ``tomorrow at 8`` → tomorrow at 8:00
* ``בעוד שעה`` / ``בעוד שעתיים`` / ``in 1 hour`` / ``in 2 hours`` → now + N hours
* ``בעוד חצי שעה`` / ``in 30 minutes`` → now + 30 minutes
* ``20:00`` / ``8:00`` / ``8pm`` / ``20`` → time today (or tomorrow if past)
* ``2026-09-05 08:00`` → explicit datetime
* Day names: ``יום ראשון ב-10`` / ``sunday at 10`` → next occurrence of that day

The parser returns ``None`` if it cannot parse the expression — the flow
service then asks the user to rephrase.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

__all__ = ["TimeParseError", "parse_time_expression"]


class TimeParseError(ValueError):
    """Raised when a time expression cannot be parsed."""


# Hebrew day names → weekday number (Monday=0 ... Sunday=6).
_HEBREW_DAYS: dict[str, int] = {
    "ראשון": 6,      # Sunday
    "שני": 0,        # Monday
    "שלישי": 1,      # Tuesday
    "רביעי": 2,      # Wednesday
    "חמישי": 3,      # Thursday
    "שישי": 4,       # Friday
    "שבת": 5,        # Saturday
}

_ENGLISH_DAYS: dict[str, int] = {
    "sunday": 6,
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
}


@dataclass(frozen=True)
class _ParseResult:
    """Internal result: a naive local datetime in the user's timezone."""

    local_dt: datetime


def parse_time_expression(
    text: str,
    *,
    user_timezone: str,
    now_utc: datetime | None = None,
) -> datetime:
    """Parse a time expression into a timezone-aware UTC datetime.

    Args:
        text: The user's time expression (Hebrew or English).
        user_timezone: IANA timezone name (e.g. ``Asia/Jerusalem``).
        now_utc: Override for testing; defaults to current UTC time.

    Returns:
        A timezone-aware UTC datetime.

    Raises:
        TimeParseError: If the expression cannot be parsed or the
            resulting time is in the past.
    """
    raw = text.strip()
    if not raw:
        raise TimeParseError("empty time expression")

    now = now_utc or datetime.now(timezone.utc)
    tz = ZoneInfo(user_timezone)
    now_local = now.astimezone(tz)

    result = (
        _try_relative_hours(raw, now_local)
        or _try_relative_minutes(raw, now_local)
        or _try_tomorrow_at_time(raw, now_local)
        or _try_day_name_at_time(raw, now_local)
        or _try_bare_time(raw, now_local)
        or _try_explicit_datetime(raw, tz)
    )

    if result is None:
        raise TimeParseError(f"could not parse time expression: {text!r}")

    # Convert local datetime to UTC.
    local_aware = result.local_dt.replace(tzinfo=tz)
    utc_dt = local_aware.astimezone(timezone.utc)

    if utc_dt <= now:
        raise TimeParseError(
            f"parsed time {utc_dt.isoformat()} is not in the future "
            f"(now={now.isoformat()})"
        )

    return utc_dt


# --- pattern matchers ------------------------------------------------------


def _extract_hour_minute(text: str) -> tuple[int, int] | None:
    """Extract HH:MM or HH from a text fragment.

    Handles ``8``, ``8:00``, ``8:30``, ``20:00``, ``8pm``, ``8am``,
    ``ב-8``, ``ב8``, ``at 8``, ``at 8:30``.
    """
    # Strip Hebrew/English prefixes.
    cleaned = re.sub(r"^(ב[-\s]*|at\s+)", "", text, flags=re.IGNORECASE).strip()

    # HH:MM or HH:MMam/pm
    m = re.match(r"^(\d{1,2}):(\d{2})\s*(am|pm)?$", cleaned, re.IGNORECASE)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2))
        ampm = m.group(3)
        if ampm:
            hour = _apply_ampm(hour, ampm.lower())
        if _valid_hm(hour, minute):
            return hour, minute

    # HHam/pm
    m = re.match(r"^(\d{1,2})\s*(am|pm)$", cleaned, re.IGNORECASE)
    if m:
        hour = int(m.group(1))
        ampm = m.group(2).lower()
        hour = _apply_ampm(hour, ampm)
        if _valid_hm(hour, 0):
            return hour, 0

    # Bare HH (no am/pm, no colon)
    m = re.match(r"^(\d{1,2})$", cleaned)
    if m:
        hour = int(m.group(1))
        if _valid_hm(hour, 0):
            return hour, 0

    return None


def _apply_ampm(hour: int, ampm: str) -> int:
    if ampm == "am":
        return 0 if hour == 12 else hour
    # pm
    return hour if hour == 12 else hour + 12


def _valid_hm(hour: int, minute: int) -> bool:
    return 0 <= hour <= 23 and 0 <= minute <= 59


def _try_relative_hours(
    text: str, now_local: datetime
) -> _ParseResult | None:
    """Match 'בעוד שעה' / 'בעוד שעתיים' / 'in 1 hour' / 'in 2 hours'."""
    # Hebrew: בעוד שעה (1 hour), בעוד שעתיים (2 hours), בעוד 3 שעות
    m = re.match(r"^בעוד\s+שעה$", text)
    if m:
        return _ParseResult(local_dt=now_local + timedelta(hours=1))

    m = re.match(r"^בעוד\s+שעתיים$", text)
    if m:
        return _ParseResult(local_dt=now_local + timedelta(hours=2))

    m = re.match(r"^בעוד\s+(\d+)\s+שעות$", text)
    if m:
        return _ParseResult(local_dt=now_local + timedelta(hours=int(m.group(1))))

    # English: in N hour(s)
    m = re.match(r"^in\s+(\d+)\s+hour[s]?$", text, re.IGNORECASE)
    if m:
        return _ParseResult(local_dt=now_local + timedelta(hours=int(m.group(1))))

    return None


def _try_relative_minutes(
    text: str, now_local: datetime
) -> _ParseResult | None:
    """Match 'בעוד חצי שעה' / 'in 30 minutes' / 'in 30 min'."""
    m = re.match(r"^בעוד\s+חצי\s+שעה$", text)
    if m:
        return _ParseResult(local_dt=now_local + timedelta(minutes=30))

    m = re.match(r"^בעוד\s+(\d+)\s+דקות$", text)
    if m:
        return _ParseResult(local_dt=now_local + timedelta(minutes=int(m.group(1))))

    m = re.match(r"^in\s+(\d+)\s+min(?:ute)?s?$", text, re.IGNORECASE)
    if m:
        return _ParseResult(local_dt=now_local + timedelta(minutes=int(m.group(1))))

    return None


def _try_tomorrow_at_time(
    text: str, now_local: datetime
) -> _ParseResult | None:
    """Match 'מחר ב-8' / 'מחר ב8' / 'tomorrow at 8' / 'tomorrow 8:00'."""
    # Hebrew: מחר ב-8 / מחר ב8 / מחר ב-8:30
    m = re.match(r"^מחר\s+ב[-\s]*(.+)$", text)
    if m:
        hm = _extract_hour_minute(m.group(1))
        if hm:
            tomorrow = now_local.date() + timedelta(days=1)
            return _ParseResult(
                local_dt=datetime.combine(tomorrow, _hm_to_time(*hm))
            )

    # English: tomorrow at 8 / tomorrow 8:00 / tomorrow at 8pm
    m = re.match(r"^tomorrow\s+(?:at\s+)?(.+)$", text, re.IGNORECASE)
    if m:
        hm = _extract_hour_minute(m.group(1))
        if hm:
            tomorrow = now_local.date() + timedelta(days=1)
            return _ParseResult(
                local_dt=datetime.combine(tomorrow, _hm_to_time(*hm))
            )

    return None


def _try_day_name_at_time(
    text: str, now_local: datetime
) -> _ParseResult | None:
    """Match 'יום ראשון ב-10' / 'sunday at 10' / 'sunday 10:00'."""
    # Hebrew: יום <day> ב-<time>
    m = re.match(r"^יום\s+(\S+)\s+ב[-\s]*(.+)$", text)
    if m:
        day_name = m.group(1)
        if day_name in _HEBREW_DAYS:
            hm = _extract_hour_minute(m.group(2))
            if hm:
                target_weekday = _HEBREW_DAYS[day_name]
                return _next_weekday_at_time(now_local, target_weekday, *hm)

    # English: <day> at <time> / <day> <time>
    m = re.match(r"^(\w+)\s+(?:at\s+)?(.+)$", text, re.IGNORECASE)
    if m:
        day_name = m.group(1).lower()
        if day_name in _ENGLISH_DAYS:
            hm = _extract_hour_minute(m.group(2))
            if hm:
                target_weekday = _ENGLISH_DAYS[day_name]
                return _next_weekday_at_time(now_local, target_weekday, *hm)

    return None


def _try_bare_time(
    text: str, now_local: datetime
) -> _ParseResult | None:
    """Match 'ב-8' / 'at 8' / '8:00' / '8pm' / '20' — time today or tomorrow."""
    hm = _extract_hour_minute(text)
    if hm is None:
        return None

    today = now_local.date()
    candidate = datetime.combine(today, _hm_to_time(*hm))

    # If the time has already passed today, schedule for tomorrow.
    if candidate <= now_local.replace(tzinfo=None):
        candidate = datetime.combine(
            today + timedelta(days=1), _hm_to_time(*hm)
        )

    return _ParseResult(local_dt=candidate)


def _try_explicit_datetime(
    text: str, tz: ZoneInfo
) -> _ParseResult | None:
    """Match '2026-09-05 08:00' or '2026-09-05T08:00:00'."""
    # Try ISO format.
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt)  # noqa: DTZ007 - intentional naive local dt, tz attached later
            return _ParseResult(local_dt=dt)
        except ValueError:
            continue
    return None


# --- helpers ---------------------------------------------------------------


def _hm_to_time(hour: int, minute: int):
    from datetime import time as dt_time
    return dt_time(hour=hour, minute=minute)


def _next_weekday_at_time(
    now_local: datetime,
    target_weekday: int,
    hour: int,
    minute: int,
) -> _ParseResult:
    """Find the next occurrence of ``target_weekday`` at ``hour:minute``."""
    current_weekday = now_local.weekday()
    days_ahead = (target_weekday - current_weekday) % 7
    if days_ahead == 0:
        # Same day — check if the time is still ahead.
        candidate = datetime.combine(
            now_local.date(), _hm_to_time(hour, minute)
        )
        if candidate <= now_local.replace(tzinfo=None):
            days_ahead = 7

    target_date = now_local.date() + timedelta(days=days_ahead)
    return _ParseResult(
        local_dt=datetime.combine(target_date, _hm_to_time(hour, minute))
    )
