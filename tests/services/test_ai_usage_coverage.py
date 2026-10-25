"""Cover the _record_usage helper paths in analyzers that accept a recorder.

These tests pass a mock recorder to analyzers whose existing tests don't
use one, ensuring the recorder helper branches are covered.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from echo_v2.domain.ai_usage_event import AiUsageType
from echo_v2.services.chat_analysis_worker import ConversationInput
from echo_v2.services.summary_rewriter import SummaryRewriter
from echo_v2.services.time_parser import LLMTimeParser


def _openai_response(*, prompt=10, completion=5, total=15, content="{}"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(
            prompt_tokens=prompt, completion_tokens=completion, total_tokens=total
        ),
    )


def _conversation_input() -> ConversationInput:
    return ConversationInput(
        user_id="child-1",
        chat_id="chat@g.us",
        target_version=1,
        messages=[("inbound", "hello", datetime.now(timezone.utc))],
    )


@pytest.mark.asyncio
async def test_summary_rewriter_records_usage_on_success() -> None:
    recorder = AsyncMock()
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=_openai_response(content="warmer summary")
    )
    rewriter = SummaryRewriter(client=client, model="gpt-test", recorder=recorder)
    result = await rewriter.rewrite("cold summary")
    assert result == "warmer summary"
    recorder.record.assert_awaited_once()
    ctx, payload = recorder.record.call_args[0]
    assert ctx.operation == "summary_rewriter"
    assert payload.status == "success"
    assert payload.prompt_tokens == 10


@pytest.mark.asyncio
async def test_summary_rewriter_records_usage_on_error() -> None:
    recorder = AsyncMock()
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=RuntimeError("api down"))
    rewriter = SummaryRewriter(client=client, model="gpt-test", recorder=recorder)
    result = await rewriter.rewrite("cold summary")
    assert result == "cold summary"  # falls back to original
    recorder.record.assert_awaited_once()
    payload = recorder.record.call_args[0][1]
    assert payload.status == "error"
    assert payload.error_type == "RuntimeError"


@pytest.mark.asyncio
async def test_llm_time_parser_records_usage_on_success() -> None:
    recorder = AsyncMock()
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=_openai_response(
            content='{"utc_datetime": "2099-01-01T00:00:00+00:00"}'
        )
    )
    parser = LLMTimeParser(client=client, model="gpt-test", recorder=recorder)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = await parser.parse("tomorrow", user_timezone="UTC", now_utc=now)
    assert result.year == 2099
    recorder.record.assert_awaited_once()
    payload = recorder.record.call_args[0][1]
    assert payload.status == "success"


@pytest.mark.asyncio
async def test_llm_time_parser_records_usage_on_error() -> None:
    from echo_v2.services.time_parser_regex import TimeParseError

    recorder = AsyncMock()
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=RuntimeError("api down"))
    parser = LLMTimeParser(client=client, model="gpt-test", recorder=recorder)
    with pytest.raises(TimeParseError):
        await parser.parse("tomorrow", user_timezone="UTC")
    recorder.record.assert_awaited_once()
    payload = recorder.record.call_args[0][1]
    assert payload.status == "error"


@pytest.mark.asyncio
async def test_analysis_judge_records_usage_on_success() -> None:
    from echo_v2.domain.waiting_for_me import (
        NextOwner,
        WaitingForMeDecision,
        WaitingForMeResult,
    )
    from echo_v2.services.analysis_judge import AnalysisJudge

    recorder = AsyncMock()
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=_openai_response(content='{"score": 1.0, "explanation": "correct"}')
    )
    judge = AnalysisJudge(client=client, model="gpt-test", recorder=recorder)
    result = WaitingForMeResult(
        decision=WaitingForMeDecision.WAITING_FOR_ME,
        next_owner=NextOwner.USER,
        confidence=0.9,
        target_version=1,
    )
    await judge.judge(_conversation_input(), result)
    recorder.record.assert_awaited_once()
    payload = recorder.record.call_args[0][1]
    assert payload.status == "success"


@pytest.mark.asyncio
async def test_analysis_judge_records_usage_on_error() -> None:
    from echo_v2.domain.waiting_for_me import (
        NextOwner,
        WaitingForMeDecision,
        WaitingForMeResult,
    )
    from echo_v2.services.analysis_judge import AnalysisJudge

    recorder = AsyncMock()
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=RuntimeError("api down"))
    judge = AnalysisJudge(client=client, model="gpt-test", recorder=recorder)
    result = WaitingForMeResult(
        decision=WaitingForMeDecision.WAITING_FOR_ME,
        next_owner=NextOwner.USER,
        confidence=0.9,
        target_version=1,
    )
    await judge.judge(_conversation_input(), result)
    recorder.record.assert_awaited_once()
    payload = recorder.record.call_args[0][1]
    assert payload.status == "error"


@pytest.mark.asyncio
async def test_waiting_for_me_analyzer_records_usage_on_success() -> None:
    from echo_v2.services.waiting_for_me_analyzer import LLMWaitingForMeAnalyzer

    recorder = AsyncMock()
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=_openai_response(
            content='{"next_owner": "user", "open_obligation": "test", "confidence": 0.9}'
        )
    )
    analyzer = LLMWaitingForMeAnalyzer(client=client, model="gpt-test", recorder=recorder)
    await analyzer.analyze(_conversation_input())
    recorder.record.assert_awaited_once()
    ctx, payload = recorder.record.call_args[0]
    assert ctx.operation == "wfm_analyzer"
    assert ctx.child_user_id == "child-1"
    assert payload.status == "success"


@pytest.mark.asyncio
async def test_waiting_for_me_analyzer_records_usage_on_error() -> None:
    from echo_v2.services.waiting_for_me_analyzer import (
        AnalysisError,
        LLMWaitingForMeAnalyzer,
    )

    recorder = AsyncMock()
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=RuntimeError("api down"))
    analyzer = LLMWaitingForMeAnalyzer(client=client, model="gpt-test", recorder=recorder)
    with pytest.raises(AnalysisError):
        await analyzer.analyze(_conversation_input())
    recorder.record.assert_awaited_once()
    payload = recorder.record.call_args[0][1]
    assert payload.status == "error"


@pytest.mark.asyncio
async def test_incident_matcher_records_usage_on_success() -> None:
    from echo_v2.services.incident_matcher import (
        IncidentCandidate,
        IncidentMatchDecision,
        IncidentMatcher,
        IncidentMatcherLLMOutput,
        MatcherAssessment,
    )

    recorder = AsyncMock()
    parsed = IncidentMatcherLLMOutput(
        decision=IncidentMatchDecision.NEW_INCIDENT.value,
        incident_id=None,
        reason="distinct issue",
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))],
        usage=SimpleNamespace(prompt_tokens=50, completion_tokens=10, total_tokens=60),
    )
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(return_value=response)
    matcher = IncidentMatcher(client=client, model="gpt-test", recorder=recorder)

    now = datetime.now(timezone.utc)
    assessment = MatcherAssessment(
        summary="test",
        reason="test",
        evidence_message_ids=("m1",),
        child_user_id="child-1",
        chat_id="chat@g.us",
        created_at=now,
    )
    candidate = IncidentCandidate(
        id="inc-1",
        alert_type="security",
        issue_summary="old issue",
        severity_summary="low",
        first_seen_at=now,
        last_seen_at=now,
        evidence_message_ids=("m2",),
    )
    result = await matcher.match(
        alert_type="security",
        assessment=assessment,
        incidents=(candidate,),
    )
    assert result.decision == IncidentMatchDecision.NEW_INCIDENT
    recorder.record.assert_awaited_once()
    ctx, payload = recorder.record.call_args[0]
    assert ctx.operation == "guard_incident_match"
    assert payload.status == "success"


@pytest.mark.asyncio
async def test_incident_matcher_records_usage_on_error() -> None:
    from echo_v2.services.incident_matcher import (
        IncidentCandidate,
        IncidentMatcher,
        IncidentMatchError,
        MatcherAssessment,
    )

    recorder = AsyncMock()
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(side_effect=RuntimeError("api down"))
    matcher = IncidentMatcher(client=client, model="gpt-test", recorder=recorder)

    now = datetime.now(timezone.utc)
    assessment = MatcherAssessment(
        summary="test",
        reason="test",
        evidence_message_ids=("m1",),
        child_user_id="child-1",
        chat_id="chat@g.us",
        created_at=now,
    )
    candidate = IncidentCandidate(
        id="inc-1",
        alert_type="security",
        issue_summary="old issue",
        severity_summary="low",
        first_seen_at=now,
        last_seen_at=now,
        evidence_message_ids=("m2",),
    )
    with pytest.raises(IncidentMatchError):
        await matcher.match(
            alert_type="security",
            assessment=assessment,
            incidents=(candidate,),
        )
    # Default max_retries=1 → 2 attempts, both error.
    assert recorder.record.await_count == 2
    for call in recorder.record.call_args_list:
        assert call.args[1].status == "error"


@pytest.mark.asyncio
async def test_media_summarizer_records_image_usage() -> None:
    """The module-level _record_media_usage helper is covered."""
    from echo_v2.services.media_summarizer import summarize_image_bytes

    recorder = AsyncMock()
    client = MagicMock()
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="a photo"))],
        usage=SimpleNamespace(prompt_tokens=20, completion_tokens=5, total_tokens=25),
    )
    client.chat.completions.create = AsyncMock(return_value=response)
    result = await summarize_image_bytes(
        client=client,
        model="gpt-test",
        image_bytes=b"fake",
        mime_type="image/jpeg",
        recorder=recorder,
        child_user_id="child-1",
    )
    assert result.text == "a photo"
    recorder.record.assert_awaited_once()
    ctx, payload = recorder.record.call_args[0]
    assert ctx.operation == "media_image"
    assert payload.status == "success"
    assert payload.prompt_tokens == 20


@pytest.mark.asyncio
async def test_media_summarizer_records_image_usage_on_error() -> None:
    from echo_v2.services.media_summarizer import summarize_image_bytes

    recorder = AsyncMock()
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=RuntimeError("api down"))
    result = await summarize_image_bytes(
        client=client,
        model="gpt-test",
        image_bytes=b"fake",
        mime_type="image/jpeg",
        recorder=recorder,
        child_user_id="child-1",
    )
    assert "image" in result.kind  # placeholder
    recorder.record.assert_awaited_once()
    payload = recorder.record.call_args[0][1]
    assert payload.status == "error"


@pytest.mark.asyncio
async def test_recorder_image_units_branch() -> None:
    """Cover the IMAGE_UNITS branch in AiUsageRecorder._estimate."""
    from echo_v2.services.reporting.ai_usage_recorder import (
        AiUsageRecorder,
        UsageContext,
        UsagePayload,
    )
    from echo_v2.services.reporting.pricing_service import PricingService

    repo = AsyncMock()
    recorder = AiUsageRecorder(repo=repo, pricing=PricingService())
    ctx = UsageContext(
        operation="media_image",
        provider="openai",
        model="gpt-test",
    )
    payload = UsagePayload(
        usage_type=AiUsageType.IMAGE_UNITS,
        status="success",
        usage_details={"image_count": 2},
        invocation_id=UUID("00000000-0000-0000-0000-000000000001"),
    )
    await recorder.record(ctx, payload)
    event = repo.record.call_args[0][0]
    assert event.cost_source == "fallback_estimate"


@pytest.mark.asyncio
async def test_recorder_unknown_usage_type_branch() -> None:
    """Cover the unknown usage type branch in AiUsageRecorder._estimate."""
    from echo_v2.services.reporting.ai_usage_recorder import (
        AiUsageRecorder,
        UsageContext,
        UsagePayload,
    )
    from echo_v2.services.reporting.pricing_service import PricingService

    repo = AsyncMock()
    recorder = AiUsageRecorder(repo=repo, pricing=PricingService())
    ctx = UsageContext(
        operation="other",
        provider="openai",
        model="gpt-test",
    )
    payload = UsagePayload(
        usage_type="gpu_seconds",  # not priced yet
        status="success",
        invocation_id=UUID("00000000-0000-0000-0000-000000000002"),
    )
    await recorder.record(ctx, payload)
    event = repo.record.call_args[0][0]
    assert event.cost_source == "unknown"
    assert event.estimated_cost_usd is None


def test_pricing_google_fallback_when_no_usage() -> None:
    """Google tokens with no usage → fallback_estimate with gemini assumptions."""
    from echo_v2.services.reporting.pricing_service import PricingService

    service = PricingService()
    cost, source = service.estimate_token_cost(
        provider="google",
        model="gemini-3.5-flash-lite",
        prompt_tokens=None,
        completion_tokens=None,
    )
    assert source == "fallback_estimate"
    assert cost is not None
    # 2000 * 0.10/1M + 150 * 0.40/1M
    expected = Decimal("0.0002") + Decimal("0.00006")
    assert cost == expected


def test_pricing_unknown_provider_fallback_returns_zero() -> None:
    """Unknown provider with no usage → unknown (fallback returns 0,0)."""
    from echo_v2.services.reporting.pricing_service import PricingService

    service = PricingService()
    cost, source = service.estimate_token_cost(
        provider="anthropic",
        model="claude-3",
        prompt_tokens=None,
        completion_tokens=None,
    )
    assert source == "unknown"
    assert cost is None


def test_pricing_invalid_config_raises() -> None:
    """Invalid pricing config raises ValueError."""
    from echo_v2.services.reporting.pricing_service import _parse_config

    with pytest.raises(ValueError, match="invalid pricing configuration"):
        _parse_config({"version": "1"}, source="test")


@pytest.mark.asyncio
async def test_gemini_video_summarizer_records_usage_on_success() -> None:
    """Gemini video summarizer records usage with the recorder."""
    from unittest.mock import patch

    import httpx

    from echo_v2.services.gemini_video_summarizer import GeminiVideoSummarizer

    recorder = AsyncMock()
    summarizer = GeminiVideoSummarizer(api_key="test-key", recorder=recorder)
    video_bytes = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 100

    response = MagicMock()
    response.text = "סרטון קצר"
    response.usage_metadata = SimpleNamespace(
        prompt_token_count=100,
        candidates_token_count=20,
        cached_content_token_count=0,
        total_token_count=120,
    )

    stream_response = AsyncMock()
    stream_response.status_code = 200
    stream_response.raise_for_status = MagicMock()

    async def _aiter_bytes():
        yield video_bytes

    stream_response.aiter_bytes = _aiter_bytes

    class _FakeStreamContext:
        async def __aenter__(self):
            return stream_response

        async def __aexit__(self, *args):
            pass

    with (
        patch.object(
            httpx.AsyncClient, "stream",
            return_value=_FakeStreamContext(),
        ),
        patch.object(GeminiVideoSummarizer, "_assert_safe_url"),
        patch("google.genai.Client") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=response)
        mock_client_cls.return_value = mock_client

        result = await summarizer.summarize_media(
            download_url="https://media.test/video.mp4",
            mime_type="video/mp4",
            file_name="video.mp4",
            message_type="video",
            caption=None,
            child_user_id="child-1",
        )

    assert result.kind == "video"
    recorder.record.assert_awaited_once()
    ctx, payload = recorder.record.call_args[0]
    assert ctx.operation == "media_video"
    assert ctx.provider == "google"
    assert ctx.child_user_id == "child-1"
    assert payload.status == "success"
    assert payload.prompt_tokens == 100
    assert payload.completion_tokens == 20


@pytest.mark.asyncio
async def test_gemini_video_summarizer_records_usage_on_error() -> None:
    """Gemini video summarizer records usage on provider error."""
    from unittest.mock import patch

    import httpx

    from echo_v2.services.gemini_video_summarizer import GeminiVideoSummarizer

    recorder = AsyncMock()
    summarizer = GeminiVideoSummarizer(api_key="test-key", recorder=recorder)
    video_bytes = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 100

    stream_response = AsyncMock()
    stream_response.status_code = 200
    stream_response.raise_for_status = MagicMock()

    async def _aiter_bytes():
        yield video_bytes

    stream_response.aiter_bytes = _aiter_bytes

    class _FakeStreamContext:
        async def __aenter__(self):
            return stream_response

        async def __aexit__(self, *args):
            pass

    with (
        patch.object(
            httpx.AsyncClient, "stream",
            return_value=_FakeStreamContext(),
        ),
        patch.object(GeminiVideoSummarizer, "_assert_safe_url"),
        patch("google.genai.Client") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(
            side_effect=RuntimeError("gemini api down")
        )
        mock_client_cls.return_value = mock_client

        with pytest.raises(RuntimeError, match="gemini api down"):
            await summarizer.summarize_media(
                download_url="https://media.test/video.mp4",
                mime_type="video/mp4",
                file_name="video.mp4",
                message_type="video",
                caption=None,
                child_user_id="child-1",
            )

    recorder.record.assert_awaited_once()
    payload = recorder.record.call_args[0][1]
    assert payload.status == "error"
    assert payload.error_type == "RuntimeError"


@pytest.mark.asyncio
async def test_legacy_guard_analyzer_records_usage_on_success() -> None:
    """Legacy LLMGuardAnalyzer records usage with the recorder."""
    from echo_v2.services.guard_analyzer import (
        GuardAnalysisInput,
        GuardLLMOutput,
        GuardMessage,
        LLMGuardAnalyzer,
    )

    recorder = AsyncMock()
    parsed = GuardLLMOutput(
        signals=[],
        categories=[],
        evidence_message_ids=[],
        confidence=1.0,
        reason="ok",
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="{}", parsed=parsed))],
        usage=SimpleNamespace(prompt_tokens=50, completion_tokens=10, total_tokens=60),
    )
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(return_value=response)
    analyzer = LLMGuardAnalyzer(client=client, model="gpt-test", recorder=recorder)

    conversation = GuardAnalysisInput(
        child_id="child-1",
        chat_id="chat-1",
        messages=(GuardMessage(id="m1", sender="other", text="hello"),),
    )
    await analyzer.analyze(conversation)
    recorder.record.assert_awaited_once()
    ctx, payload = recorder.record.call_args[0]
    assert ctx.operation == "guard_security"
    assert ctx.child_user_id == "child-1"
    assert payload.status == "success"
    assert payload.prompt_tokens == 50


@pytest.mark.asyncio
async def test_legacy_guard_analyzer_records_usage_on_error() -> None:
    """Legacy LLMGuardAnalyzer records usage on provider error."""
    from echo_v2.services.guard_analyzer import (
        AnalysisError,
        GuardAnalysisInput,
        GuardMessage,
        LLMGuardAnalyzer,
    )

    recorder = AsyncMock()
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(side_effect=RuntimeError("api down"))
    analyzer = LLMGuardAnalyzer(client=client, model="gpt-test", recorder=recorder)

    conversation = GuardAnalysisInput(
        child_id="child-1",
        chat_id="chat-1",
        messages=(GuardMessage(id="m1", sender="other", text="hello"),),
    )
    with pytest.raises(AnalysisError):
        await analyzer.analyze(conversation)
    # Default max_retries=1 → 2 attempts.
    assert recorder.record.await_count == 2
    for call in recorder.record.call_args_list:
        assert call.args[1].status == "error"


@pytest.mark.asyncio
async def test_exposure_analyzer_records_usage_on_success() -> None:
    """ExposureAnalyzer records usage with the recorder."""
    from datetime import datetime, timezone

    from echo_v2.domain.guard_simple import (
        SimpleConversation,
        SimpleConversationMessage,
    )
    from echo_v2.services.guard_exposure_analyzer import (
        ExposureAnalyzer,
        ExposureLLMOutput,
    )

    recorder = AsyncMock()
    parsed = ExposureLLMOutput(
        significant_exposure=False,
        category=None,
        summary="no exposure",
        evidence_message_ids=[],
        reason="ok",
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="{}", parsed=parsed))],
        usage=SimpleNamespace(prompt_tokens=40, completion_tokens=8, total_tokens=48),
    )
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(return_value=response)
    analyzer = ExposureAnalyzer(client=client, model="gpt-test", recorder=recorder)

    now = datetime.now(timezone.utc)
    conversation = SimpleConversation(
        child_user_id="child-1",
        child_name="Shay",
        chat_id="chat-1",
        chat_type="group",
        connection_id="conn-1",
        timezone="Asia/Jerusalem",
        window_start=now,
        window_end=now,
        monitored_child_role="child",
        messages=(SimpleConversationMessage("m1", "other", "hello"),),
    )
    await analyzer.analyze(conversation)
    recorder.record.assert_awaited_once()
    ctx, payload = recorder.record.call_args[0]
    assert ctx.operation == "guard_exposure"
    assert ctx.child_user_id == "child-1"
    assert payload.status == "success"
    assert payload.prompt_tokens == 40


@pytest.mark.asyncio
async def test_exposure_analyzer_records_usage_on_error() -> None:
    """ExposureAnalyzer records usage on provider error."""
    from datetime import datetime, timezone

    from echo_v2.domain.guard_simple import (
        SimpleConversation,
        SimpleConversationMessage,
    )
    from echo_v2.services.guard_exposure_analyzer import (
        ExposureAnalysisError,
        ExposureAnalyzer,
    )

    recorder = AsyncMock()
    client = MagicMock()
    client.chat.completions.parse = AsyncMock(side_effect=RuntimeError("api down"))
    analyzer = ExposureAnalyzer(client=client, model="gpt-test", recorder=recorder)

    now = datetime.now(timezone.utc)
    conversation = SimpleConversation(
        child_user_id="child-1",
        child_name="Shay",
        chat_id="chat-1",
        chat_type="group",
        connection_id="conn-1",
        timezone="Asia/Jerusalem",
        window_start=now,
        window_end=now,
        monitored_child_role="child",
        messages=(SimpleConversationMessage("m1", "other", "hello"),),
    )
    with pytest.raises(ExposureAnalysisError):
        await analyzer.analyze(conversation)
    assert recorder.record.await_count >= 1
    for call in recorder.record.call_args_list:
        assert call.args[1].status == "error"
