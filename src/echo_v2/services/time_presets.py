"""Shared time-preset → UTC conversion.

Used by both the snooze action (WaitingForMeActionService) and the
schedule-message action (WaitingListService.schedule_send) so they
never disagree on preset semantics.

Presets:
* ``10m`` → 10 minutes from now
* ``1h`` → 1 hour from now
* ``3h`` → 3 hours from now
* ``morning`` → next 08:00 local
* ``afternoon`` → next 14:00 local
* ``evening`` → next 18:00 local
* ``tomorrow`` → tomorrow 08:00 local

"Next" means: if the target hour hasn't passed today, use today;
otherwise use tomorrow. ``tomorrow`` always uses the next day.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

__all__ = [
    "PRESET_HOURS",
    "PRESET_RELATIVE",
    "preset_to_utc",
    "valid_presets",
]

# Absolute-time presets → local hour.
PRESET_HOURS: dict[str, int] = {
    "morning": 8,
    "afternoon": 14,
    "evening": 18,
}

# Relative-time presets (offset from now).
PRESET_RELATIVE: dict[str, timedelta] = {
    "10m": timedelta(minutes=10),
    "1h": timedelta(hours=1),
    "3h": timedelta(hours=3),
}


def valid_presets() -> list[str]:
    """Return all valid preset names."""
    return list(PRESET_RELATIVE.keys()) + list(PRESET_HOURS.keys()) + ["tomorrow"]


def preset_to_utc(
    preset: str,
    *,
    now_utc: datetime,
    tz_name: str = "Asia/Jerusalem",
) -> datetime:
    """Map a time preset to a UTC datetime.

    Raises ``ValueError`` for an unknown preset.
    """
    relative = PRESET_RELATIVE.get(preset)
    if relative is not None:
        return now_utc + relative

    tz = ZoneInfo(tz_name)
    local_now = now_utc.astimezone(tz)

    if preset == "tomorrow":
        target = (local_now + timedelta(days=1)).replace(
            hour=PRESET_HOURS["morning"], minute=0, second=0, microsecond=0
        )
        return target.astimezone(timezone.utc)

    hour = PRESET_HOURS.get(preset)
    if hour is None:
        raise ValueError(f"unknown time preset: {preset}")

    target = local_now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= local_now:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)
