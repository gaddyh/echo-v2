"""Tests for the time parser (regex layer + combined parser)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

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


# --- LLMTimeParser ----------------------------------------------------------


async def test_llm_parser_parse_success():
    """LLMTimeParser.parse returns a future datetime from the LLM response."""
    from unittest.mock import AsyncMock, MagicMock

    from echo_v2.services.time_parser import LLMTimeParser

    future = (NOW + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = f'{{"utc_datetime": "{future}"}}'

    mock_client = MagicMock()
    mock_client.chat = MagicMock()
    mock_client.chat.completions = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

    parser = LLMTimeParser(client=mock_client)
    result = await parser.parse("sometime", user_timezone=TZ, now_utc=NOW)
    assert result == NOW + timedelta(hours=2)
    assert result.tzinfo is not None


async def test_llm_parser_parse_api_error_raises():
    """If the OpenAI API raises, LLMTimeParser wraps it in TimeParseError."""
    from unittest.mock import AsyncMock, MagicMock

    from echo_v2.services.time_parser import LLMTimeParser

    mock_client = MagicMock()
    mock_client.chat = MagicMock()
    mock_client.chat.completions = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        side_effect=RuntimeError("network error")
    )

    parser = LLMTimeParser(client=mock_client)
    with pytest.raises(TimeParseError, match="LLM time parser request failed"):
        await parser.parse("sometime", user_timezone=TZ, now_utc=NOW)


# --- _parse_llm_output ------------------------------------------------------


def test_parse_llm_output_valid():
    from echo_v2.services.time_parser import _parse_llm_output

    future = NOW + timedelta(hours=1)
    raw = json.dumps({"utc_datetime": future.isoformat()})
    result = _parse_llm_output(raw, NOW)
    assert result == future


def test_parse_llm_output_with_z_suffix():
    from echo_v2.services.time_parser import _parse_llm_output

    future = (NOW + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = json.dumps({"utc_datetime": future})
    result = _parse_llm_output(raw, NOW)
    assert result.tzinfo is not None
    assert result > NOW


def test_parse_llm_output_with_code_fences():
    from echo_v2.services.time_parser import _parse_llm_output

    future = (NOW + timedelta(hours=1)).isoformat()
    raw = f"```json\n{{\"utc_datetime\": \"{future}\"}}\n```"
    result = _parse_llm_output(raw, NOW)
    assert result > NOW


def test_parse_llm_output_invalid_json_raises():
    from echo_v2.services.time_parser import _parse_llm_output

    with pytest.raises(TimeParseError, match="not valid JSON"):
        _parse_llm_output("not json", NOW)


def test_parse_llm_output_missing_key_raises():
    from echo_v2.services.time_parser import _parse_llm_output

    with pytest.raises(TimeParseError, match="missing 'utc_datetime'"):
        _parse_llm_output('{"foo": "bar"}', NOW)


def test_parse_llm_output_invalid_datetime_raises():
    from echo_v2.services.time_parser import _parse_llm_output

    with pytest.raises(TimeParseError, match="invalid datetime"):
        _parse_llm_output('{"utc_datetime": "not-a-date"}', NOW)


def test_parse_llm_output_past_time_raises():
    from echo_v2.services.time_parser import _parse_llm_output

    past = (NOW - timedelta(hours=1)).isoformat()
    with pytest.raises(TimeParseError, match="past time"):
        _parse_llm_output(f'{{"utc_datetime": "{past}"}}', NOW)


def test_parse_llm_output_naive_datetime_assumes_utc():
    from echo_v2.services.time_parser import _parse_llm_output

    future_naive = (NOW + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    raw = json.dumps({"utc_datetime": future_naive})
    result = _parse_llm_output(raw, NOW)
    assert result.tzinfo is not None
    assert result > NOW


# --- CombinedTimeParser with LLM fallback -----------------------------------


async def test_combined_parser_falls_back_to_llm():
    """CombinedTimeParser falls back to LLM when regex fails."""
    from unittest.mock import AsyncMock, MagicMock

    from echo_v2.services.time_parser import CombinedTimeParser, LLMTimeParser

    future = (NOW + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = f'{{"utc_datetime": "{future}"}}'

    mock_client = MagicMock()
    mock_client.chat = MagicMock()
    mock_client.chat.completions = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

    llm = LLMTimeParser(client=mock_client)
    parser = CombinedTimeParser(llm_parser=llm)
    result = await parser.parse("sometime next week", user_timezone=TZ, now_utc=NOW)
    assert result == NOW + timedelta(hours=2)


async def test_combined_parser_regex_success_no_llm_call():
    """CombinedTimeParser does not call LLM when regex succeeds."""
    from unittest.mock import AsyncMock, MagicMock

    from echo_v2.services.time_parser import CombinedTimeParser, LLMTimeParser

    mock_client = MagicMock()
    mock_client.chat = MagicMock()
    mock_client.chat.completions = MagicMock()
    mock_client.chat.completions.create = AsyncMock()

    llm = LLMTimeParser(client=mock_client)
    parser = CombinedTimeParser(llm_parser=llm)
    result = await parser.parse("מחר ב-8", user_timezone=TZ, now_utc=NOW)
    # Regex handled it — LLM was not called.
    mock_client.chat.completions.create.assert_not_called()
    assert result == datetime(2026, 9, 6, 5, 0, tzinfo=timezone.utc)


# --- additional regex coverage ---------------------------------------------


def test_bare_hour_with_colon_pm():
    """HH:MMpm pattern exercises the ampm branch in _extract_hour_minute."""
    # 8:30pm local = 17:30 UTC
    result = parse_time_expression("8:30pm", user_timezone=TZ, now_utc=NOW)
    assert result == datetime(2026, 9, 5, 17, 30, tzinfo=timezone.utc)


def test_bare_hour_with_colon_am():
    """HH:MMam pattern: 8:30am is past → tomorrow."""
    result = parse_time_expression("8:30am", user_timezone=TZ, now_utc=NOW)
    assert result == datetime(2026, 9, 6, 5, 30, tzinfo=timezone.utc)


def test_relative_minutes_hebrew_n_minutes():
    """'בעוד N דקות' pattern (Hebrew relative minutes)."""
    result = parse_time_expression("בעוד 30 דקות", user_timezone=TZ, now_utc=NOW)
    assert result == datetime(2026, 9, 5, 10, 30, tzinfo=timezone.utc)


def test_relative_minutes_english_min():
    """'in N min' (short form)."""
    result = parse_time_expression("in 15 min", user_timezone=TZ, now_utc=NOW)
    assert result == datetime(2026, 9, 5, 10, 15, tzinfo=timezone.utc)


def test_day_name_same_day_time_ahead():
    """Same weekday, time still ahead → stays on the same day (days_ahead == 0).

    NOW is 2026-09-05 (Saturday) 13:00 local.  'saturday at 20' is today
    at 20:00 local (17:00 UTC), which is in the future.
    """
    result = parse_time_expression("saturday at 20", user_timezone=TZ, now_utc=NOW)
    assert result == datetime(2026, 9, 5, 17, 0, tzinfo=timezone.utc)


def test_day_name_same_day_time_past_rolls_to_next_week():
    """Same weekday, time already passed → rolls 7 days ahead."""
    # NOW is 13:00 local on Saturday.  'saturday at 8' is past → next week.
    result = parse_time_expression("saturday at 8", user_timezone=TZ, now_utc=NOW)
    assert result == datetime(2026, 9, 12, 5, 0, tzinfo=timezone.utc)


def test_day_name_hebrew_same_day_time_ahead():
    """Hebrew day name, same day, time ahead."""
    # NOW is Saturday (יום שבת).  יום שבת ב-20 → today 20:00 local.
    result = parse_time_expression("יום שבת ב-20", user_timezone=TZ, now_utc=NOW)
    assert result == datetime(2026, 9, 5, 17, 0, tzinfo=timezone.utc)


def test_explicit_datetime_with_seconds():
    """Explicit datetime with seconds format."""
    result = parse_time_expression(
        "2026-09-10 08:30:00", user_timezone=TZ, now_utc=NOW
    )
    assert result == datetime(2026, 9, 10, 5, 30, tzinfo=timezone.utc)


def test_explicit_datetime_iso_with_t():
    """Explicit datetime with T separator."""
    result = parse_time_expression("2026-09-10T08:00", user_timezone=TZ, now_utc=NOW)
    assert result == datetime(2026, 9, 10, 5, 0, tzinfo=timezone.utc)


def test_12am_converts_to_midnight():
    """12am → 0:00 local (midnight), rolls to next day since past today."""
    # NOW is 13:00 local on Sep 5. 12am (0:00) has passed → next day.
    # 0:00 local on Sep 6 = 21:00 UTC on Sep 5.
    result = parse_time_expression("12am", user_timezone=TZ, now_utc=NOW)
    assert result == datetime(2026, 9, 5, 21, 0, tzinfo=timezone.utc)


def test_12pm_converts_to_noon():
    """12pm → 12:00 local (noon), rolls to next day since past today."""
    # NOW is 13:00 local on Sep 5. 12pm (noon) has passed → next day.
    # 12:00 local on Sep 6 = 09:00 UTC on Sep 6.
    result = parse_time_expression("12pm", user_timezone=TZ, now_utc=NOW)
    assert result == datetime(2026, 9, 6, 9, 0, tzinfo=timezone.utc)


# --- time_presets ----------------------------------------------------------


def test_valid_presets_returns_all_keys():
    """valid_presets() returns all relative, hour, and 'tomorrow' presets."""
    from echo_v2.services.time_presets import valid_presets

    presets = valid_presets()
    assert "now" in presets
    assert "10m" in presets
    assert "1h" in presets
    assert "3h" in presets
    assert "morning" in presets
    assert "afternoon" in presets
    assert "evening" in presets
    assert "tomorrow" in presets


def test_preset_to_utc_unknown_raises():
    """Unknown preset raises ValueError."""
    from echo_v2.services.time_presets import preset_to_utc

    with pytest.raises(ValueError, match="unknown time preset"):
        preset_to_utc("bogus", now_utc=NOW, tz_name=TZ)


def test_preset_to_utc_morning_when_already_past():
    """Morning preset when 08:00 has passed → next day."""
    from echo_v2.services.time_presets import preset_to_utc

    # NOW is 13:00 local, so morning (08:00) has passed → tomorrow.
    result = preset_to_utc("morning", now_utc=NOW, tz_name=TZ)
    assert result == datetime(2026, 9, 6, 5, 0, tzinfo=timezone.utc)


def test_preset_to_utc_evening_when_still_ahead():
    """Evening preset (18:00) when still ahead → today."""
    from echo_v2.services.time_presets import preset_to_utc

    # NOW is 13:00 local, evening (18:00) is ahead → today.
    result = preset_to_utc("evening", now_utc=NOW, tz_name=TZ)
    assert result == datetime(2026, 9, 5, 15, 0, tzinfo=timezone.utc)


def test_preset_to_utc_tomorrow():
    """Tomorrow preset → next day at 08:00 local."""
    from echo_v2.services.time_presets import preset_to_utc

    result = preset_to_utc("tomorrow", now_utc=NOW, tz_name=TZ)
    assert result == datetime(2026, 9, 6, 5, 0, tzinfo=timezone.utc)


# --- LLMTimeParser reasoning model -----------------------------------------


async def test_llm_parser_reasoning_model_uses_larger_budget():
    """Reasoning models get max_completion_tokens=1000, no temperature."""
    from unittest.mock import AsyncMock, MagicMock

    from echo_v2.services.time_parser import LLMTimeParser

    future = (NOW + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = f'{{"utc_datetime": "{future}"}}'

    mock_client = MagicMock()
    mock_client.chat = MagicMock()
    mock_client.chat.completions = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

    parser = LLMTimeParser(client=mock_client, model="gpt-5")
    await parser.parse("sometime", user_timezone=TZ, now_utc=NOW)

    call_kwargs = mock_client.chat.completions.create.await_args.kwargs
    assert call_kwargs["max_completion_tokens"] == 1000
    assert "temperature" not in call_kwargs


# --- regex edge cases for branch coverage ----------------------------------


def test_invalid_hour_minute_with_ampm_returns_none():
    """HH:MMpm with invalid hour (e.g. 13:30pm → 25) returns None from _extract_hour_minute."""
    from echo_v2.services.time_parser_regex import _extract_hour_minute

    assert _extract_hour_minute("13:30pm") is None


def test_invalid_hour_with_ampm_returns_none():
    """HHpm with invalid hour (e.g. 13pm → 25) returns None from _extract_hour_minute."""
    from echo_v2.services.time_parser_regex import _extract_hour_minute

    assert _extract_hour_minute("13pm") is None


def test_invalid_bare_hour_returns_none():
    """Bare HH with invalid hour (e.g. 25) returns None from _extract_hour_minute."""
    from echo_v2.services.time_parser_regex import _extract_hour_minute

    assert _extract_hour_minute("25") is None


def test_hebrew_tomorrow_with_invalid_time_falls_through():
    """'מחר ב-25' (invalid time) falls through all patterns and raises TimeParseError."""
    from echo_v2.services.time_parser_regex import TimeParseError

    with pytest.raises(TimeParseError, match="could not parse"):
        parse_time_expression("מחר ב-25", user_timezone=TZ, now_utc=NOW)


def test_english_tomorrow_with_invalid_time_returns_none():
    """'tomorrow at 25' (invalid time) falls through and raises TimeParseError."""
    from echo_v2.services.time_parser_regex import TimeParseError

    with pytest.raises(TimeParseError, match="could not parse"):
        parse_time_expression("tomorrow at 25", user_timezone=TZ, now_utc=NOW)


def test_hebrew_day_name_unknown_day_falls_through():
    """'יום פלאמפלום ב-8' (unknown day) falls through and raises TimeParseError."""
    from echo_v2.services.time_parser_regex import TimeParseError

    with pytest.raises(TimeParseError, match="could not parse"):
        parse_time_expression("יום פלאמפלום ב-8", user_timezone=TZ, now_utc=NOW)


def test_hebrew_day_name_with_invalid_time_falls_through():
    """'יום ראשון ב-25' (invalid time) falls through and raises TimeParseError."""
    from echo_v2.services.time_parser_regex import TimeParseError

    with pytest.raises(TimeParseError, match="could not parse"):
        parse_time_expression("יום ראשון ב-25", user_timezone=TZ, now_utc=NOW)


def test_english_day_name_with_invalid_time_returns_none():
    """'sunday at 25' (invalid time) falls through and raises TimeParseError."""
    from echo_v2.services.time_parser_regex import TimeParseError

    with pytest.raises(TimeParseError, match="could not parse"):
        parse_time_expression("sunday at 25", user_timezone=TZ, now_utc=NOW)
