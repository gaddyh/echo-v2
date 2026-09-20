"""Tests for LLMWaitingForMeAnalyzer (mocked OpenAI, no real API calls)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

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

    analyzer = LLMWaitingForMeAnalyzer(client=client)
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

    analyzer = LLMWaitingForMeAnalyzer(client=client)
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

    analyzer = LLMWaitingForMeAnalyzer(client=client)
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
    client = _mock_client()
    analyzer = LLMWaitingForMeAnalyzer(client=client)
    result = await analyzer.analyze(conv)
    assert result.decision == WaitingForMeDecision.UNCERTAIN
    assert result.confidence == 1.0
    assert "No messages" in (result.reason or "")
    # Client should not be called for empty conversations
    client.chat.completions.create.assert_not_called()


async def test_analyze_api_error_raises():
    conv = _make_conversation()
    client = _mock_client(side_effect=RuntimeError("network error"))

    analyzer = LLMWaitingForMeAnalyzer(client=client)
    with pytest.raises(AnalysisError, match="LLM request failed"):
        await analyzer.analyze(conv)


async def test_analyze_passes_model_and_messages_to_client():
    """The analyzer passes the configured model and prompt to the client."""
    conv = _make_conversation()
    response = _mock_openai_response(
        json.dumps({"decision": "waiting_for_me", "confidence": 0.9, "reason": "test"})
    )
    client = _mock_client(response)

    analyzer = LLMWaitingForMeAnalyzer(client=client, model="gpt-4o")
    await analyzer.analyze(conv)

    call_kwargs = client.chat.completions.create.call_args
    assert call_kwargs.kwargs["model"] == "gpt-4o"
    assert call_kwargs.kwargs["temperature"] == 0
    assert len(call_kwargs.kwargs["messages"]) == 2  # system + user


async def test_analyze_fills_analysis_version_metadata():
    """The analyzer fills model/prompt_version/analyzer_version on the result."""
    conv = _make_conversation()
    response = _mock_openai_response(
        json.dumps({"decision": "waiting_for_me", "confidence": 0.9, "reason": "test"})
    )
    client = _mock_client(response)

    analyzer = LLMWaitingForMeAnalyzer(client=client, model="gpt-4o", prompt_version="v2")
    result = await analyzer.analyze(conv)

    assert result.model == "gpt-4o"
    assert result.prompt_version == "v2"
    assert result.analyzer_version is not None
    assert result.analyzer_version != ""


async def test_analyze_empty_conversation_fills_analysis_version_metadata():
    """Even the empty-conversation path fills model/prompt_version/analyzer_version."""
    conv = ConversationInput(
        user_id="user-1",
        chat_id="972501234567@c.us",
        target_version=1,
        messages=[],
    )
    client = _mock_client()
    analyzer = LLMWaitingForMeAnalyzer(client=client, model="gpt-4o", prompt_version="v2")
    result = await analyzer.analyze(conv)
    assert result.model == "gpt-4o"
    assert result.prompt_version == "v2"
    assert result.analyzer_version is not None


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
    with pytest.raises(AnalysisError, match="missing 'next_owner' or 'decision'"):
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


# --- summary parsing --------------------------------------------------------


def test_parse_summary_present():
    raw = json.dumps({
        "decision": "waiting_for_me",
        "confidence": 0.9,
        "reason": "Direct question.",
        "summary": "רוצה לתאם פגישה למחר ומחכה שתאשר אם אתה פנוי.",
    })
    result = _parse_llm_output(raw, target_version=1)
    assert result.summary == "רוצה לתאם פגישה למחר ומחכה שתאשר אם אתה פנוי."


def test_parse_summary_missing_is_none():
    raw = json.dumps({"decision": "waiting_for_me", "confidence": 0.9})
    result = _parse_llm_output(raw, target_version=1)
    assert result.summary is None


def test_parse_summary_empty_string_becomes_none():
    raw = json.dumps({"decision": "waiting_for_me", "summary": ""})
    result = _parse_llm_output(raw, target_version=1)
    assert result.summary is None


def test_parse_summary_whitespace_only_becomes_none():
    raw = json.dumps({"decision": "waiting_for_me", "summary": "   "})
    result = _parse_llm_output(raw, target_version=1)
    assert result.summary is None


def test_parse_summary_truncated_at_160():
    long_summary = "א" * 200
    raw = json.dumps({
        "decision": "waiting_for_me",
        "summary": long_summary,
    })
    result = _parse_llm_output(raw, target_version=1)
    assert result.summary is not None
    # Truncated to 157 chars + "…" = 158 total.
    assert len(result.summary) == 158
    assert result.summary.endswith("…")


def test_parse_summary_exactly_160_not_truncated():
    summary = "א" * 160
    raw = json.dumps({
        "decision": "waiting_for_me",
        "summary": summary,
    })
    result = _parse_llm_output(raw, target_version=1)
    assert result.summary == summary


async def test_analyze_summary_in_result():
    conv = _make_conversation()
    response = _mock_openai_response(
        json.dumps({
            "decision": "waiting_for_me",
            "confidence": 0.95,
            "reason": "Direct question.",
            "summary": "מחכה לאישור פגישה.",
        })
    )
    client = _mock_client(response)
    analyzer = LLMWaitingForMeAnalyzer(client=client)
    result = await analyzer.analyze(conv)
    assert result.summary == "מחכה לאישור פגישה."


# --- v3 next_owner contract -------------------------------------------------
# The v3 prompt emits `next_owner` (user|other|none|uncertain) instead of
# `decision`. The product decision is derived deterministically in the
# parser so the model can never invert the label.


def test_parse_next_owner_user_derives_wfm():
    raw = json.dumps({
        "next_owner": "user",
        "open_obligation": "user owes a reply",
        "confidence": 0.9,
        "reason": "User must reply.",
        "summary": "מחכה לתשובה שלך.",
    })
    result = _parse_llm_output(raw, target_version=1)
    assert result.decision == WaitingForMeDecision.WAITING_FOR_ME
    assert result.next_owner is not None
    assert result.next_owner.value == "user"
    assert result.open_obligation == "user owes a reply"
    assert result.summary == "מחכה לתשובה שלך."


def test_parse_next_owner_other_derives_nwm():
    raw = json.dumps({
        "next_owner": "other",
        "open_obligation": "other person must answer the user's question",
        "confidence": 0.9,
        "reason": "User asked a question; other person must answer.",
    })
    result = _parse_llm_output(raw, target_version=1)
    assert result.decision == WaitingForMeDecision.NOT_WAITING_FOR_ME
    assert result.next_owner is not None
    assert result.next_owner.value == "other"
    assert result.open_obligation == "other person must answer the user's question"


def test_parse_next_owner_none_derives_nwm():
    raw = json.dumps({
        "next_owner": "none",
        "reason": "No open obligation.",
    })
    result = _parse_llm_output(raw, target_version=1)
    assert result.decision == WaitingForMeDecision.NOT_WAITING_FOR_ME
    assert result.next_owner is not None
    assert result.next_owner.value == "none"
    # NONE → open_obligation should be null per the contract.
    assert result.open_obligation is None


def test_parse_next_owner_uncertain_derives_unc():
    raw = json.dumps({
        "next_owner": "uncertain",
        "reason": "Ambiguous.",
    })
    result = _parse_llm_output(raw, target_version=1)
    assert result.decision == WaitingForMeDecision.UNCERTAIN
    assert result.next_owner is not None
    assert result.next_owner.value == "uncertain"
    assert result.open_obligation is None


def test_parse_next_owner_case_insensitive():
    raw = json.dumps({"next_owner": "OTHER", "reason": "r"})
    result = _parse_llm_output(raw, target_version=1)
    assert result.decision == WaitingForMeDecision.NOT_WAITING_FOR_ME
    assert result.next_owner is not None
    assert result.next_owner.value == "other"


def test_parse_next_owner_unknown_raises():
    raw = json.dumps({"next_owner": "banana"})
    with pytest.raises(AnalysisError, match="unknown next_owner"):
        _parse_llm_output(raw, target_version=1)


def test_parse_next_owner_missing_and_no_decision_raises():
    raw = json.dumps({"foo": "bar"})
    with pytest.raises(AnalysisError, match="missing 'next_owner' or 'decision'"):
        _parse_llm_output(raw, target_version=1)


def test_parse_next_owner_with_code_fences():
    raw = f"```json\n{json.dumps({'next_owner': 'user', 'reason': 'r'})}\n```"
    result = _parse_llm_output(raw, target_version=1)
    assert result.decision == WaitingForMeDecision.WAITING_FOR_ME
    assert result.next_owner is not None
    assert result.next_owner.value == "user"


def test_parse_next_owner_strips_open_obligation_whitespace():
    raw = json.dumps({
        "next_owner": "user",
        "open_obligation": "  user owes X  ",
        "reason": "r",
    })
    result = _parse_llm_output(raw, target_version=1)
    assert result.open_obligation == "user owes X"


def test_parse_next_owner_empty_open_obligation_becomes_none():
    raw = json.dumps({
        "next_owner": "user",
        "open_obligation": "   ",
        "reason": "r",
    })
    result = _parse_llm_output(raw, target_version=1)
    assert result.open_obligation is None


def test_parse_next_owner_wins_over_decision_when_both_present():
    """If both keys are present, next_owner wins (v3 contract)."""
    raw = json.dumps({
        "next_owner": "other",
        "decision": "waiting_for_me",
        "reason": "r",
    })
    result = _parse_llm_output(raw, target_version=1)
    assert result.decision == WaitingForMeDecision.NOT_WAITING_FOR_ME
    assert result.next_owner is not None
    assert result.next_owner.value == "other"


# --- owner-inversion scenario test ------------------------------------------
# The core bug this change fixes: a user-originated unanswered question
# must classify as OTHER (→ NOT_WAITING_FOR_ME), not WAITING_FOR_ME.


async def test_owner_inversion_user_question_is_not_waiting_for_me():
    """User asks a direct question; next_owner should be OTHER → NWM."""
    conv = _make_conversation([("outbound", "הבאת קופסה קטנה יותר?")])
    response = _mock_openai_response(
        json.dumps({
            "next_owner": "other",
            "open_obligation": "other person must answer whether they brought a smaller box",
            "confidence": 0.95,
            "reason": "User asked a direct question; other person must answer next.",
        })
    )
    client = _mock_client(response)
    analyzer = LLMWaitingForMeAnalyzer(client=client, prompt_version="v3")
    result = await analyzer.analyze(conv)
    assert result.decision == WaitingForMeDecision.NOT_WAITING_FOR_ME
    assert result.next_owner is not None
    assert result.next_owner.value == "other"


# --- legacy decision contract still works (backward compat) ----------------


def test_parse_legacy_decision_still_works_with_next_owner_none():
    """v0–v2 prompts emit `decision` directly; next_owner stays None."""
    raw = json.dumps({"decision": "waiting_for_me", "confidence": 0.9, "reason": "r"})
    result = _parse_llm_output(raw, target_version=1)
    assert result.decision == WaitingForMeDecision.WAITING_FOR_ME
    assert result.next_owner is None
    assert result.open_obligation is None


# --- additional coverage ----------------------------------------------------


async def test_waiting_for_me_analyzer_protocol_raises_not_implemented():
    """The Protocol's default analyze body raises NotImplementedError."""
    from echo_v2.services.waiting_for_me_analyzer import WaitingForMeAnalyzer

    with pytest.raises(NotImplementedError):
        await WaitingForMeAnalyzer.analyze(None, None)  # type: ignore[arg-type]


async def test_analyze_with_raw_returns_result_and_raw():
    """analyze_with_raw returns both the result and the raw LLM response."""
    raw = json.dumps({
        "next_owner": "user",
        "open_obligation": "user owes a reply",
        "confidence": 0.9,
        "reason": "User must reply.",
    })
    response = _mock_openai_response(raw)
    client = _mock_client(response)

    analyzer = LLMWaitingForMeAnalyzer(client=client, prompt_version="v3")
    conv = _make_conversation()
    result, raw_output = await analyzer.analyze_with_raw(conv)

    assert result.decision == WaitingForMeDecision.WAITING_FOR_ME
    assert raw_output == raw


async def test_analyze_reasoning_model_uses_larger_token_budget():
    """Reasoning models (gpt-5+) get max_completion_tokens=4000, no temperature."""
    raw = json.dumps({
        "next_owner": "user",
        "open_obligation": "user owes a reply",
        "confidence": 0.9,
        "reason": "User must reply.",
    })
    response = _mock_openai_response(raw)
    client = _mock_client(response)

    analyzer = LLMWaitingForMeAnalyzer(client=client, model="gpt-5", prompt_version="v3")
    conv = _make_conversation()
    await analyzer.analyze(conv)

    call_kwargs = client.chat.completions.create.await_args.kwargs
    assert call_kwargs["max_completion_tokens"] == 4000
    assert "temperature" not in call_kwargs


def test_parse_llm_output_not_a_dict_raises():
    """When the LLM returns a JSON array (not an object), AnalysisError is raised."""
    with pytest.raises(AnalysisError, match="not a JSON object"):
        _parse_llm_output("[1, 2, 3]", target_version=1)


# --- waiting_for_me_prompts -------------------------------------------------


def test_get_prompt_unknown_version_raises():
    from echo_v2.services.waiting_for_me_prompts import get_prompt

    with pytest.raises(ValueError, match="Unknown prompt version"):
        get_prompt("v999")


def test_get_prompt_default_returns_prompt():
    from echo_v2.services.waiting_for_me_prompts import (
        DEFAULT_PROMPT_VERSION,
        get_prompt,
    )

    prompt = get_prompt()
    assert isinstance(prompt, str)
    assert len(prompt) > 0
    # Default version should match.
    assert get_prompt(DEFAULT_PROMPT_VERSION) == prompt
