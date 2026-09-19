"""Tests for AnalysisJudge, _build_transcript, and _parse_judge_output."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from echo_v2.domain.waiting_for_me import WaitingForMeDecision, WaitingForMeResult
from echo_v2.services.analysis_judge import (
    AnalysisJudge,
    JudgeResult,
    _build_transcript,
    _parse_judge_output,
)
from echo_v2.services.chat_analysis_worker import ConversationInput

NOW = datetime(2026, 9, 5, 10, 0, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 5, 10, 5, 0, tzinfo=timezone.utc)


def _make_conversation() -> ConversationInput:
    return ConversationInput(
        user_id="user-1",
        chat_id="972501234567@c.us",
        target_version=1,
        messages=[
            ("inbound", "Are you free on Thursday?", NOW),
            ("outbound", "Let me check and get back to you", LATER),
        ],
    )


def _make_result(
    decision: WaitingForMeDecision = WaitingForMeDecision.WAITING_FOR_ME,
    confidence: float = 0.9,
    reason: str | None = "User must reply.",
) -> WaitingForMeResult:
    return WaitingForMeResult(
        decision=decision,
        confidence=confidence,
        reason=reason,
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


# --- _build_transcript -----------------------------------------------------


def test_build_transcript_renders_inbound_as_them_outbound_as_me():
    transcript = _build_transcript(_make_conversation())
    assert "[2026-09-05 10:00] them: Are you free on Thursday?" in transcript
    assert "[2026-09-05 10:05] me: Let me check and get back to you" in transcript


def test_build_transcript_single_inbound_message():
    conv = ConversationInput(
        user_id="u",
        chat_id="c",
        target_version=1,
        messages=[("inbound", "Hello", NOW)],
    )
    assert _build_transcript(conv) == "[2026-09-05 10:00] them: Hello"


def test_build_transcript_empty_messages():
    conv = ConversationInput(
        user_id="u",
        chat_id="c",
        target_version=1,
        messages=[],
    )
    assert _build_transcript(conv) == ""


# --- _parse_judge_output ----------------------------------------------------


def test_parse_judge_output_valid_score_one():
    result = _parse_judge_output(json.dumps({"score": 1.0, "explanation": "correct"}))
    assert result.score == 1.0
    assert result.explanation == "correct"


def test_parse_judge_output_valid_score_zero():
    result = _parse_judge_output(json.dumps({"score": 0.0, "explanation": "wrong"}))
    assert result.score == 0.0
    assert result.explanation == "wrong"


def test_parse_judge_output_valid_score_half():
    result = _parse_judge_output(json.dumps({"score": 0.5, "explanation": "maybe"}))
    assert result.score == 0.5
    assert result.explanation == "maybe"


def test_parse_judge_output_with_markdown_code_fence_json():
    raw = "```json\n" + json.dumps({"score": 1.0, "explanation": "fenced"}) + "\n```"
    result = _parse_judge_output(raw)
    assert result.score == 1.0
    assert result.explanation == "fenced"


def test_parse_judge_output_with_markdown_code_fence_no_newline():
    raw = "```" + json.dumps({"score": 0.0, "explanation": "nofence"}) + "```"
    result = _parse_judge_output(raw)
    assert result.score == 0.0
    assert result.explanation == "nofence"


def test_parse_judge_output_invalid_json_returns_half():
    result = _parse_judge_output("not json at all")
    assert result.score == 0.5
    assert result.explanation == "judge output parse error"


def test_parse_judge_output_missing_score_returns_half():
    result = _parse_judge_output(json.dumps({"explanation": "no score"}))
    assert result.score == 0.5
    assert result.explanation == "judge output missing score"


def test_parse_judge_output_non_numeric_score_returns_half():
    result = _parse_judge_output(json.dumps({"score": "high", "explanation": "bad"}))
    assert result.score == 0.5
    assert result.explanation == "bad"


def test_parse_judge_output_clamp_high_to_one():
    result = _parse_judge_output(json.dumps({"score": 0.75, "explanation": "clamped up"}))
    assert result.score == 1.0


def test_parse_judge_output_clamp_low_to_zero():
    result = _parse_judge_output(json.dumps({"score": 0.25, "explanation": "clamped down"}))
    assert result.score == 0.0


def test_parse_judge_output_clamp_mid_to_half():
    result = _parse_judge_output(json.dumps({"score": 0.5, "explanation": "stays"}))
    assert result.score == 0.5


def test_parse_judge_output_missing_explanation_defaults_empty():
    result = _parse_judge_output(json.dumps({"score": 1.0}))
    assert result.score == 1.0
    assert result.explanation == ""


# --- AnalysisJudge.judge ---------------------------------------------------


async def test_judge_successful_returns_parsed_result():
    response = _mock_openai_response(
        json.dumps({"score": 1.0, "explanation": "decision is correct"})
    )
    client = _mock_client(response)
    judge = AnalysisJudge(client=client, model="gpt-test")

    result = await judge.judge(_make_conversation(), _make_result())

    assert isinstance(result, JudgeResult)
    assert result.score == 1.0
    assert result.explanation == "decision is correct"
    assert result.run_id is None
    assert result.run_start_time is None


async def test_judge_llm_failure_returns_half_with_error():
    client = _mock_client(side_effect=RuntimeError("API down"))
    judge = AnalysisJudge(client=client, model="gpt-test")

    result = await judge.judge(_make_conversation(), _make_result())

    assert result.score == 0.5
    assert "judge error: API down" in result.explanation


async def test_judge_user_message_contains_transcript_decision_confidence_reason():
    response = _mock_openai_response(
        json.dumps({"score": 0.0, "explanation": "wrong decision"})
    )
    client = _mock_client(response)
    judge = AnalysisJudge(client=client, model="gpt-test")

    await judge.judge(_make_conversation(), _make_result(reason="User owes a reply."))

    call_kwargs = client.chat.completions.create.call_args
    messages = call_kwargs.kwargs["messages"]
    user_content = messages[1]["content"]

    assert "Are you free on Thursday?" in user_content
    assert "Let me check and get back to you" in user_content
    assert "waiting_for_me" in user_content
    assert "0.9" in user_content
    assert "User owes a reply." in user_content


async def test_judge_user_message_with_none_reason_shows_none_placeholder():
    response = _mock_openai_response(
        json.dumps({"score": 0.5, "explanation": "uncertain"})
    )
    client = _mock_client(response)
    judge = AnalysisJudge(client=client, model="gpt-test")

    await judge.judge(_make_conversation(), _make_result(reason=None))

    call_kwargs = client.chat.completions.create.call_args
    messages = call_kwargs.kwargs["messages"]
    user_content = messages[1]["content"]

    assert "(none)" in user_content


async def test_judge_with_run_tree_context():
    """When called inside a traceable context, run_id and run_start_time are set."""
    response = _mock_openai_response(
        json.dumps({"score": 1.0, "explanation": "ok"})
    )
    client = _mock_client(response)
    judge = AnalysisJudge(client=client, model="gpt-test")

    fake_run = MagicMock()
    fake_run.id = "run-123"
    fake_run.start_time = datetime(2026, 9, 5, 9, 0, 0, tzinfo=timezone.utc)

    with patch(
        "langsmith.run_helpers.get_current_run_tree",
        return_value=fake_run,
    ):
        result = await judge.judge(_make_conversation(), _make_result())

    assert result.run_id == "run-123"
    assert result.run_start_time == "2026-09-05T09:00:00+00:00"
