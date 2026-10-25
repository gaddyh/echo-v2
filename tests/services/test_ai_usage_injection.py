"""Per-analyzer usage injection tests — verify the recorder is called at the call site."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from echo_v2.domain.guard_simple import (
    ChildRole,
    SimpleConversation,
    SimpleConversationMessage,
)
from echo_v2.services.guard_simple_analyzer import (
    SimpleSafetyAnalyzer,
    SimpleSafetyLLMOutput,
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


@pytest.mark.asyncio
async def test_simple_analyzer_records_usage_on_success() -> None:
    """A successful analyzer call records usage with the recorder."""
    recorder = AsyncMock()
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(return_value=response(output()))
    analyzer = SimpleSafetyAnalyzer(client=client, model="gpt-test", recorder=recorder)

    await analyzer.analyze_with_raw(conversation())

    recorder.record.assert_awaited_once()
    ctx, payload = recorder.record.call_args[0]
    assert ctx.operation == "guard_security"
    assert ctx.provider == "openai"
    assert ctx.model == "gpt-test"
    assert ctx.child_user_id == "child-1"
    assert payload.status == "success"
    assert payload.prompt_tokens == 11
    assert payload.completion_tokens == 7
    assert payload.total_tokens == 18
    assert payload.invocation_id is not None
    assert isinstance(payload.invocation_id, UUID)


@pytest.mark.asyncio
async def test_simple_analyzer_records_usage_on_provider_error() -> None:
    """A provider error still records a usage event with error status."""
    recorder = AsyncMock()
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(
        side_effect=RuntimeError("API connection lost")
    )
    analyzer = SimpleSafetyAnalyzer(client=client, model="gpt-test", recorder=recorder)

    from echo_v2.services.guard_simple_analyzer import SimpleAnalysisError

    with pytest.raises(SimpleAnalysisError, match="API connection lost"):
        await analyzer.analyze_with_raw(conversation())

    # Default max_retries=1 → 2 attempts, both error.
    assert recorder.record.await_count == 2
    # Both calls have error status.
    for call in recorder.record.call_args_list:
        assert call.args[1].status == "error"
        assert call.args[1].error_type == "RuntimeError"


@pytest.mark.asyncio
async def test_simple_analyzer_records_separate_invocation_per_retry() -> None:
    """Each provider retry gets a fresh invocation_id (separate billable attempt)."""
    recorder = AsyncMock()
    client = MagicMock()
    # First call fails, second succeeds.
    client.chat.completions.parse = AsyncMock(
        side_effect=[RuntimeError("transient"), response(output())]
    )
    analyzer = SimpleSafetyAnalyzer(
        client=client, model="gpt-test", recorder=recorder, max_retries=1
    )

    await analyzer.analyze_with_raw(conversation())

    # Two provider attempts → two recorder calls with different invocation_ids.
    assert recorder.record.await_count == 2
    inv_ids = [
        call.args[1].invocation_id for call in recorder.record.call_args_list
    ]
    assert inv_ids[0] != inv_ids[1]
    # First attempt is error, second is success.
    assert recorder.record.call_args_list[0].args[1].status == "error"
    assert recorder.record.call_args_list[1].args[1].status == "success"


@pytest.mark.asyncio
async def test_simple_analyzer_succeeds_when_recorder_persistence_fails() -> None:
    """Analysis succeeds even when usage persistence fails.

    This is the core requirement: the recorder's persistence failure must not
    break the analysis. The real AiUsageRecorder swallows persistence errors
    internally and surfaces them via error_count. We use a real recorder with
    a failing repo to verify the full contract.
    """
    from echo_v2.services.reporting.ai_usage_recorder import AiUsageRecorder
    from echo_v2.services.reporting.pricing_service import PricingService

    repo = AsyncMock()
    repo.record = AsyncMock(side_effect=RuntimeError("db down"))
    recorder = AiUsageRecorder(repo=repo, pricing=PricingService())
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(return_value=response(output()))
    analyzer = SimpleSafetyAnalyzer(client=client, model="gpt-test", recorder=recorder)

    # Must not raise despite recorder persistence failure.
    result = await analyzer.analyze_with_raw(conversation())
    assert result.assessment.parent_should_know is True
    # The failure is visible operationally via error_count.
    assert recorder.error_count == 1
    assert recorder.recorded_count == 0


@pytest.mark.asyncio
async def test_simple_analyzer_without_recorder_still_works() -> None:
    """When recorder is None (disabled), the analyzer works normally."""
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(return_value=response(output()))
    analyzer = SimpleSafetyAnalyzer(client=client, model="gpt-test")  # no recorder

    result = await analyzer.analyze_with_raw(conversation())
    assert result.assessment.parent_should_know is True
