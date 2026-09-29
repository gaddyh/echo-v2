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


def _response(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _conversation() -> GuardAnalysisInput:
    return GuardAnalysisInput(
        child_id="child-1",
        chat_id="chat-1",
        messages=[("other", "היי")],
    )


def test_guard_output_schema_is_strict_and_requires_alert() -> None:
    schema = GuardLLMOutput.model_json_schema()

    assert schema["additionalProperties"] is False
    assert "should_alert" in schema["required"]
    assert schema["properties"]["should_alert"]["type"] == "boolean"
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


def test_parse_guard_output_rejects_string_boolean() -> None:
    with pytest.raises(AnalysisError, match="Invalid Guard output"):
        _parse_llm_output(
            '{"decision":"none","signals":[],"categories":[],'
            '"should_alert":"false","confidence":0.9,"reason":"ok"}'
        )


def test_parse_guard_output_rejects_unknown_fields() -> None:
    with pytest.raises(AnalysisError, match="Invalid Guard output"):
        _parse_llm_output(
            '{"decision":"none","signals":[],"categories":[],'
            '"should_alert":false,"confidence":0.9,"reason":"ok",'
            '"summary":"unexpected"}'
        )


def test_parse_guard_output_deduplicates_signals_and_categories() -> None:
    result = _parse_llm_output(
        '{"decision":"concerning","signals":["secrecy","secrecy"],'
        '"categories":["bullying","bullying"],"should_alert":true,'
        '"confidence":0.9,"reason":"pattern"}'
    )

    assert result.signals == ("secrecy",)
    assert result.categories == ("bullying",)
    assert result.should_alert is True


@pytest.mark.asyncio
async def test_analyzer_requests_strict_json_schema() -> None:
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=_response(
            '{"decision":"none","signals":[],"categories":[],'
            '"should_alert":false,"confidence":1.0,"reason":"ok"}'
        )
    )
    analyzer = LLMGuardAnalyzer(client=client, model="gpt-test")

    result, _raw = await analyzer.analyze_with_raw(_conversation())

    assert result.decision == "none"
    assert result.should_alert is False
    request = client.chat.completions.create.call_args.kwargs
    response_format = request["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"]["additionalProperties"] is False
