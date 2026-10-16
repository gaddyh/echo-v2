"""Tests for the isolated Baseline B structured analyzer."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from echo_v2.domain.guard_simple import (
    ChildRole,
    SafetyStatus,
    SimpleConversation,
    SimpleConversationMessage,
)
from echo_v2.services.guard_simple_analyzer import (
    _SYSTEM_PROMPT,
    SimpleAnalysisError,
    SimpleSafetyAnalyzer,
    SimpleSafetyLLMOutput,
    _build_user_message,
)

NOW = datetime(2026, 10, 16, 10, tzinfo=timezone.utc)


def conversation(**overrides: object) -> SimpleConversation:
    values: dict[str, object] = {
        "child_user_id": "child-1",
        "chat_id": "group@g.us",
        "chat_type": "group",
        "connection_id": "connection-1",
        "timezone": "Asia/Jerusalem",
        "window_start": NOW.replace(hour=0),
        "window_end": NOW,
        "monitored_child_role": "child",
        "messages": (
            SimpleConversationMessage("m1", "other", "They keep excluding Shay."),
            SimpleConversationMessage("m2", "child", "Please stop."),
        ),
    }
    values.update(overrides)
    return SimpleConversation(**values)


def output(**overrides: object) -> SimpleSafetyLLMOutput:
    values: dict[str, object] = {
        "status": SafetyStatus.ATTENTION,
        "child_role": ChildRole.TARGET,
        "summary": "The child is being repeatedly targeted.",
        "evidence_message_ids": ["m1", "m2"],
        "reason": "The messages describe repeated targeted behavior.",
    }
    values.update(overrides)
    return SimpleSafetyLLMOutput(**values)


def response(parsed: SimpleSafetyLLMOutput, *, content: str = "{}") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=content, parsed=parsed))
        ],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18),
    )


def test_output_schema_is_strict_and_has_only_simple_fields() -> None:
    schema = SimpleSafetyLLMOutput.model_json_schema()

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "status",
        "child_role",
        "summary",
        "evidence_message_ids",
        "reason",
    }


def test_user_payload_identifies_child_and_preserves_group_context() -> None:
    payload = _build_user_message(conversation())

    assert '"monitored_child":{"user_id":"child-1","role":"child"}' in payload
    assert '"type":"group"' in payload
    assert '"id":"m1"' in payload


def test_prompt_calibrates_urgent_non_physical_risk_and_current_context() -> None:
    prompt = " ".join(_SYSTEM_PROMPT.lower().split())

    assert "does not require physical violence" in prompt
    assert "publish private, intimate, or humiliating photos or videos" in prompt
    assert "meet alone or secretly" in prompt
    assert "explicit imminent self-harm danger" in prompt
    assert "ordinary meetup request" in prompt
    assert "unresolved prior concern is context" in prompt


@pytest.mark.asyncio
async def test_analyzer_returns_structured_assessment_and_usage() -> None:
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(return_value=response(output()))
    analyzer = SimpleSafetyAnalyzer(client=client, model="gpt-test")

    result = await analyzer.analyze_with_raw(conversation())

    assert result.assessment.status is SafetyStatus.ATTENTION
    assert result.assessment.child_role is ChildRole.TARGET
    assert result.assessment.evidence_message_ids == ("m1", "m2")
    assert result.prompt_tokens == 11
    assert result.total_tokens == 18
    assert (
        client.chat.completions.parse.call_args.kwargs["response_format"]
        is SimpleSafetyLLMOutput
    )


@pytest.mark.asyncio
async def test_empty_context_is_uncertain_without_calling_llm() -> None:
    client = MagicMock()
    client.chat.completions.parse = AsyncMock()
    analyzer = SimpleSafetyAnalyzer(client=client)

    result = await analyzer.analyze(conversation(messages=()))

    assert result.status is SafetyStatus.UNCERTAIN
    client.chat.completions.parse.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_evidence_is_rejected() -> None:
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(
        return_value=response(output(evidence_message_ids=["unknown"]))
    )
    analyzer = SimpleSafetyAnalyzer(client=client, max_retries=0)

    with pytest.raises(SimpleAnalysisError, match="unknown evidence"):
        await analyzer.analyze(conversation())


@pytest.mark.asyncio
async def test_actionable_output_requires_evidence() -> None:
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(
        return_value=response(output(evidence_message_ids=[]))
    )
    analyzer = SimpleSafetyAnalyzer(client=client, max_retries=0)

    with pytest.raises(SimpleAnalysisError, match="must cite evidence"):
        await analyzer.analyze(conversation())


@pytest.mark.asyncio
async def test_safe_output_can_have_no_evidence() -> None:
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(
        return_value=response(
            output(
                status=SafetyStatus.SAFE,
                child_role=ChildRole.PARTICIPANT,
                evidence_message_ids=[],
                summary="No actionable concern found.",
            )
        )
    )
    analyzer = SimpleSafetyAnalyzer(client=client, max_retries=0)

    result = await analyzer.analyze(conversation())

    assert result.status is SafetyStatus.SAFE
    assert result.evidence_message_ids == ()


@pytest.mark.asyncio
async def test_analyzer_rejects_negative_retries() -> None:
    with pytest.raises(ValueError, match="max_retries"):
        SimpleSafetyAnalyzer(client=MagicMock(), max_retries=-1)


@pytest.mark.asyncio
async def test_reasoning_model_uses_reasoning_token_parameter() -> None:
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(return_value=response(output()))
    analyzer = SimpleSafetyAnalyzer(client=client, model="gpt-5-mini", max_retries=0)

    await analyzer.analyze(conversation())

    kwargs = client.chat.completions.parse.call_args.kwargs
    assert kwargs["max_completion_tokens"] == 1200
    assert "temperature" not in kwargs


@pytest.mark.asyncio
async def test_analyzer_retries_provider_failure() -> None:
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(
        side_effect=[RuntimeError("temporary"), response(output())]
    )
    analyzer = SimpleSafetyAnalyzer(client=client, max_retries=1)

    result = await analyzer.analyze(conversation())

    assert result.status is SafetyStatus.ATTENTION
    assert client.chat.completions.parse.await_count == 2


@pytest.mark.asyncio
async def test_analyzer_rejects_missing_parsed_output() -> None:
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="{}", parsed=None))],
            usage=None,
        )
    )
    analyzer = SimpleSafetyAnalyzer(client=client, max_retries=0)

    with pytest.raises(SimpleAnalysisError, match="no parsed"):
        await analyzer.analyze(conversation())
