"""Tests for the independent group-level Exposure Guard analyzer."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from echo_v2.domain.guard_exposure import ExposureCategory
from echo_v2.domain.guard_simple import SimpleConversation, SimpleConversationMessage
from echo_v2.services.guard_exposure_analyzer import (
    _SYSTEM_PROMPT,
    ExposureAnalysisError,
    ExposureAnalyzer,
    ExposureLLMOutput,
)

NOW = datetime(2026, 10, 18, 10, tzinfo=timezone.utc)


def conversation(**overrides: object) -> SimpleConversation:
    values: dict[str, object] = {
        "child_user_id": "child",
        "child_name": "Shay",
        "chat_id": "class@g.us",
        "chat_type": "group",
        "connection_id": "connection",
        "timezone": "Asia/Jerusalem",
        "window_start": NOW.replace(hour=0),
        "window_end": NOW,
        "monitored_child_role": "child",
        "messages": (
            SimpleConversationMessage("m17", "other", "Nobody wants Daniel here."),
            SimpleConversationMessage("m21", "other_2", "Daniel is a loser."),
            SimpleConversationMessage("m24", "other", "Stop asking us to stop."),
        ),
    }
    values.update(overrides)
    return SimpleConversation(**values)


def output(**overrides: object) -> ExposureLLMOutput:
    values: dict[str, object] = {
        "significant_exposure": True,
        "category": ExposureCategory.BULLYING,
        "summary": "Repeated targeted humiliation of another participant.",
        "evidence_message_ids": ["m17", "m21", "m24"],
        "reason": "Targeted humiliation continues despite requests to stop.",
    }
    values.update(overrides)
    return ExposureLLMOutput(**values)


def response(parsed: ExposureLLMOutput) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="{}", parsed=parsed))],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18),
    )


def test_schema_and_prompt_define_independent_exposure_contract() -> None:
    schema = ExposureLLMOutput.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "significant_exposure", "category", "summary", "evidence_message_ids", "reason",
    }
    prompt = " ".join(_SYSTEM_PROMPT.lower().split())
    assert "independently of whether the monitored child is personally harmed" in prompt
    assert "ordinary arguments" in prompt
    assert "missing media could change a negative conclusion" in prompt


@pytest.mark.asyncio
async def test_analyzer_returns_category_and_evidence() -> None:
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(return_value=response(output()))
    result = await ExposureAnalyzer(client=client).analyze(conversation())
    assert result.significant_exposure is True
    assert result.category is ExposureCategory.BULLYING
    assert result.evidence_message_ids == ("m17", "m21", "m24")


@pytest.mark.asyncio
async def test_missing_media_cannot_be_confident_no() -> None:
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(
        return_value=response(output(
            significant_exposure=False,
            category=None,
            evidence_message_ids=[],
            summary="No significant event found.",
        ))
    )
    result = await ExposureAnalyzer(client=client, max_retries=0).analyze(
        conversation(messages=(SimpleConversationMessage("m1", "other", "A video was sent.", enrichment_status="failed"),))
    )
    assert result.significant_exposure is None
    assert result.reason == "incomplete_media_evidence_could_change_negative"


@pytest.mark.asyncio
async def test_empty_context_is_unknown_without_llm_call() -> None:
    client = MagicMock()
    client.chat.completions.parse = AsyncMock()
    result = await ExposureAnalyzer(client).analyze(conversation(messages=()))
    assert result.significant_exposure is None
    client.chat.completions.parse.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_result_can_be_confident_when_media_is_complete() -> None:
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(return_value=response(output(
        significant_exposure=False, category=None, evidence_message_ids=[],
        summary="A normal disagreement.", reason="No meaningful event is present.",
    )))
    result = await ExposureAnalyzer(client, max_retries=0).analyze(conversation())
    assert result.significant_exposure is False
    assert result.category is None
    assert result.evidence_message_ids == ()


@pytest.mark.asyncio
async def test_analyzer_retries_and_supports_reasoning_models() -> None:
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(side_effect=[RuntimeError("temporary"), response(output())])
    analyzer = ExposureAnalyzer(client, model="gpt-5-mini", max_retries=1)
    result = await analyzer.analyze(conversation())
    assert result.significant_exposure is True
    assert client.chat.completions.parse.await_count == 2
    assert client.chat.completions.parse.call_args.kwargs["max_completion_tokens"] == 1200


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_provider_failure_is_reported_after_retries() -> None:
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(side_effect=RuntimeError("down"))
    with pytest.raises(ExposureAnalysisError, match="request failed"):
        await ExposureAnalyzer(client, max_retries=0).analyze(conversation())


@pytest.mark.asyncio
async def test_missing_parsed_output_and_negative_retries_are_rejected() -> None:
    with pytest.raises(ValueError, match="max_retries"):
        ExposureAnalyzer(MagicMock(), max_retries=-1)
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="{}", parsed=None))], usage=None,
    ))
    with pytest.raises(ExposureAnalysisError, match="no parsed"):
        await ExposureAnalyzer(client, max_retries=0).analyze(conversation())


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_true_output_requires_category_and_unique_evidence() -> None:
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(return_value=response(output(category=None)))
    with pytest.raises(ExposureAnalysisError, match="category"):
        await ExposureAnalyzer(client, max_retries=0).analyze(conversation())
    client.chat.completions.parse = AsyncMock(return_value=response(output(evidence_message_ids=["m17", "m17"])))
    with pytest.raises(ExposureAnalysisError, match="duplicate"):
        await ExposureAnalyzer(client, max_retries=0).analyze(conversation())


@pytest.mark.asyncio
async def test_unknown_evidence_is_rejected() -> None:
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(return_value=response(output(evidence_message_ids=["missing"])))
    with pytest.raises(ExposureAnalysisError, match="unknown evidence"):
        await ExposureAnalyzer(client, max_retries=0).analyze(conversation())
