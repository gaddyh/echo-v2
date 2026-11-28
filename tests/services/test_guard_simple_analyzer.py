"""Tests for the isolated Baseline B structured analyzer."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from echo_v2.domain.guard_simple import (
    ChildRole,
    PriorConcern,
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
        "child_name": "Shay",
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
        "parent_should_know": True,
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
        "parent_should_know",
        "child_role",
        "summary",
        "evidence_message_ids",
        "reason",
    }
    assert schema["properties"]["parent_should_know"]["anyOf"] == [
        {"type": "boolean"},
        {"type": "null"},
    ]


def test_user_payload_identifies_child_and_preserves_group_context() -> None:
    payload = _build_user_message(conversation())

    assert '"monitored_child":{"user_id":"child-1","name":"Shay","role":"child"}' in payload
    assert '"type":"group"' in payload
    assert '"id":"m1"' in payload


def test_user_payload_serializes_uncertain_prior_context_as_null() -> None:
    prior = PriorConcern(
        parent_should_know=None,
        child_role=ChildRole.PARTICIPANT,
        summary="Ambiguous object exchange.",
        reason="The evidence was insufficient.",
        evidence_message_ids=("old-1",),
        created_at=NOW,
        first_detected_at=NOW,
        last_evidence_at=None,
    )

    payload = _build_user_message(conversation(prior_concern=prior))

    assert '"unresolved_prior_concern":{"parent_should_know":null' in payload
    assert '"evidence_message_ids":["old-1"]' in payload


def test_prompt_calibrates_urgent_non_physical_risk_and_current_context() -> None:
    prompt = " ".join(_SYSTEM_PROMPT.lower().split())

    assert "parent_should_know = true, false, or null" in prompt
    assert "false does not require positive proof of a benign explanation" in prompt
    assert "a plausible harmful interpretation is not sufficient evidence of harm" in prompt
    assert "quantities, prices, arrival times, or exchanging objects" in prompt
    assert "specific, realistic safety hypothesis" in prompt
    assert "specific, realistic dangerous interpretation remains unresolved" in prompt
    assert "ordinary object or a dangerous object or substance" in prompt
    assert "exact identity of the object or activity remains uncertain" in prompt
    assert "prior null value means the earlier evidence was uncertain" in prompt
    assert "merely being addressed in a benign or unclear message" in prompt
    assert "do not require multiple independent signals" in prompt
    assert "configured name" in prompt
    assert "publish private or humiliating material" in prompt
    assert "ordinary social activity" in prompt
    assert "internal unknown outcome" in prompt
    assert "unresolved prior concern is context" in prompt


@pytest.mark.asyncio
async def test_analyzer_returns_structured_assessment_and_usage() -> None:
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(return_value=response(output()))
    analyzer = SimpleSafetyAnalyzer(client=client, model="gpt-test")

    result = await analyzer.analyze_with_raw(conversation())

    assert result.assessment.parent_should_know is True
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

    assert result.parent_should_know is None
    client.chat.completions.parse.assert_not_awaited()


def test_incomplete_media_evidence_true_for_retryable_statuses() -> None:
    """Pending/processing/failed enrichments are incomplete — media might still arrive."""
    for status in ("pending", "processing", "failed"):
        conv = conversation(messages=(
            SimpleConversationMessage("m1", "other", "text", enrichment_status=status),
        ))
        assert conv.incomplete_media_evidence is True, f"status={status}"


def test_incomplete_media_evidence_false_for_terminal() -> None:
    """Terminal enrichment (media fundamentally unavailable) is not incomplete —
    retrying won't help and the analyzer should proceed with available text."""
    conv = conversation(messages=(
        SimpleConversationMessage(
            "m1", "other", "a video caption",
            enrichment_status="terminal",
            enrichment_error="media reference and download URL are unavailable",
        ),
    ))
    assert conv.incomplete_media_evidence is False


def test_incomplete_media_evidence_false_for_completed_and_none() -> None:
    """Completed and no-enrichment messages are not incomplete."""
    for status in (None, "completed"):
        conv = conversation(messages=(
            SimpleConversationMessage("m1", "other", "text", enrichment_status=status),
        ))
        assert conv.incomplete_media_evidence is False, f"status={status}"


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
                parent_should_know=False,
                child_role=ChildRole.PARTICIPANT,
                evidence_message_ids=[],
                summary="No actionable concern found.",
            )
        )
    )
    analyzer = SimpleSafetyAnalyzer(client=client, max_retries=0)

    result = await analyzer.analyze(conversation())

    assert result.parent_should_know is False
    assert result.evidence_message_ids == ()


@pytest.mark.asyncio
async def test_uncertain_output_is_valid_and_may_cite_supporting_evidence() -> None:
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(
        return_value=response(
            output(
                parent_should_know=None,
                child_role=ChildRole.UNKNOWN,
                evidence_message_ids=["m1"],
                summary="The available evidence is ambiguous.",
                reason="A potentially meaningful concern is not sufficiently supported.",
            )
        )
    )
    analyzer = SimpleSafetyAnalyzer(client=client, max_retries=0)

    result = await analyzer.analyze(conversation())

    assert result.parent_should_know is None
    assert result.evidence_message_ids == ("m1",)


def test_parent_should_know_is_required_even_though_nullable() -> None:
    with pytest.raises(ValueError, match="parent_should_know"):
        SimpleSafetyLLMOutput.model_validate(
            {
                "child_role": "unknown",
                "summary": "Ambiguous.",
                "evidence_message_ids": [],
                "reason": "Insufficient evidence.",
            }
        )


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

    assert result.parent_should_know is True
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
