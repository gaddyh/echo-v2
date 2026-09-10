"""Tests for the time parser (regex layer + combined parser)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

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


async def test_llm_parser_no_api_key_raises(monkeypatch):
    """LLMTimeParser without an API key raises TimeParseError."""
    from echo_v2.services.time_parser import LLMTimeParser

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    parser = LLMTimeParser(api_key="")
    with pytest.raises(TimeParseError, match="OPENAI_API_KEY not configured"):
        await parser.parse("מחר", user_timezone=TZ, now_utc=NOW)


def test_llm_parser_uses_env_api_key():
    """LLMTimeParser reads OPENAI_API_KEY from env if not passed."""
    from echo_v2.services.time_parser import LLMTimeParser

    old = os.environ.get("OPENAI_API_KEY")
    os.environ["OPENAI_API_KEY"] = "test-key"
    try:
        parser = LLMTimeParser()
        assert parser._api_key == "test-key"
    finally:
        if old is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = old


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
    mock_client.close = AsyncMock()

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        parser = LLMTimeParser(api_key="test-key")
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
    mock_client.close = AsyncMock()

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        parser = LLMTimeParser(api_key="test-key")
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
    mock_client.close = AsyncMock()

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        llm = LLMTimeParser(api_key="test-key")
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
    mock_client.close = AsyncMock()

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        llm = LLMTimeParser(api_key="test-key")
        parser = CombinedTimeParser(llm_parser=llm)
        result = await parser.parse("מחר ב-8", user_timezone=TZ, now_utc=NOW)
    # Regex handled it — LLM was not called.
    mock_client.chat.completions.create.assert_not_called()
    assert result == datetime(2026, 9, 6, 5, 0, tzinfo=timezone.utc)
