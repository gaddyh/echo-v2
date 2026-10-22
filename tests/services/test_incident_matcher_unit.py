"""Unit tests for the IncidentMatcher with a fake LLM client.

Exercises all branches of incident_matcher.py without requiring a real API key:
- Constructor validation
- match() with no incidents → NEW without LLM
- match() with incidents → LLM call
- LLM returns valid NEW / SAME / ESCALATION
- LLM returns invalid output (incident_id mismatch)
- LLM raises ValidationError → retry
- LLM raises generic exception → retry
- Reasoning model branch (max_completion_tokens without temperature)
- _build_user_message and _result_from_output helpers
- MatcherAssessment.from_safety / from_exposure
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from echo_v2.domain.guard_exposure import ExposureAssessment, ExposureCategory
from echo_v2.domain.guard_simple import ChildRole, SafetyAssessment
from echo_v2.services.incident_matcher import (
    INCIDENT_MATCHER_VERSION,
    IncidentCandidate,
    IncidentMatchDecision,
    IncidentMatcher,
    IncidentMatcherLLMOutput,
    IncidentMatchError,
    MatcherAssessment,
    _build_user_message,
    _result_from_output,
)

pytestmark = pytest.mark.asyncio

_T0 = datetime(2026, 10, 22, 8, tzinfo=timezone.utc)


def _make_safety(**kw: Any) -> SafetyAssessment:
    defaults: dict[str, Any] = {
        "parent_should_know": True,
        "child_role": ChildRole.TARGET,
        "summary": "Pain and breathing",
        "evidence_message_ids": ("msg-1",),
        "reason": "test",
        "child_user_id": "child-1",
        "child_name": "Mikush",
        "chat_id": "chat-1",
        "created_at": _T0,
    }
    defaults.update(kw)
    return SafetyAssessment(**defaults)


def _make_exposure(**kw: Any) -> ExposureAssessment:
    defaults: dict[str, Any] = {
        "significant_exposure": True,
        "category": ExposureCategory.VIOLENCE,
        "summary": "Violent content",
        "evidence_message_ids": ("msg-1",),
        "reason": "test",
        "child_user_id": "child-1",
        "child_name": "Mikush",
        "chat_id": "chat-1",
        "created_at": _T0,
    }
    defaults.update(kw)
    return ExposureAssessment(**defaults)


def _make_candidate(**kw: Any) -> IncidentCandidate:
    defaults: dict[str, Any] = {
        "id": "inc-1",
        "alert_type": "security",
        "issue_summary": "Bullying",
        "severity_summary": "Bullying",
        "first_seen_at": _T0,
        "last_seen_at": _T0,
        "evidence_message_ids": ("msg-1",),
    }
    defaults.update(kw)
    return IncidentCandidate(**defaults)


def _make_assessment(**kw: Any) -> MatcherAssessment:
    defaults: dict[str, Any] = {
        "summary": "Pain",
        "reason": "test",
        "evidence_message_ids": ("msg-1",),
        "child_user_id": "child-1",
        "chat_id": "chat-1",
        "created_at": _T0,
    }
    defaults.update(kw)
    return MatcherAssessment(**defaults)


# ---------------------------------------------------------------------------
# Fake LLM client
# ---------------------------------------------------------------------------


class FakeParsedMessage:
    def __init__(self, parsed: Any) -> None:
        self.parsed = parsed


class FakeChoice:
    def __init__(self, parsed: Any) -> None:
        self.message = FakeParsedMessage(parsed)


class FakeResponse:
    def __init__(self, parsed: Any) -> None:
        self.choices = [FakeChoice(parsed)]


class FakeChat:
    def __init__(self) -> None:
        self.completions = MagicMock()
        self.completions.parse = AsyncMock()
        self.calls: list[dict[str, Any]] = []

    def set_response(self, parsed: Any) -> None:
        self.completions.parse.return_value = FakeResponse(parsed)

    def set_exception(self, exc: Exception) -> None:
        self.completions.parse.side_effect = exc

    def set_sequence(self, *responses: Any) -> None:
        """Return different responses on successive calls (for retry tests)."""
        fakes = [FakeResponse(r) for r in responses]
        self.completions.parse.side_effect = fakes


class FakeClient:
    def __init__(self) -> None:
        self.chat = FakeChat()


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


def test_constructor_rejects_negative_retries() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        IncidentMatcher(client=FakeClient(), max_retries=-1)


def test_constructor_accepts_zero_retries() -> None:
    matcher = IncidentMatcher(client=FakeClient(), max_retries=0)
    assert matcher._max_retries == 0


def test_reasoning_model_flag() -> None:
    """Models starting with gpt-5/gpt-6/o should set _is_reasoning_model=True."""
    for prefix in ("gpt-5", "gpt-6", "o1", "o3-mini"):
        matcher = IncidentMatcher(client=FakeClient(), model=prefix)
        assert matcher._is_reasoning_model is True, f"{prefix} should be reasoning"

    for model in ("gpt-4.1", "gpt-4o", "claude-3"):
        matcher = IncidentMatcher(client=FakeClient(), model=model)
        assert matcher._is_reasoning_model is False, f"{model} should not be reasoning"


# ---------------------------------------------------------------------------
# match() with no incidents → NEW without LLM
# ---------------------------------------------------------------------------


async def test_match_no_incidents_returns_new_without_llm() -> None:
    client = FakeClient()
    matcher = IncidentMatcher(client=client)
    result = await matcher.match(
        alert_type="security",
        assessment=_make_assessment(),
        incidents=(),
    )
    assert result.decision is IncidentMatchDecision.NEW_INCIDENT
    assert result.incident_id is None
    assert "No existing incidents" in result.reason
    client.chat.completions.parse.assert_not_called()


# ---------------------------------------------------------------------------
# match() with incidents → LLM call
# ---------------------------------------------------------------------------


async def test_match_with_incidents_calls_llm_new() -> None:
    client = FakeClient()
    client.chat.set_response(
        IncidentMatcherLLMOutput(
            decision="new_incident",
            incident_id=None,
            reason="distinct issue",
        )
    )
    matcher = IncidentMatcher(client=client)
    result = await matcher.match(
        alert_type="security",
        assessment=_make_assessment(),
        incidents=(_make_candidate(),),
    )
    assert result.decision is IncidentMatchDecision.NEW_INCIDENT
    assert result.incident_id is None
    assert result.reason == "distinct issue"
    client.chat.completions.parse.assert_awaited_once()


async def test_match_with_incidents_calls_llm_same() -> None:
    client = FakeClient()
    client.chat.set_response(
        IncidentMatcherLLMOutput(
            decision="same_incident",
            incident_id="inc-1",
            reason="same issue",
        )
    )
    matcher = IncidentMatcher(client=client)
    result = await matcher.match(
        alert_type="security",
        assessment=_make_assessment(),
        incidents=(_make_candidate(),),
    )
    assert result.decision is IncidentMatchDecision.SAME_INCIDENT
    assert result.incident_id == "inc-1"


async def test_match_with_incidents_calls_llm_escalation() -> None:
    client = FakeClient()
    client.chat.set_response(
        IncidentMatcherLLMOutput(
            decision="significant_escalation",
            incident_id="inc-1",
            reason="weapon involved",
        )
    )
    matcher = IncidentMatcher(client=client)
    result = await matcher.match(
        alert_type="security",
        assessment=_make_assessment(),
        incidents=(_make_candidate(),),
    )
    assert result.decision is IncidentMatchDecision.SIGNIFICANT_ESCALATION
    assert result.incident_id == "inc-1"


# ---------------------------------------------------------------------------
# LLM returns invalid output
# ---------------------------------------------------------------------------


async def test_match_new_with_incident_id_raises() -> None:
    """LLM says new_incident but returns an incident_id → error."""
    client = FakeClient()
    client.chat.set_response(
        IncidentMatcherLLMOutput(
            decision="new_incident",
            incident_id="inc-1",  # should be None for NEW
            reason="bad output",
        )
    )
    matcher = IncidentMatcher(client=client, max_retries=0)
    with pytest.raises(IncidentMatchError, match="must not reference"):
        await matcher.match(
            alert_type="security",
            assessment=_make_assessment(),
            incidents=(_make_candidate(),),
        )


async def test_match_same_without_incident_id_raises() -> None:
    """LLM says same_incident but no incident_id → error."""
    client = FakeClient()
    client.chat.set_response(
        IncidentMatcherLLMOutput(
            decision="same_incident",
            incident_id=None,  # should be set for SAME
            reason="bad output",
        )
    )
    matcher = IncidentMatcher(client=client, max_retries=0)
    with pytest.raises(IncidentMatchError, match="must reference"):
        await matcher.match(
            alert_type="security",
            assessment=_make_assessment(),
            incidents=(_make_candidate(),),
        )


# ---------------------------------------------------------------------------
# LLM returns no parsed output
# ---------------------------------------------------------------------------


async def test_match_llm_returns_no_parsed_output() -> None:
    client = FakeClient()
    client.chat.set_response(None)  # parsed is None
    matcher = IncidentMatcher(client=client, max_retries=0)
    with pytest.raises(IncidentMatchError, match="no parsed output"):
        await matcher.match(
            alert_type="security",
            assessment=_make_assessment(),
            incidents=(_make_candidate(),),
        )


# ---------------------------------------------------------------------------
# Retry behavior
# ---------------------------------------------------------------------------


async def test_match_retries_on_error_then_succeeds() -> None:
    """First call fails, second succeeds with max_retries=1."""
    client = FakeClient()
    good = IncidentMatcherLLMOutput(
        decision="same_incident",
        incident_id="inc-1",
        reason="same",
    )
    client.chat.set_sequence(
        None,  # first attempt: no parsed output
        good,  # second attempt: success
    )
    matcher = IncidentMatcher(client=client, max_retries=1)
    result = await matcher.match(
        alert_type="security",
        assessment=_make_assessment(),
        incidents=(_make_candidate(),),
    )
    assert result.decision is IncidentMatchDecision.SAME_INCIDENT
    assert client.chat.completions.parse.await_count == 2


async def test_match_retries_on_generic_exception() -> None:
    """Generic provider exception is retried."""
    client = FakeClient()
    good = IncidentMatcherLLMOutput(
        decision="new_incident",
        incident_id=None,
        reason="new",
    )
    client.chat.set_sequence(
        RuntimeError("provider timeout"),
        good,
    )
    matcher = IncidentMatcher(client=client, max_retries=1)
    result = await matcher.match(
        alert_type="security",
        assessment=_make_assessment(),
        incidents=(_make_candidate(),),
    )
    assert result.decision is IncidentMatchDecision.NEW_INCIDENT


async def test_match_exhausts_retries_and_raises() -> None:
    """All retries fail → raises last error."""
    client = FakeClient()
    client.chat.set_exception(ConnectionError("network down"))
    matcher = IncidentMatcher(client=client, max_retries=1)
    with pytest.raises(IncidentMatchError, match="request failed"):
        await matcher.match(
            alert_type="security",
            assessment=_make_assessment(),
            incidents=(_make_candidate(),),
        )
    assert client.chat.completions.parse.await_count == 2  # initial + 1 retry


async def test_match_retries_on_validation_error() -> None:
    """ValidationError from pydantic is retried."""
    client = FakeClient()
    good = IncidentMatcherLLMOutput(
        decision="new_incident",
        incident_id=None,
        reason="new",
    )
    # First call returns an invalid type that triggers ValidationError,
    # second call returns valid output.
    client.chat.set_sequence(
        "not-a-valid-output",  # triggers ValidationError
        good,
    )
    matcher = IncidentMatcher(client=client, max_retries=1)
    result = await matcher.match(
        alert_type="security",
        assessment=_make_assessment(),
        incidents=(_make_candidate(),),
    )
    assert result.decision is IncidentMatchDecision.NEW_INCIDENT
    assert client.chat.completions.parse.await_count == 2


# ---------------------------------------------------------------------------
# Reasoning model branch
# ---------------------------------------------------------------------------


async def test_reasoning_model_uses_max_completion_tokens() -> None:
    """Reasoning models should set max_completion_tokens without temperature."""
    client = FakeClient()
    client.chat.set_response(
        IncidentMatcherLLMOutput(
            decision="new_incident",
            incident_id=None,
            reason="new",
        )
    )
    matcher = IncidentMatcher(client=client, model="o3-mini")
    await matcher.match(
        alert_type="security",
        assessment=_make_assessment(),
        incidents=(_make_candidate(),),
    )
    call_kwargs = client.chat.completions.parse.call_args.kwargs
    assert "max_completion_tokens" in call_kwargs
    assert "temperature" not in call_kwargs


async def test_non_reasoning_model_sets_temperature_zero() -> None:
    """Non-reasoning models should set temperature=0."""
    client = FakeClient()
    client.chat.set_response(
        IncidentMatcherLLMOutput(
            decision="new_incident",
            incident_id=None,
            reason="new",
        )
    )
    matcher = IncidentMatcher(client=client, model="gpt-4.1")
    await matcher.match(
        alert_type="security",
        assessment=_make_assessment(),
        incidents=(_make_candidate(),),
    )
    call_kwargs = client.chat.completions.parse.call_args.kwargs
    assert call_kwargs["temperature"] == 0
    assert "max_completion_tokens" in call_kwargs


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def test_build_user_message_valid_json() -> None:
    """_build_user_message should produce valid JSON with all fields."""
    msg = _build_user_message(
        "security",
        _make_assessment(),
        (_make_candidate(),),
    )
    import json

    parsed = json.loads(msg)
    assert parsed["alert_type"] == "security"
    assert "new_assessment" in parsed
    assert "existing_incidents" in parsed
    assert len(parsed["existing_incidents"]) == 1
    assert parsed["existing_incidents"][0]["id"] == "inc-1"


def test_result_from_output_new() -> None:
    output = IncidentMatcherLLMOutput(
        decision="new_incident",
        incident_id=None,
        reason="test",
    )
    result = _result_from_output(output)
    assert result.decision is IncidentMatchDecision.NEW_INCIDENT
    assert result.incident_id is None


def test_result_from_output_same() -> None:
    output = IncidentMatcherLLMOutput(
        decision="same_incident",
        incident_id="inc-1",
        reason="test",
    )
    result = _result_from_output(output)
    assert result.decision is IncidentMatchDecision.SAME_INCIDENT
    assert result.incident_id == "inc-1"


def test_result_from_output_escalation() -> None:
    output = IncidentMatcherLLMOutput(
        decision="significant_escalation",
        incident_id="inc-1",
        reason="test",
    )
    result = _result_from_output(output)
    assert result.decision is IncidentMatchDecision.SIGNIFICANT_ESCALATION


def test_result_from_output_new_with_id_raises() -> None:
    output = IncidentMatcherLLMOutput(
        decision="new_incident",
        incident_id="inc-1",
        reason="test",
    )
    with pytest.raises(IncidentMatchError, match="must not reference"):
        _result_from_output(output)


def test_result_from_output_same_without_id_raises() -> None:
    output = IncidentMatcherLLMOutput(
        decision="same_incident",
        incident_id=None,
        reason="test",
    )
    with pytest.raises(IncidentMatchError, match="must reference"):
        _result_from_output(output)


# ---------------------------------------------------------------------------
# MatcherAssessment adapters
# ---------------------------------------------------------------------------


def test_matcher_assessment_from_safety() -> None:
    safety = _make_safety(summary="Pain", reason="test", evidence_message_ids=("msg-1",))
    ma = MatcherAssessment.from_safety(safety)
    assert ma.summary == "Pain"
    assert ma.reason == "test"
    assert ma.evidence_message_ids == ("msg-1",)
    assert ma.child_user_id == "child-1"
    assert ma.chat_id == "chat-1"


def test_matcher_assessment_from_safety_defaults_created_at() -> None:
    """When created_at is None, from_safety should use now()."""
    safety = _make_safety(created_at=None)
    ma = MatcherAssessment.from_safety(safety)
    assert ma.created_at is not None


def test_matcher_assessment_from_exposure() -> None:
    exposure = _make_exposure(summary="Violent", reason="test", evidence_message_ids=("msg-1",))
    ma = MatcherAssessment.from_exposure(exposure)
    assert ma.summary == "Violent"
    assert ma.reason == "test"
    assert ma.evidence_message_ids == ("msg-1",)


def test_matcher_assessment_from_exposure_defaults_created_at() -> None:
    exposure = _make_exposure(created_at=None)
    ma = MatcherAssessment.from_exposure(exposure)
    assert ma.created_at is not None


# ---------------------------------------------------------------------------
# Version constants
# ---------------------------------------------------------------------------


def test_matcher_version_constant() -> None:
    assert INCIDENT_MATCHER_VERSION == "2026-10-22.1"
