"""AiUsageRecorder unit tests — persistence, cost-source, and failure behavior."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from echo_v2.domain.ai_usage_event import AiCostSource, AiUsageStatus, AiUsageType
from echo_v2.services.reporting.ai_usage_recorder import (
    AiUsageRecorder,
    UsageContext,
    UsagePayload,
)
from echo_v2.services.reporting.pricing_service import PricingService

pytestmark = pytest.mark.asyncio


def _make_recorder(repo: AsyncMock | None = None) -> AiUsageRecorder:
    return AiUsageRecorder(
        repo=repo or AsyncMock(),
        pricing=PricingService(),
    )


async def test_record_token_success_measured_usage_priced() -> None:
    """A successful OpenAI token call → measured_usage_priced, persisted."""
    repo = AsyncMock()
    recorder = _make_recorder(repo)
    ctx = UsageContext(
        operation="guard_security",
        provider="openai",
        model="gpt-5.4-mini",
        child_user_id="child-1",
    )
    payload = UsagePayload(
        usage_type=AiUsageType.TOKENS,
        status=AiUsageStatus.SUCCESS,
        prompt_tokens=1000,
        completion_tokens=200,
        invocation_id=uuid4(),
    )
    await recorder.record(ctx, payload)
    repo.record.assert_awaited_once()
    event = repo.record.call_args[0][0]
    assert event.cost_source == AiCostSource.MEASURED_USAGE_PRICED
    assert event.estimated_cost_usd is not None
    assert event.status == AiUsageStatus.SUCCESS
    assert event.child_user_id == "child-1"
    assert event.prompt_tokens == 1000
    assert event.completion_tokens == 200
    assert recorder.recorded_count == 1
    assert recorder.error_count == 0


async def test_record_token_error_still_persisted() -> None:
    """A failed billable attempt is still persisted with error status."""
    repo = AsyncMock()
    recorder = _make_recorder(repo)
    ctx = UsageContext(
        operation="guard_security",
        provider="openai",
        model="gpt-5.4-mini",
    )
    payload = UsagePayload(
        usage_type=AiUsageType.TOKENS,
        status=AiUsageStatus.ERROR,
        error_type="APIConnectionError",
        invocation_id=uuid4(),
    )
    await recorder.record(ctx, payload)
    repo.record.assert_awaited_once()
    event = repo.record.call_args[0][0]
    assert event.status == AiUsageStatus.ERROR
    assert event.error_type == "APIConnectionError"
    # No usage → fallback estimate (not unknown, since fallback assumptions exist)
    assert event.cost_source == AiCostSource.FALLBACK_ESTIMATE
    assert event.estimated_cost_usd is not None


async def test_record_audio_fallback_estimate() -> None:
    """Modal audio → fallback_estimate, audio duration stored in usage_details."""
    repo = AsyncMock()
    recorder = _make_recorder(repo)
    ctx = UsageContext(
        operation="media_audio",
        provider="modal",
        model="whisper-large-v3",
        child_user_id="child-1",
    )
    payload = UsagePayload(
        usage_type=AiUsageType.AUDIO_SECONDS,
        status=AiUsageStatus.SUCCESS,
        usage_details={
            "audio_duration_seconds": 120.0,
            "processing_seconds": 5.0,
        },
        invocation_id=uuid4(),
    )
    await recorder.record(ctx, payload)
    event = repo.record.call_args[0][0]
    assert event.cost_source == AiCostSource.FALLBACK_ESTIMATE
    assert event.estimated_cost_usd == Decimal("0.012")
    assert event.usage_details["audio_duration_seconds"] == 120.0
    assert event.usage_details["processing_seconds"] == 5.0


async def test_record_audio_no_duration_unknown() -> None:
    """Modal audio with no duration → unknown cost."""
    repo = AsyncMock()
    recorder = _make_recorder(repo)
    ctx = UsageContext(
        operation="media_audio",
        provider="modal",
        model="whisper-large-v3",
    )
    payload = UsagePayload(
        usage_type=AiUsageType.AUDIO_SECONDS,
        status=AiUsageStatus.ERROR,
        error_type="TranscriptionError",
        invocation_id=uuid4(),
    )
    await recorder.record(ctx, payload)
    event = repo.record.call_args[0][0]
    assert event.cost_source == AiCostSource.UNKNOWN
    assert event.estimated_cost_usd is None


async def test_persistence_failure_does_not_raise() -> None:
    """Recorder persistence failure is swallowed, analysis path is not broken."""
    repo = AsyncMock()
    repo.record = AsyncMock(side_effect=RuntimeError("db connection lost"))
    recorder = _make_recorder(repo)
    ctx = UsageContext(
        operation="guard_security",
        provider="openai",
        model="gpt-5.4-mini",
    )
    payload = UsagePayload(
        usage_type=AiUsageType.TOKENS,
        status=AiUsageStatus.SUCCESS,
        prompt_tokens=1000,
        completion_tokens=200,
        invocation_id=uuid4(),
    )
    # Must NOT raise.
    await recorder.record(ctx, payload)
    assert recorder.error_count == 1
    assert recorder.recorded_count == 0


async def test_persistence_failure_visible_operationally() -> None:
    """The persistence failure is visible via error_count, not silently zero."""
    repo = AsyncMock()
    repo.record = AsyncMock(side_effect=RuntimeError("db down"))
    recorder = _make_recorder(repo)
    ctx = UsageContext(
        operation="guard_security",
        provider="openai",
        model="gpt-5.4-mini",
    )
    payload = UsagePayload(
        usage_type=AiUsageType.TOKENS,
        status=AiUsageStatus.SUCCESS,
        prompt_tokens=1000,
        completion_tokens=200,
        invocation_id=uuid4(),
    )
    await recorder.record(ctx, payload)
    # The operational signal is error_count > 0, not a silent zero cost.
    assert recorder.error_count == 1
    assert recorder.recorded_count == 0


async def test_analysis_succeeds_while_persistence_fails() -> None:
    """The core requirement: AI analysis succeeds even when usage persistence fails.

    This simulates the real-world scenario: an analyzer calls the recorder,
    the recorder's DB is down, but the analyzer's own result is still returned
    to the caller. The persistence failure is visible operationally via
    error_count but does not propagate.
    """
    repo = AsyncMock()
    repo.record = AsyncMock(side_effect=RuntimeError("db connection lost"))
    recorder = _make_recorder(repo)

    # Simulate an analyzer that records usage then returns its result.
    async def analyze() -> str:
        ctx = UsageContext(
            operation="guard_security",
            provider="openai",
            model="gpt-5.4-mini",
            child_user_id="child-1",
        )
        payload = UsagePayload(
            usage_type=AiUsageType.TOKENS,
            status=AiUsageStatus.SUCCESS,
            prompt_tokens=1000,
            completion_tokens=200,
            invocation_id=uuid4(),
        )
        await recorder.record(ctx, payload)  # must not raise
        return "analysis_result"

    result = await analyze()
    assert result == "analysis_result"
    assert recorder.error_count == 1
    assert recorder.recorded_count == 0


async def test_invocation_id_generated_when_not_provided() -> None:
    """When invocation_id is None, the recorder generates one."""
    repo = AsyncMock()
    recorder = _make_recorder(repo)
    ctx = UsageContext(
        operation="guard_security",
        provider="openai",
        model="gpt-5.4-mini",
    )
    payload = UsagePayload(
        usage_type=AiUsageType.TOKENS,
        status=AiUsageStatus.SUCCESS,
        prompt_tokens=1000,
        completion_tokens=200,
        # invocation_id not set
    )
    await recorder.record(ctx, payload)
    event = repo.record.call_args[0][0]
    assert event.invocation_id is not None


async def test_invocation_id_preserved_when_provided() -> None:
    """When invocation_id is provided, the recorder uses it (idempotency)."""
    repo = AsyncMock()
    recorder = _make_recorder(repo)
    inv_id = uuid4()
    ctx = UsageContext(
        operation="guard_security",
        provider="openai",
        model="gpt-5.4-mini",
    )
    payload = UsagePayload(
        usage_type=AiUsageType.TOKENS,
        status=AiUsageStatus.SUCCESS,
        prompt_tokens=1000,
        completion_tokens=200,
        invocation_id=inv_id,
    )
    await recorder.record(ctx, payload)
    event = repo.record.call_args[0][0]
    assert event.invocation_id == inv_id


async def test_price_version_stored_on_event() -> None:
    """Each event stores the active pricing version."""
    repo = AsyncMock()
    recorder = _make_recorder(repo)
    ctx = UsageContext(
        operation="guard_security",
        provider="openai",
        model="gpt-5.4-mini",
    )
    payload = UsagePayload(
        usage_type=AiUsageType.TOKENS,
        status=AiUsageStatus.SUCCESS,
        prompt_tokens=1000,
        completion_tokens=200,
        invocation_id=uuid4(),
    )
    await recorder.record(ctx, payload)
    event = repo.record.call_args[0][0]
    assert event.price_version == recorder._pricing.version()


async def test_logical_ref_and_pipeline_event_id_passed_through() -> None:
    """logical_ref and pipeline_event_id are passed from context to event."""
    repo = AsyncMock()
    recorder = _make_recorder(repo)
    ctx = UsageContext(
        operation="guard_security",
        provider="openai",
        model="gpt-5.4-mini",
        child_user_id="child-1",
        logical_ref="guard_security:child-1:chat-1:v3",
        pipeline_event_id="evt-123",
    )
    payload = UsagePayload(
        usage_type=AiUsageType.TOKENS,
        status=AiUsageStatus.SUCCESS,
        prompt_tokens=1000,
        completion_tokens=200,
        invocation_id=uuid4(),
    )
    await recorder.record(ctx, payload)
    event = repo.record.call_args[0][0]
    assert event.logical_ref == "guard_security:child-1:chat-1:v3"
    assert event.pipeline_event_id == "evt-123"
