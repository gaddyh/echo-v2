"""API-backed evaluation for the Incident Matcher (Layer 1).

Replays gold cases sequentially, maintaining an in-memory list of incident
candidates that accumulates evidence and severity after every match.
Requires ``OPENAI_API_KEY`` and calls the real LLM.
"""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

from echo_v2.services.incident_matcher import (
    IncidentCandidate,
    IncidentMatchDecision,
    IncidentMatcher,
    MatcherAssessment,
)
from tests.evaluation.guard.incident_matcher_cases import (
    CASES,
    validate_cases,
)

pytestmark = [
    pytest.mark.eval_guard,
    pytest.mark.skipif(
        not os.environ.get("OPENAI_API_KEY"),
        reason="OPENAI_API_KEY not set — eval requires real API access",
    ),
]


@pytest.fixture
def matcher() -> IncidentMatcher:
    from openai import AsyncOpenAI

    return IncidentMatcher(
        client=AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", "")),
        model=os.environ.get("INCIDENT_MATCHER_LLM_MODEL", os.environ.get("GUARD_LLM_MODEL_NAME", "gpt-4.1")),
    )


async def test_incident_matcher_gold_cases(matcher: IncidentMatcher) -> None:
    validate_cases()
    failures: list[str] = []
    for case in CASES:
        incidents: list[IncidentCandidate] = []
        for spec in case.assessments:
            assessment = MatcherAssessment(
                summary=spec.summary,
                reason=spec.reason,
                evidence_message_ids=spec.evidence_message_ids,
                child_user_id="gold-child",
                chat_id="gold-chat",
                created_at=spec.created_at,
            )
            result = await matcher.match(
                alert_type=spec.alert_type or case.alert_type,
                assessment=assessment,
                incidents=tuple(incidents),
            )
            print(
                f"{case.case_id}@{spec.assessment_id}: "
                f"{result.decision.value} / incident_id={result.incident_id} / {result.reason}"
            )
            if result.decision != spec.expected_decision:
                failures.append(
                    f"{case.case_id}@{spec.assessment_id}: "
                    f"got {result.decision.value}, expected {spec.expected_decision.value}"
                )
            # SAME/ESCALATION must return a valid incident_id; NEW must return None
            if result.decision is IncidentMatchDecision.NEW_INCIDENT and result.incident_id is not None:
                failures.append(
                    f"{case.case_id}@{spec.assessment_id}: "
                    f"NEW_INCIDENT returned incident_id={result.incident_id}, expected None"
                )
            if (
                result.decision in (IncidentMatchDecision.SAME_INCIDENT, IncidentMatchDecision.SIGNIFICANT_ESCALATION)
                and not result.incident_id
            ):
                failures.append(
                    f"{case.case_id}@{spec.assessment_id}: "
                    f"{result.decision.value} returned incident_id=None, expected a valid id"
                )
            if spec.expected_incident_id and result.incident_id != spec.expected_incident_id:
                failures.append(
                    f"{case.case_id}@{spec.assessment_id}: "
                    f"got incident_id={result.incident_id}, expected {spec.expected_incident_id}"
                )
            # Update the in-memory candidate after EVERY matching assessment
            # so the next assessment is compared against accumulated incident
            # history, not just the first assessment.
            if result.decision is IncidentMatchDecision.NEW_INCIDENT:
                incidents.append(
                    IncidentCandidate(
                        id=spec.assessment_id,
                        alert_type=spec.alert_type or case.alert_type,
                        issue_summary=spec.summary,
                        severity_summary=spec.summary,
                        first_seen_at=spec.created_at,
                        last_seen_at=spec.created_at,
                        evidence_message_ids=spec.evidence_message_ids,
                    )
                )
            elif result.decision in (
                IncidentMatchDecision.SAME_INCIDENT,
                IncidentMatchDecision.SIGNIFICANT_ESCALATION,
            ):
                idx = next(
                    (i for i, inc in enumerate(incidents) if inc.id == result.incident_id),
                    None,
                )
                if idx is not None:
                    existing = incidents[idx]
                    new_evidence = tuple(
                        set(existing.evidence_message_ids) | set(spec.evidence_message_ids)
                    )
                    # On escalation, update severity_summary to the new summary
                    new_severity = (
                        spec.summary
                        if result.decision is IncidentMatchDecision.SIGNIFICANT_ESCALATION
                        else existing.severity_summary
                    )
                    incidents[idx] = replace(
                        existing,
                        issue_summary=spec.summary,
                        severity_summary=new_severity,
                        last_seen_at=spec.created_at,
                        evidence_message_ids=new_evidence,
                    )
    if failures:
        pytest.fail("\n".join(failures))
