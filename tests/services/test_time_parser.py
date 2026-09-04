"""Tests for the time parser (regex layer + combined parser)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from echo_v2.services.time_parser import (
    CombinedTimeParser,
    TimeParseError,
)
from echo_v2.services.time_parser_regex import parse_time_expression

TZ = "Asia/Jerusalem"
# Fixed "now" for deterministic tests: 2026-09-05 10:00 UTC = 13:00 Israel (UTC+3).
NOW = datetime(2026, 9, 5, 10, 0, 0, tzinfo=timezone.utc)


# --- bare time -------------------------------------------------------------


def test_bare_hour_today():
    # 14:00 local = 11:00 UTC (same day, future)
    result = parse_time_expression("ב-14", user_timezone=TZ, now_utc=NOW)
    assert result == datetime(2026, 9, 5, 11, 0, tzinfo=timezone.utc)


def test_bare_hour_past_today_rolls_to_tomorrow():
    # 8:00 local = 5:00 UTC (past today → tomorrow)
    result = parse_time_expression("8", user_timezone=TZ, now_utc=NOW)
    assert result == datetime(2026, 9, 6, 5, 0, tzinfo=timezone.utc)


def test_bare_hour_with_colon():
    result = parse_time_expression("20:00", user_timezone=TZ, now_utc=NOW)
    # 20:00 local = 17:00 UTC
    assert result == datetime(2026, 9, 5, 17, 0, tzinfo=timezone.utc)


def test_bare_hour_pm():
    result = parse_time_expression("8pm", user_timezone=TZ, now_utc=NOW)
    # 20:00 local = 17:00 UTC
    assert result == datetime(2026, 9, 5, 17, 0, tzinfo=timezone.utc)


def test_bare_hour_am():
    # 8am local = 5:00 UTC (past → tomorrow)
    result = parse_time_expression("8am", user_timezone=TZ, now_utc=NOW)
    assert result == datetime(2026, 9, 6, 5, 0, tzinfo=timezone.utc)


# --- tomorrow --------------------------------------------------------------


def test_tomorrow_hebrew():
    result = parse_time_expression("מחר ב-8", user_timezone=TZ, now_utc=NOW)
    assert result == datetime(2026, 9, 6, 5, 0, tzinfo=timezone.utc)


def test_tomorrow_english():
    result = parse_time_expression("tomorrow at 8", user_timezone=TZ, now_utc=NOW)
    assert result == datetime(2026, 9, 6, 5, 0, tzinfo=timezone.utc)


def test_tomorrow_with_colon():
    result = parse_time_expression("tomorrow 20:00", user_timezone=TZ, now_utc=NOW)
    assert result == datetime(2026, 9, 6, 17, 0, tzinfo=timezone.utc)


# --- relative hours/minutes ------------------------------------------------


def test_relative_hour_hebrew():
    result = parse_time_expression("בעוד שעה", user_timezone=TZ, now_utc=NOW)
    assert result == datetime(2026, 9, 5, 11, 0, tzinfo=timezone.utc)


def test_relative_two_hours_hebrew():
    result = parse_time_expression("בעוד שעתיים", user_timezone=TZ, now_utc=NOW)
    assert result == datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def test_relative_n_hours_hebrew():
    result = parse_time_expression("בעוד 3 שעות", user_timezone=TZ, now_utc=NOW)
    assert result == datetime(2026, 9, 5, 13, 0, tzinfo=timezone.utc)


def test_relative_hours_english():
    result = parse_time_expression("in 2 hours", user_timezone=TZ, now_utc=NOW)
    assert result == datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def test_relative_half_hour_hebrew():
    result = parse_time_expression("בעוד חצי שעה", user_timezone=TZ, now_utc=NOW)
    assert result == datetime(2026, 9, 5, 10, 30, tzinfo=timezone.utc)


def test_relative_minutes_english():
    result = parse_time_expression("in 30 minutes", user_timezone=TZ, now_utc=NOW)
    assert result == datetime(2026, 9, 5, 10, 30, tzinfo=timezone.utc)


# --- day names -------------------------------------------------------------


def test_day_name_hebrew():
    # NOW is 2026-09-05 (Saturday). יום ראשון = Sunday = next day.
    result = parse_time_expression("יום ראשון ב-10", user_timezone=TZ, now_utc=NOW)
    # Sunday 10:00 local = 07:00 UTC
    assert result == datetime(2026, 9, 6, 7, 0, tzinfo=timezone.utc)


def test_day_name_english():
    result = parse_time_expression("sunday at 10", user_timezone=TZ, now_utc=NOW)
    assert result == datetime(2026, 9, 6, 7, 0, tzinfo=timezone.utc)


# --- explicit datetime -----------------------------------------------------


def test_explicit_datetime():
    result = parse_time_expression("2026-09-10 08:00", user_timezone=TZ, now_utc=NOW)
    # 08:00 local = 05:00 UTC
    assert result == datetime(2026, 9, 10, 5, 0, tzinfo=timezone.utc)


# --- errors ----------------------------------------------------------------


def test_empty_raises():
    with pytest.raises(TimeParseError):
        parse_time_expression("", user_timezone=TZ, now_utc=NOW)


def test_unparseable_raises():
    with pytest.raises(TimeParseError):
        parse_time_expression("sometime next week maybe", user_timezone=TZ, now_utc=NOW)


def test_past_time_raises():
    # 8am today is past → bare_time rolls to tomorrow, but explicit datetime in the past raises
    with pytest.raises(TimeParseError):
        parse_time_expression("2020-01-01 08:00", user_timezone=TZ, now_utc=NOW)


# --- combined parser -------------------------------------------------------


async def test_combined_parser_regex_success():
    """Combined parser handles regex-parseable expressions without LLM."""
    parser = CombinedTimeParser(llm_parser=None)
    result = await parser.parse("מחר ב-8", user_timezone=TZ, now_utc=NOW)
    assert result == datetime(2026, 9, 6, 5, 0, tzinfo=timezone.utc)


async def test_combined_parser_regex_fail_no_llm_raises():
    """If regex fails and no LLM is configured, raise TimeParseError."""
    parser = CombinedTimeParser(llm_parser=None)
    with pytest.raises(TimeParseError):
        await parser.parse("sometime next week", user_timezone=TZ, now_utc=NOW)
