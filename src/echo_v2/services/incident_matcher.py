"""Incident Matcher — Layer 1 of the Echo Guard Incident Tracker.

A narrow, specialized LLM step that resolves whether a new YES assessment
describes the same ongoing issue as a recent incident, a significant escalation
of an existing issue, or a distinct new issue.

The matcher is side-effect-free with respect to application state: it invokes
an LLM but creates no incidents, sends no alerts, and modifies no state.
Lifecycle decisions (active/quiet/closed, suppress/notify) are the
responsibility of the Incident Service (Layer 3).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import uuid4

from langsmith import traceable
from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError

from echo_v2.domain.ai_usage_event import AiUsageStatus, AiUsageType
from echo_v2.domain.guard_exposure import ExposureAssessment
from echo_v2.domain.guard_simple import SafetyAssessment
from echo_v2.observability.tracing import tracing_client

if TYPE_CHECKING:
    from echo_v2.services.reporting.ai_usage_recorder import AiUsageRecorder

_logger = logging.getLogger("echo_v2.services.incident_matcher")

INCIDENT_MATCHER_PROMPT_VERSION = "incident-matcher-v1"
INCIDENT_MATCHER_VERSION = "2026-10-22.1"


class IncidentMatchDecision(str, Enum):
    NEW_INCIDENT = "new_incident"
    SAME_INCIDENT = "same_incident"
    SIGNIFICANT_ESCALATION = "significant_escalation"


@dataclass(frozen=True)
class MatcherAssessment:
    """Normalized view of a YES assessment for incident matching.

    Decouples the matcher from both ``SafetyAssessment`` and
    ``ExposureAssessment`` domain types.
    """

    summary: str
    reason: str
    evidence_message_ids: tuple[str, ...]
    child_user_id: str
    chat_id: str
    created_at: datetime

    @classmethod
    def from_safety(cls, a: SafetyAssessment) -> MatcherAssessment:
        return cls(
            summary=a.summary,
            reason=a.reason,
            evidence_message_ids=a.evidence_message_ids,
            child_user_id=a.child_user_id,
            chat_id=a.chat_id,
            created_at=a.created_at or datetime.now(timezone.utc),
        )

    @classmethod
    def from_exposure(cls, a: ExposureAssessment) -> MatcherAssessment:
        return cls(
            summary=a.summary,
            reason=a.reason,
            evidence_message_ids=a.evidence_message_ids,
            child_user_id=a.child_user_id,
            chat_id=a.chat_id,
            created_at=a.created_at or datetime.now(timezone.utc),
        )


@dataclass(frozen=True)
class IncidentCandidate:
    """A previous incident presented to the matcher for comparison."""

    id: str
    alert_type: str
    issue_summary: str
    severity_summary: str
    first_seen_at: datetime
    last_seen_at: datetime
    evidence_message_ids: tuple[str, ...]


@dataclass(frozen=True)
class IncidentMatchResult:
    decision: IncidentMatchDecision
    incident_id: str | None
    reason: str


class IncidentMatcherLLMOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["new_incident", "same_incident", "significant_escalation"]
    incident_id: str | None
    reason: StrictStr


class IncidentMatchError(Exception):
    """Raised when the incident matcher LLM call or output is invalid."""


class ChatCompletionClient(Protocol):
    chat: Any


_SYSTEM_PROMPT = """\
You are an incident matcher for a child-safety monitoring system. Your job is
narrow: given a new safety assessment and a list of previously identified
incidents for the same child, decide whether the new assessment:

1. Describes the SAME ongoing issue as an existing incident (same_incident).
2. Describes a SIGNIFICANT ESCALATION of an existing incident — the same issue
   but the risk has materially increased (significant_escalation).
3. Describes a DISTINCT NEW issue that is unrelated to any existing incident
   (new_incident).

You do NOT decide whether content is dangerous. You do NOT decide whether to
notify parents. You ONLY resolve incident identity and escalation.

Rules for matching:
- Two findings describe the same incident when they concern the same underlying
  situation, even if the specific messages, evidence IDs, or wording differ.
  For example, a child reporting ongoing pain at 10:00 and again at 11:00 is
  one incident, not two.
- Evidence message ID overlap is a positive signal but NOT a requirement. The
  same ongoing issue may produce completely different evidence IDs across
  assessments.
- A significant escalation is when the SAME issue has materially worsened — for
  example, verbal bullying that escalates into a credible physical threat.
  Repeated descriptions of the same severity level are NOT an escalation.
- To detect escalation, compare the new assessment against the
  "severity_summary" field of each incident, which records the most serious
  risk observed so far. If the new assessment does not represent a higher
  severity than what was already observed, it is same_incident, not
  significant_escalation.
- Two genuinely unrelated issues in the same chat are new_incident, not
  same_incident. For example, cyberbullying and a separate threat to share a
  private photo are two distinct incidents.

Return your decision as structured output with:
- decision: "new_incident", "same_incident", or "significant_escalation"
- incident_id: the ID of the matched incident for same_incident or
  significant_escalation; null for new_incident
- reason: a brief explanation of your decision
"""


class IncidentMatcher:
    """LLM-backed incident identity resolver.

    Shared by Security and Exposure analyzers. The ``alert_type`` parameter
    provides context but does not change the matching logic.
    """

    def __init__(
        self,
        client: ChatCompletionClient,
        model: str = "gpt-4.1",
        prompt_version: str = INCIDENT_MATCHER_PROMPT_VERSION,
        matcher_version: str = INCIDENT_MATCHER_VERSION,
        max_retries: int = 1,
        recorder: AiUsageRecorder | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        self._client = client
        self._model = model
        self._prompt_version = prompt_version
        self._matcher_version = matcher_version
        self._max_retries = max_retries
        self._is_reasoning_model = model.startswith(("gpt-5", "gpt-6", "o"))
        self._recorder = recorder

    async def match(
        self,
        *,
        alert_type: str,
        assessment: MatcherAssessment,
        incidents: tuple[IncidentCandidate, ...],
    ) -> IncidentMatchResult:
        if not incidents:
            return IncidentMatchResult(
                decision=IncidentMatchDecision.NEW_INCIDENT,
                incident_id=None,
                reason="No existing incidents to match against.",
            )
        return await self._match_with_llm(alert_type, assessment, incidents)

    @traceable(
        name="guard.incident_matcher",
        client=tracing_client,
    )
    async def _match_with_llm(
        self,
        alert_type: str,
        assessment: MatcherAssessment,
        incidents: tuple[IncidentCandidate, ...],
    ) -> IncidentMatchResult:
        request_kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_message(alert_type, assessment, incidents)},
            ],
            "response_format": IncidentMatcherLLMOutput,
        }
        if self._is_reasoning_model:
            request_kwargs["max_completion_tokens"] = 800
        else:
            request_kwargs["temperature"] = 0
            request_kwargs["max_completion_tokens"] = 300

        last_error: IncidentMatchError | None = None
        for attempt in range(self._max_retries + 1):
            invocation_id = uuid4()
            try:
                response = await self._client.chat.completions.parse(**request_kwargs)
                message = response.choices[0].message
                parsed = message.parsed
                if not isinstance(parsed, IncidentMatcherLLMOutput):
                    raise IncidentMatchError("Incident matcher LLM returned no parsed output")
                usage = getattr(response, "usage", None)
                await self._record_usage(
                    assessment=assessment,
                    usage=usage,
                    status=AiUsageStatus.SUCCESS,
                    invocation_id=invocation_id,
                )
                return _result_from_output(parsed)
            except IncidentMatchError as exc:
                last_error = exc
                await self._record_usage(
                    assessment=assessment,
                    usage=None,
                    status=AiUsageStatus.ERROR,
                    invocation_id=invocation_id,
                    error_type=type(exc).__name__,
                )
            except (ValidationError, ValueError) as exc:
                last_error = IncidentMatchError(f"Invalid incident matcher output: {exc}")
                await self._record_usage(
                    assessment=assessment,
                    usage=None,
                    status=AiUsageStatus.ERROR,
                    invocation_id=invocation_id,
                    error_type=type(exc).__name__,
                )
            except Exception as exc:  # noqa: BLE001 - provider failures are retried
                last_error = IncidentMatchError(f"Incident matcher LLM request failed: {exc}")
                await self._record_usage(
                    assessment=assessment,
                    usage=None,
                    status=AiUsageStatus.ERROR,
                    invocation_id=invocation_id,
                    error_type=type(exc).__name__,
                )
            if attempt < self._max_retries:
                _logger.warning(
                    "Incident matcher retrying (%d/%d): %s",
                    attempt + 1,
                    self._max_retries,
                    last_error,
                )
        assert last_error is not None
        raise last_error

    async def _record_usage(
        self,
        *,
        assessment: MatcherAssessment,
        usage: Any,
        status: str,
        invocation_id: Any,
        error_type: str | None = None,
    ) -> None:
        """Record one billable provider attempt. Best-effort, never breaks analysis."""
        if self._recorder is None:
            return
        from echo_v2.services.reporting.ai_usage_recorder import (
            UsageContext,
            UsagePayload,
        )

        def _uv(field: str) -> int | None:
            value = getattr(usage, field, None) if usage is not None else None
            return value if isinstance(value, int) else None

        context = UsageContext(
            operation="guard_incident_match",
            provider="openai",
            model=self._model,
            child_user_id=assessment.child_user_id or None,
        )
        payload = UsagePayload(
            usage_type=AiUsageType.TOKENS,
            status=status,
            prompt_tokens=_uv("prompt_tokens"),
            completion_tokens=_uv("completion_tokens"),
            total_tokens=_uv("total_tokens"),
            invocation_id=invocation_id,
            error_type=error_type if status == AiUsageStatus.ERROR else None,
        )
        await self._recorder.record(context, payload)


def _build_user_message(
    alert_type: str,
    assessment: MatcherAssessment,
    incidents: tuple[IncidentCandidate, ...],
) -> str:
    payload: dict[str, object] = {
        "alert_type": alert_type,
        "new_assessment": {
            "summary": assessment.summary,
            "reason": assessment.reason,
            "evidence_message_ids": list(assessment.evidence_message_ids),
            "created_at": assessment.created_at.isoformat(),
        },
        "existing_incidents": [
            {
                "id": inc.id,
                "alert_type": inc.alert_type,
                "issue_summary": inc.issue_summary,
                "severity_summary": inc.severity_summary,
                "first_seen_at": inc.first_seen_at.isoformat(),
                "last_seen_at": inc.last_seen_at.isoformat(),
                "evidence_message_ids": list(inc.evidence_message_ids),
            }
            for inc in incidents
        ],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _result_from_output(output: IncidentMatcherLLMOutput) -> IncidentMatchResult:
    decision = IncidentMatchDecision(output.decision)
    if decision is IncidentMatchDecision.NEW_INCIDENT and output.incident_id is not None:
        raise IncidentMatchError("new_incident decision must not reference an incident_id")
    if decision is not IncidentMatchDecision.NEW_INCIDENT and not output.incident_id:
        raise IncidentMatchError(f"{output.decision} decision must reference a valid incident_id")
    return IncidentMatchResult(
        decision=decision,
        incident_id=output.incident_id,
        reason=output.reason,
    )


__all__ = [
    "INCIDENT_MATCHER_PROMPT_VERSION",
    "INCIDENT_MATCHER_VERSION",
    "IncidentCandidate",
    "IncidentMatchDecision",
    "IncidentMatchError",
    "IncidentMatchResult",
    "IncidentMatcher",
    "IncidentMatcherLLMOutput",
    "MatcherAssessment",
]
