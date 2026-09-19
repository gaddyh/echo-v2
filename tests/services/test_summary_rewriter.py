"""Tests for SummaryRewriter and analyzer integration (mocked, no real API)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from echo_v2.domain.waiting_for_me import WaitingForMeDecision
from echo_v2.services.chat_analysis_worker import ConversationInput
from echo_v2.services.summary_rewriter import SummaryRewriter
from echo_v2.services.waiting_for_me_analyzer import LLMWaitingForMeAnalyzer

NOW = datetime(2026, 9, 5, 10, 0, 0, tzinfo=timezone.utc)


def _make_conversation():
    return ConversationInput(
        user_id="user-1",
        chat_id="972501234567@c.us",
        target_version=1,
        messages=[("inbound", "Are you free on Thursday?", NOW)],
    )


def _mock_openai_response(content: str) -> MagicMock:
    mock = MagicMock()
    mock.choices = [MagicMock()]
    mock.choices[0].message.content = content
    return mock


def _mock_client(response: MagicMock | None = None, side_effect=None) -> MagicMock:
    mock_client = MagicMock()
    mock_client.chat = MagicMock()
    mock_client.chat.completions = MagicMock()
    if side_effect is not None:
        mock_client.chat.completions.create = AsyncMock(side_effect=side_effect)
    else:
        mock_client.chat.completions.create = AsyncMock(return_value=response)
    return mock_client


# --- SummaryRewriter unit tests -------------------------------------------


async def test_rewrite_returns_warmed_summary():
    client = _mock_client(_mock_openai_response("מחכים שתאשר את תאריך הפגישה"))
    rewriter = SummaryRewriter(client=client)
    result = await rewriter.rewrite("ממתין לאישור תאריך הפגישה")
    assert result == "מחכים שתאשר את תאריך הפגישה"


async def test_rewrite_falls_back_on_api_error():
    client = _mock_client(side_effect=RuntimeError("API down"))
    rewriter = SummaryRewriter(client=client)
    original = "ממתין לאישור תאריך הפגישה"
    result = await rewriter.rewrite(original)
    assert result == original


async def test_rewrite_falls_back_on_empty_response():
    client = _mock_client(_mock_openai_response(""))
    rewriter = SummaryRewriter(client=client)
    original = "ממתין לאישור תאריך הפגישה"
    result = await rewriter.rewrite(original)
    assert result == original


async def test_rewrite_falls_back_on_whitespace_only():
    client = _mock_client(_mock_openai_response("   \n  "))
    rewriter = SummaryRewriter(client=client)
    original = "ממתין לאישור תאריך הפגישה"
    result = await rewriter.rewrite(original)
    assert result == original


async def test_rewrite_truncates_overlong_output():
    long = "א" * 200
    client = _mock_client(_mock_openai_response(long))
    rewriter = SummaryRewriter(client=client)
    result = await rewriter.rewrite("ממתין לאישור תאריך הפגישה")
    assert len(result) == 158
    assert result.endswith("…")


async def test_rewrite_empty_input_returns_empty():
    client = _mock_client(_mock_openai_response("should not be called"))
    rewriter = SummaryRewriter(client=client)
    assert await rewriter.rewrite("") == ""
    assert await rewriter.rewrite("   ") == "   "
    # The LLM should not have been called for empty input.
    client.chat.completions.create.assert_not_called()


async def test_rewrite_strips_whitespace_from_output():
    client = _mock_client(_mock_openai_response("  מחכים שתאשר  \n"))
    rewriter = SummaryRewriter(client=client)
    result = await rewriter.rewrite("ממתין לאישור תאריך הפגישה")
    assert result == "מחכים שתאשר"


async def test_rewrite_reasoning_model_uses_larger_token_budget():
    """Reasoning models (gpt-5+, o*) get max_completion_tokens=2000, no temperature."""
    client = _mock_client(_mock_openai_response("מחכים שתאשר"))
    rewriter = SummaryRewriter(client=client, model="gpt-5")
    await rewriter.rewrite("ממתין לאישור תאריך הפגישה")

    call_kwargs = client.chat.completions.create.await_args.kwargs
    assert call_kwargs["max_completion_tokens"] == 2000
    assert "temperature" not in call_kwargs


async def test_rewrite_reasoning_model_o_prefix():
    """Model names starting with 'o' (e.g. o3) are treated as reasoning models."""
    client = _mock_client(_mock_openai_response("מחכים שתאשר"))
    rewriter = SummaryRewriter(client=client, model="o3")
    await rewriter.rewrite("ממתין לאישור תאריך הפגישה")

    call_kwargs = client.chat.completions.create.await_args.kwargs
    assert call_kwargs["max_completion_tokens"] == 2000
    assert "temperature" not in call_kwargs


async def test_rewrite_non_reasoning_model_sets_temperature_zero():
    """Non-reasoning models get temperature=0 and max_completion_tokens=100."""
    client = _mock_client(_mock_openai_response("מחכים שתאשר"))
    rewriter = SummaryRewriter(client=client, model="gpt-4.1")
    await rewriter.rewrite("ממתין לאישור תאריך הפגישה")

    call_kwargs = client.chat.completions.create.await_args.kwargs
    assert call_kwargs["temperature"] == 0
    assert call_kwargs["max_completion_tokens"] == 100


async def test_summary_rewriter_protocol_raises_not_implemented():
    """The Protocol's default rewrite body raises NotImplementedError."""
    from echo_v2.services.summary_rewriter import SummaryRewriterProtocol

    with pytest.raises(NotImplementedError):
        await SummaryRewriterProtocol.rewrite(None, "summary")  # type: ignore[arg-type]


# --- Analyzer integration tests -------------------------------------------


async def test_analyzer_calls_rewriter_for_wfm_summary():
    analyzer_response = _mock_openai_response(
        json.dumps({
            "next_owner": "user",
            "open_obligation": "user owes a reply",
            "confidence": 0.9,
            "reason": "User must reply.",
            "summary": "ממתין לאישור תאריך הפגישה",
        })
    )
    rewriter_response = _mock_openai_response("מחכים שתאשר את תאריך הפגישה")

    # Analyzer call first, then rewriter call.
    client = _mock_client(side_effect=[analyzer_response, rewriter_response])
    rewriter = SummaryRewriter(client=client)
    analyzer = LLMWaitingForMeAnalyzer(
        client=client, prompt_version="v4", summary_rewriter=rewriter
    )

    result = await analyzer.analyze(_make_conversation())
    assert result.decision is WaitingForMeDecision.WAITING_FOR_ME
    assert result.summary == "מחכים שתאשר את תאריך הפגישה"
    assert client.chat.completions.create.call_count == 2


async def test_analyzer_skips_rewriter_for_nwm():
    analyzer_response = _mock_openai_response(
        json.dumps({
            "next_owner": "none",
            "open_obligation": None,
            "confidence": 0.95,
            "reason": "Conversation closed.",
            "summary": None,
        })
    )
    client = _mock_client(analyzer_response)
    rewriter = SummaryRewriter(client=client)
    analyzer = LLMWaitingForMeAnalyzer(
        client=client, prompt_version="v4", summary_rewriter=rewriter
    )

    result = await analyzer.analyze(_make_conversation())
    assert result.decision is WaitingForMeDecision.NOT_WAITING_FOR_ME
    assert result.summary is None
    # Only the analyzer call, no rewriter call.
    assert client.chat.completions.create.call_count == 1


async def test_analyzer_skips_rewriter_when_summary_is_none():
    analyzer_response = _mock_openai_response(
        json.dumps({
            "next_owner": "user",
            "open_obligation": "user owes a reply",
            "confidence": 0.9,
            "reason": "User must reply.",
            "summary": None,
        })
    )
    client = _mock_client(analyzer_response)
    rewriter = SummaryRewriter(client=client)
    analyzer = LLMWaitingForMeAnalyzer(
        client=client, prompt_version="v4", summary_rewriter=rewriter
    )

    result = await analyzer.analyze(_make_conversation())
    assert result.decision is WaitingForMeDecision.WAITING_FOR_ME
    assert result.summary is None
    assert client.chat.completions.create.call_count == 1


async def test_analyzer_falls_back_to_original_summary_on_rewrite_error():
    analyzer_response = _mock_openai_response(
        json.dumps({
            "next_owner": "user",
            "open_obligation": "user owes a reply",
            "confidence": 0.9,
            "reason": "User must reply.",
            "summary": "ממתין לאישור תאריך הפגישה",
        })
    )
    client = _mock_client(side_effect=[analyzer_response, RuntimeError("down")])
    rewriter = SummaryRewriter(client=client)
    analyzer = LLMWaitingForMeAnalyzer(
        client=client, prompt_version="v4", summary_rewriter=rewriter
    )

    result = await analyzer.analyze(_make_conversation())
    assert result.decision is WaitingForMeDecision.WAITING_FOR_ME
    # Falls back to the original cold summary.
    assert result.summary == "ממתין לאישור תאריך הפגישה"


async def test_analyzer_without_rewriter_works_as_before():
    """No rewriter injected — analyzer behaves exactly as before."""
    analyzer_response = _mock_openai_response(
        json.dumps({
            "next_owner": "user",
            "open_obligation": "user owes a reply",
            "confidence": 0.9,
            "reason": "User must reply.",
            "summary": "ממתין לאישור תאריך הפגישה",
        })
    )
    client = _mock_client(analyzer_response)
    analyzer = LLMWaitingForMeAnalyzer(client=client, prompt_version="v4")

    result = await analyzer.analyze(_make_conversation())
    assert result.decision is WaitingForMeDecision.WAITING_FOR_ME
    assert result.summary == "ממתין לאישור תאריך הפגישה"
    assert client.chat.completions.create.call_count == 1
