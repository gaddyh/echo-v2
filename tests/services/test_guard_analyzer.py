"""Tests for the Guard analyzer output contract."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from echo_v2.services.guard_analyzer import (
    AnalysisError,
    GuardAnalysisInput,
    GuardCategory,
    GuardLLMOutput,
    GuardSignal,
    LLMGuardAnalyzer,
    _parse_llm_output,
)


def _response(content: str, parsed: GuardLLMOutput | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, parsed=parsed)
            )
        ]
    )


def _conversation() -> GuardAnalysisInput:
    return GuardAnalysisInput(
        child_id="child-1",
        chat_id="chat-1",
        messages=[("other", "היי")],
    )


def test_guard_output_schema_is_strict_and_detection_only() -> None:
    schema = GuardLLMOutput.model_json_schema()

    assert schema["additionalProperties"] is False
    assert schema["properties"]["decision"]["enum"] == [
        "none",
        "watch",
        "concerning",
        "urgent",
    ]
    assert schema["$defs"]["GuardSignal"]["enum"] == [
        signal.value for signal in GuardSignal
    ]
    assert schema["$defs"]["GuardCategory"]["enum"] == [
        category.value for category in GuardCategory
    ]


def test_parse_guard_output_rejects_unknown_fields() -> None:
    with pytest.raises(AnalysisError, match="Invalid Guard output"):
        _parse_llm_output(
            '{"decision":"none","signals":[],"categories":[],'
            '"confidence":0.9,"reason":"ok",'
            '"summary":"unexpected"}'
        )


def test_parse_guard_output_deduplicates_signals_and_categories() -> None:
    result = _parse_llm_output(
        '{"decision":"concerning","signals":["secrecy","secrecy"],'
        '"categories":["bullying","bullying"],'
        '"confidence":0.9,"reason":"pattern"}'
    )

    assert result.signals == ("secrecy",)
    assert result.categories == ("bullying",)


@pytest.mark.asyncio
async def test_analyzer_requests_strict_json_schema() -> None:
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(
        return_value=_response(
            '{"decision":"none","signals":[],"categories":[],'
            '"confidence":1.0,"reason":"ok"}',
            parsed=GuardLLMOutput(
                decision="none",
                signals=[],
                categories=[],
                confidence=1.0,
                reason="ok",
            ),
        )
    )
    analyzer = LLMGuardAnalyzer(client=client, model="gpt-test")

    result, _raw = await analyzer.analyze_with_raw(_conversation())

    assert result.decision == "none"
    request = client.chat.completions.parse.call_args.kwargs
    assert request["response_format"] is GuardLLMOutput
