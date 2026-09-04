"""Time expression parsing for the scheduling flow.

Two-layer strategy:
1. **Regex parser** (:func:`parse_time_expression`) — handles common
   patterns (``ב-8``, ``מחר ב-8``, ``20:00``, ``8pm``, ``בעוד שעה``,
   day names, explicit datetimes). Free, instant, no external dependency.
2. **LLM fallback** (:class:`LLMTimeParser`) — handles the long tail of
   natural Hebrew/English expressions the regex can't match. Uses the
   OpenAI API with a structured-output prompt.

:class:`CombinedTimeParser` tries regex first; if it raises
:class:`TimeParseError`, falls back to the LLM. If the LLM also fails,
the error propagates and the flow service asks the user to rephrase.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from echo_v2.services.time_parser_regex import TimeParseError, parse_time_expression

__all__ = [
    "CombinedTimeParser",
    "LLMTimeParser",
    "TimeParseError",
    "TimeParser",
]

_logger = logging.getLogger("echo_v2.services.time_parser")


@runtime_checkable
class TimeParser(Protocol):
    """Parse a time expression into a timezone-aware UTC datetime."""

    async def parse(
        self,
        text: str,
        *,
        user_timezone: str,
        now_utc: datetime | None = None,
    ) -> datetime: ...


class LLMTimeParser:
    """LLM-based time expression parser using the OpenAI API.

    The prompt gives the LLM the user's timezone, the current UTC time,
    and the expression. It must return a JSON object with a single
    ``utc_datetime`` field in ISO 8601 format. The parser validates the
    output and raises :class:`TimeParseError` on any problem.
    """

    SYSTEM_PROMPT = (
        "You are a time expression parser. Given a natural-language time "
        "expression (Hebrew or English), the user's IANA timezone, and the "
        "current UTC time, output the resolved time as a UTC datetime.\n\n"
        "Rules:\n"
        "- The output must be in the future relative to now_utc.\n"
        "- If the expression refers to a time today that has already passed, "
        "interpret it as tomorrow.\n"
        "- Return ONLY a JSON object: {\"utc_datetime\": \"<ISO 8601 UTC>\"}\n"
        "- The datetime must end with 'Z' or '+00:00' to indicate UTC.\n"
        "- Do not include any explanation, only the JSON object.\n"
    )

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gpt-4.1",
    ) -> None:
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._model = model

    async def parse(
        self,
        text: str,
        *,
        user_timezone: str,
        now_utc: datetime | None = None,
    ) -> datetime:
        if not self._api_key:
            raise TimeParseError("OPENAI_API_KEY not configured for LLM time parser")

        now = now_utc or datetime.now(timezone.utc)
        user_msg = (
            f'Expression: "{text}"\n'
            f"User timezone: {user_timezone}\n"
            f"Now (UTC): {now.isoformat()}"
        )

        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=self._api_key)
        try:
            response = await client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0,
                max_tokens=100,
            )
        except Exception as exc:
            _logger.warning("LLM time parser API error: %s", exc)
            raise TimeParseError(f"LLM time parser request failed: {exc}") from exc
        finally:
            await client.close()

        raw_output = response.choices[0].message.content or ""
        return _parse_llm_output(raw_output, now)


class CombinedTimeParser:
    """Regex-first, LLM-fallback time parser.

    Tries the deterministic regex parser first (free, instant). If it
    can't parse the expression, falls back to the LLM parser. If both
    fail, raises :class:`TimeParseError`.
    """

    def __init__(self, llm_parser: LLMTimeParser | None = None) -> None:
        self._llm = llm_parser

    async def parse(
        self,
        text: str,
        *,
        user_timezone: str,
        now_utc: datetime | None = None,
    ) -> datetime:
        # Layer 1: regex (synchronous, instant, free).
        try:
            return parse_time_expression(
                text, user_timezone=user_timezone, now_utc=now_utc
            )
        except TimeParseError:
            pass  # Fall through to LLM.

        # Layer 2: LLM fallback.
        if self._llm is None:
            raise TimeParseError(
                f"could not parse time expression {text!r} "
                "(regex failed, no LLM fallback configured)"
            )

        _logger.info("regex parser failed for %r, falling back to LLM", text)
        return await self._llm.parse(
            text, user_timezone=user_timezone, now_utc=now_utc
        )


def _parse_llm_output(raw: str, now: datetime) -> datetime:
    """Parse and validate the LLM's JSON output."""
    raw = raw.strip()
    # Strip markdown code fences if present.
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1] if "\n" in raw else raw[3:]
        raw = raw.removesuffix("```")
        raw = raw.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TimeParseError(f"LLM output is not valid JSON: {raw!r}") from exc

    if not isinstance(data, dict) or "utc_datetime" not in data:
        raise TimeParseError(f"LLM output missing 'utc_datetime' key: {raw!r}")

    dt_str = str(data["utc_datetime"])
    # Normalize 'Z' suffix to '+00:00' for fromisoformat.
    if dt_str.endswith("Z"):
        dt_str = dt_str[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(dt_str)
    except ValueError as exc:
        raise TimeParseError(f"LLM output has invalid datetime: {dt_str!r}") from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    if parsed <= now:
        raise TimeParseError(
            f"LLM returned a past time: {parsed.isoformat()} (now={now.isoformat()})"
        )

    return parsed.astimezone(timezone.utc)
