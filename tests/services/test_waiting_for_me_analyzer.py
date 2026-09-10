"""Tests for LLMWaitingForMeAnalyzer (mocked OpenAI, no real API calls)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from echo_v2.domain.waiting_for_me import WaitingForMeDecision
from echo_v2.services.chat_analysis_worker import ConversationInput
from echo_v2.services.waiting_for_me_analyzer import (
    AnalysisError,
    LLMWaitingForMeAnalyzer,
    _build_user_message,
    _parse_llm_output,
)

NOW = datetime(2026, 9, 5, 10, 0, 0, tzinfo=timezone.utc)


def _make_conversation(
    messages: list[tuple[str, str]] | None = None,
    target_version: int = 1,
) -> ConversationInput:
    if messages is None:
        messages = [("inbound", "Are you free on Thursday?")]
    return ConversationInput(
        user_id="user-1",
        chat_id="972501234567@c.us",
        target_version=target_version,
        messages=[
            (direction, text, NOW + timedelta(minutes=i))
            for i, (direction, text) in enumerate(messages)
        ],
    )


def _mock_openai_response(content: str) -> MagicMock:
    """Build a mock OpenAI response with the given content string."""
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = content
    return mock_response


def _mock_client(response: MagicMock | None = None, side_effect=None) -> MagicMock:
    mock_client = MagicMock()
    mock_client.chat = MagicMock()
    mock_client.chat.completions = MagicMock()
    if side_effect is not None:
        mock_client.chat.completions.create = AsyncMock(side_effect=side_effect)
    else:
        mock_client.chat.completions.create = AsyncMock(return_value=response)
    mock_client.close = AsyncMock()
    return mock_client


# --- analyze() success cases ------------------------------------------------


async def test_analyze_waiting_for_me():
    conv = _make_conversation()
    response = _mock_openai_response(
        json.dumps({
            "decision": "waiting_for_me",
            "confidence": 0.95,
            "reason": "Direct question awaiting answer.",
        })
    )
    client = _mock_client(response)

    with patch("openai.AsyncOpenAI", return_value=client):
        analyzer = LLMWaitingForMeAnalyzer(api_key="test-key")
        result = await analyzer.analyze(conv)

    assert result.decision == WaitingForMeDecision.WAITING_FOR_ME
    assert result.confidence == 0.95
    assert result.reason == "Direct question awaiting answer."
    assert result.target_version == 1


async def test_analyze_not_waiting_for_me():
    conv = _make_conversation([("inbound", "Thanks!")])
    response = _mock_openai_response(
        json.dumps({
            "decision": "not_waiting_for_me",
            "confidence": 0.9,
            "reason": "Closing acknowledgment.",
        })
    )
    client = _mock_client(response)

    with patch("openai.AsyncOpenAI", return_value=client):
        analyzer = LLMWaitingForMeAnalyzer(api_key="test-key")
        result = await analyzer.analyze(conv)

    assert result.decision == WaitingForMeDecision.NOT_WAITING_FOR_ME


async def test_analyze_uncertain():
    conv = _make_conversation([("inbound", "")])
    response = _mock_openai_response(
        json.dumps({
            "decision": "uncertain",
            "confidence": 0.4,
            "reason": "Empty message, no content to analyze.",
        })
    )
    client = _mock_client(response)

    with patch("openai.AsyncOpenAI", return_value=client):
        analyzer = LLMWaitingForMeAnalyzer(api_key="test-key")
        result = await analyzer.analyze(conv)

    assert result.decision == WaitingForMeDecision.UNCERTAIN


# --- analyze() edge cases ---------------------------------------------------


async def test_analyze_empty_conversation_returns_uncertain():
    conv = ConversationInput(
        user_id="user-1",
        chat_id="972501234567@c.us",
        target_version=1,
        messages=[],
    )
    analyzer = LLMWaitingForMeAnalyzer(api_key="test-key")
    result = await analyzer.analyze(conv)
    assert result.decision == WaitingForMeDecision.UNCERTAIN
    assert result.confidence == 1.0
    assert "No messages" in (result.reason or "")


async def test_analyze_no_api_key_raises(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    conv = _make_conversation()
    analyzer = LLMWaitingForMeAnalyzer(api_key="")
    with pytest.raises(AnalysisError, match="OPENAI_API_KEY not configured"):
        await analyzer.analyze(conv)


async def test_analyze_api_error_raises():
    conv = _make_conversation()
    client = _mock_client(side_effect=RuntimeError("network error"))

    with patch("openai.AsyncOpenAI", return_value=client):
        analyzer = LLMWaitingForMeAnalyzer(api_key="test-key")
        with pytest.raises(AnalysisError, match="LLM request failed"):
            await analyzer.analyze(conv)


async def test_analyze_uses_env_api_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    analyzer = LLMWaitingForMeAnalyzer()
    assert analyzer._api_key == "env-key"


# --- _parse_llm_output ------------------------------------------------------


def test_parse_valid_output():
    raw = json.dumps({
        "decision": "waiting_for_me",
        "confidence": 0.8,
        "reason": "Open question.",
    })
    result = _parse_llm_output(raw, target_version=3)
    assert result.decision == WaitingForMeDecision.WAITING_FOR_ME
    assert result.confidence == 0.8
    assert result.reason == "Open question."
    assert result.target_version == 3


def test_parse_output_with_code_fences():
    raw = f"```json\n{json.dumps({'decision': 'not_waiting_for_me', 'confidence': 0.9, 'reason': 'Closed.'})}\n```"
    result = _parse_llm_output(raw, target_version=1)
    assert result.decision == WaitingForMeDecision.NOT_WAITING_FOR_ME


def test_parse_invalid_json_raises():
    with pytest.raises(AnalysisError, match="not valid JSON"):
        _parse_llm_output("not json", target_version=1)


def test_parse_missing_decision_key_raises():
    with pytest.raises(AnalysisError, match="missing 'decision'"):
        _parse_llm_output('{"foo": "bar"}', target_version=1)


def test_parse_unknown_decision_raises():
    with pytest.raises(AnalysisError, match="unknown decision"):
        _parse_llm_output('{"decision": "maybe"}', target_version=1)


def test_parse_clamps_confidence():
    raw = json.dumps({"decision": "uncertain", "confidence": 1.5})
    result = _parse_llm_output(raw, target_version=1)
    assert result.confidence == 1.0


def test_parse_clamps_negative_confidence():
    raw = json.dumps({"decision": "uncertain", "confidence": -0.5})
    result = _parse_llm_output(raw, target_version=1)
    assert result.confidence == 0.0


def test_parse_invalid_confidence_becomes_none():
    raw = json.dumps({"decision": "uncertain", "confidence": "high"})
    result = _parse_llm_output(raw, target_version=1)
    assert result.confidence is None


def test_parse_missing_confidence_is_none():
    raw = json.dumps({"decision": "waiting_for_me"})
    result = _parse_llm_output(raw, target_version=1)
    assert result.confidence is None


def test_parse_missing_reason_is_none():
    raw = json.dumps({"decision": "waiting_for_me"})
    result = _parse_llm_output(raw, target_version=1)
    assert result.reason is None


def test_parse_decision_case_insensitive():
    raw = json.dumps({"decision": "WAITING_FOR_ME"})
    result = _parse_llm_output(raw, target_version=1)
    assert result.decision == WaitingForMeDecision.WAITING_FOR_ME


# --- _build_user_message ----------------------------------------------------


def test_build_user_message_renders_conversation():
    conv = _make_conversation([
        ("inbound", "Are you free?"),
        ("outbound", "Yes, what time?"),
        ("inbound", "3pm"),
    ])
    msg = _build_user_message(conv)
    assert "Conversation:" in msg
    assert "them: Are you free?" in msg
    assert "me: Yes, what time?" in msg
    assert "them: 3pm" in msg


def test_build_user_message_includes_timestamps():
    conv = _make_conversation([("inbound", "hello")])
    msg = _build_user_message(conv)
    assert "2026-09-05 10:00" in msg
