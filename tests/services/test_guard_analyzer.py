"""Tests for the detection-only Guard analyzer contract."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from echo_v2.services.guard_analyzer import (
    AnalysisError,
    GuardAnalysisInput,
    GuardCategory,
    GuardLLMOutput,
    GuardMessage,
    GuardSignal,
    LLMGuardAnalyzer,
    _parse_llm_output,
)


def _response(content: str, parsed: GuardLLMOutput) -> SimpleNamespace:
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
        messages=(GuardMessage(id="m1", sender="other", text="היי"),),
    )


def _output(**overrides: object) -> GuardLLMOutput:
    values: dict[str, object] = {
        "decision": "none",
        "signals": [],
        "categories": [],
        "evidence_message_ids": [],
        "confidence": 1.0,
        "reason": "ok",
    }
    values.update(overrides)
    return GuardLLMOutput(**values)


def test_guard_output_schema_is_strict_and_detection_only() -> None:
    schema = GuardLLMOutput.model_json_schema()

    assert schema["additionalProperties"] is False
    assert "evidence_message_ids" in schema["required"]
    assert schema["properties"]["evidence_message_ids"]["items"]["type"] == "string"
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
            '"evidence_message_ids":[],"confidence":0.9,"reason":"ok",'
            '"summary":"unexpected"}'
        )


def test_parse_guard_output_deduplicates_signals_and_categories() -> None:
    result = _parse_llm_output(
        '{"decision":"concerning","signals":["secrecy","secrecy"],'
        '"categories":["bullying","bullying"],"evidence_message_ids":[], '
        '"confidence":0.9,"reason":"pattern"}'
    )

    assert result.signals == ("secrecy",)
    assert result.categories == ("bullying",)
    assert result.evidence_message_ids == ()


@pytest.mark.asyncio
async def test_analyzer_uses_pydantic_parse_path_and_evidence_ids() -> None:
    client = MagicMock()
    parsed = _output(evidence_message_ids=["m1"])
    client.chat.completions.parse = AsyncMock(
        return_value=_response(
            '{"decision":"none","signals":[],"categories":[],'
            '"evidence_message_ids":["m1"],"confidence":1.0,"reason":"ok"}',
            parsed,
        )
    )
    analyzer = LLMGuardAnalyzer(client=client, model="gpt-test")

    result, _raw = await analyzer.analyze_with_raw(_conversation())

    assert result.decision == "none"
    assert result.evidence_message_ids == ("m1",)
    assert client.chat.completions.parse.call_args.kwargs["response_format"] is GuardLLMOutput


@pytest.mark.asyncio
async def test_analyzer_rejects_unknown_evidence_ids() -> None:
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(
        return_value=_response("{}", _output(evidence_message_ids=["unknown"]))
    )
    analyzer = LLMGuardAnalyzer(client=client, model="gpt-test", max_retries=0)

    with pytest.raises(AnalysisError, match="unknown evidence"):
        await analyzer.analyze(_conversation())


@pytest.mark.asyncio
async def test_analyzer_retries_after_invalid_parsed_output() -> None:
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(
        side_effect=[
            RuntimeError("temporary API failure"),
            _response("{}", _output(evidence_message_ids=["m1"])),
        ]
    )
    analyzer = LLMGuardAnalyzer(client=client, model="gpt-test", max_retries=1)

    result = await analyzer.analyze(_conversation())

    assert result.evidence_message_ids == ("m1",)
    assert client.chat.completions.parse.await_count == 2


def test_negative_retries_rejected() -> None:
    client = MagicMock()
    with pytest.raises(ValueError, match="max_retries"):
        LLMGuardAnalyzer(client=client, max_retries=-1)
