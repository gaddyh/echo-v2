"""Incident Tracker — semantic deduplication memory for parent notifications.

On each new YES assessment, the tracker compares against previously alerted
incidents using the IncidentMatcher (Layer 1). NEW → send + save. SAME →
suppress. ESCALATION → update + send if not already notified.

The tracker is thin: it does not manage incident lifecycle beyond a simple
inactivity timeout. It reuses existing delivery infrastructure (scheduling,
templates, retries) for actual notification delivery.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Protocol

from echo_v2.domain.alerted_incident import AlertedIncident
from echo_v2.domain.guard_exposure import ExposureAssessment
from echo_v2.domain.guard_simple import SafetyAssessment
from echo_v2.domain.scheduling import ScheduledActionType
from echo_v2.persistence.alerted_incident_repository import (
    AlertedIncidentRepository,
)
from echo_v2.persistence.guard_repositories import GuardianChildLinkRepository
from echo_v2.services.incident_matcher import (
    IncidentCandidate,
    IncidentMatchDecision,
    IncidentMatcher,
    MatcherAssessment,
)

_logger = logging.getLogger("echo_v2.services.incident_tracker")

__all__ = [
    "IncidentEvaluator",
    "IncidentTracker",
    "SchedulingCreator",
    "UserPhoneResolver",
]


class SchedulingCreator(Protocol):
    async def create_once(
        self,
        *,
        request_id: str,
        user_id: str,
        type: ScheduledActionType,
        execute_at_utc: datetime,
        timezone_name: str,
        payload: dict[str, object],
    ) -> tuple[object, bool]: ...


class UserPhoneResolver(Protocol):
    async def get_phone_by_id(self, user_id: str) -> str | None: ...


class IncidentEvaluator(Protocol):
    """Protocol for the worker to call the tracker."""

    async def evaluate_security(
        self, *, assessment: SafetyAssessment, assessment_id: str
    ) -> str: ...

    async def evaluate_exposure(
        self,
        *,
        assessment: ExposureAssessment,
        assessment_id: str,
        security_assessment: SafetyAssessment | None = None,
    ) -> str: ...

    async def reconcile_pending(self, *, limit: int) -> int: ...

    async def mark_evaluation_failed(self, *, assessment_id: str, error: str) -> None: ...

    async def validate_before_send(self, payload: dict[str, object]) -> bool: ...


class IncidentTracker:
    """Thin incident-aware notification decision service.

    Replaces the evidence-diff dedup in ParentAttentionDeliveryService and
    ExposureDeliveryService with semantic incident matching.
    """

    def __init__(
        self,
        *,
        matcher: IncidentMatcher,
        incidents: AlertedIncidentRepository,
        links: GuardianChildLinkRepository,
        users: UserPhoneResolver,
        scheduling: SchedulingCreator,
        security_template_name: str = "guard_security_alert_he",
        security_template_language: str = "he",
        exposure_template_name: str = "guard_exposure_alert_he",
        exposure_template_language: str = "he",
        close_after_hours: int = 24,
        recurrence_link_window_days: int = 30,
        enabled: bool = False,
    ) -> None:
        self._matcher = matcher
        self._incidents = incidents
        self._links = links
        self._users = users
        self._scheduling = scheduling
        self._security_template_name = security_template_name
        self._security_template_language = security_template_language
        self._exposure_template_name = exposure_template_name
        self._exposure_template_language = exposure_template_language
        self._close_after_hours = close_after_hours
        self._recurrence_link_window_days = recurrence_link_window_days
        self._enabled = enabled

    async def evaluate_security(
        self, *, assessment: SafetyAssessment, assessment_id: str
    ) -> str:
        if not self._enabled:
            return "disabled"
        if assessment.parent_should_know is not True:
            return "not_parent_attention"
        return await self._evaluate(
            alert_type="security",
            assessment_id=assessment_id,
            summary=assessment.summary,
            reason=assessment.reason,
            evidence_message_ids=assessment.evidence_message_ids,
            child_user_id=assessment.child_user_id,
            chat_id=assessment.chat_id,
            child_name=assessment.child_name,
        )

    async def evaluate_exposure(
        self,
        *,
        assessment: ExposureAssessment,
        assessment_id: str,
        security_assessment: SafetyAssessment | None = None,
    ) -> str:
        if not self._enabled:
            return "disabled"
        if assessment.significant_exposure is not True:
            return "not_significant_exposure"
        # Cross-alert coordination: security takes precedence over exposure
        # for overlapping evidence.
        if (
            security_assessment is not None
            and security_assessment.parent_should_know is True
            and self._evidence_overlaps(
                assessment.evidence_message_ids,
                security_assessment.evidence_message_ids,
            )
        ):
                _logger.info(
                    "Exposure assessment %s suppressed by security precedence",
                    assessment_id,
                )
                return "security_precedence"
        return await self._evaluate(
            alert_type="exposure",
            assessment_id=assessment_id,
            summary=assessment.summary,
            reason=assessment.reason,
            evidence_message_ids=assessment.evidence_message_ids,
            child_user_id=assessment.child_user_id,
            chat_id=assessment.chat_id,
            child_name=assessment.child_name or assessment.chat_name,
        )

    async def _evaluate(
        self,
        *,
        alert_type: str,
        assessment_id: str,
        summary: str,
        reason: str,
        evidence_message_ids: tuple[str, ...],
        child_user_id: str,
        chat_id: str,
        child_name: str,
    ) -> str:
        now = datetime.now(timezone.utc)

        # Idempotency: check if this assessment was already processed
        existing = await self._incidents.find_by_assessment_id(
            assessment_id, child_user_id, chat_id, alert_type
        )
        if existing is not None:
            _logger.info(
                "Assessment %s already processed as incident %s",
                assessment_id,
                existing.id,
            )
            return "already_processed"

        # Auto-close stale incidents
        await self._auto_close_stale(child_user_id, chat_id, alert_type, now)

        # Load candidates: active + recently closed
        active = await self._incidents.list_active(child_user_id, chat_id, alert_type)
        recently_closed = await self._incidents.list_recently_closed(
            child_user_id,
            chat_id,
            alert_type,
            now - timedelta(days=self._recurrence_link_window_days),
        )
        all_candidates = active + recently_closed

        # No candidates → NEW without LLM call
        if not all_candidates:
            return await self._handle_new(
                alert_type=alert_type,
                assessment_id=assessment_id,
                summary=summary,
                evidence_message_ids=evidence_message_ids,
                child_user_id=child_user_id,
                chat_id=chat_id,
                child_name=child_name,
                now=now,
                related_incident_id=None,
            )

        # Build candidates for matcher
        candidates = tuple(_to_candidate(inc) for inc in all_candidates)

        # Call matcher (outside lock)
        matcher_assessment = MatcherAssessment(
            summary=summary,
            reason=reason,
            evidence_message_ids=evidence_message_ids,
            child_user_id=child_user_id,
            chat_id=chat_id,
            created_at=now,
        )
        result = await self._matcher.match(
            alert_type=alert_type,
            assessment=matcher_assessment,
            incidents=candidates,
        )

        _logger.info(
            "Incident matcher result for %s: %s incident_id=%s reason=%s",
            assessment_id,
            result.decision.value,
            result.incident_id,
            result.reason,
        )

        if result.decision is IncidentMatchDecision.NEW_INCIDENT:
            # Acquire lock and recheck — another worker may have created an incident
            async with self._incidents.acquire_lock(child_user_id, chat_id, alert_type):
                # Reload candidates after acquiring lock
                active_after = await self._incidents.list_active(
                    child_user_id, chat_id, alert_type
                )
                closed_after = await self._incidents.list_recently_closed(
                    child_user_id,
                    chat_id,
                    alert_type,
                    now - timedelta(days=self._recurrence_link_window_days),
                )
                all_after = active_after + closed_after
                if len(all_after) > len(all_candidates):
                    # New incident(s) appeared while we waited — rerun matching
                    candidates_after = tuple(
                        _to_candidate(inc) for inc in all_after
                    )
                    result = await self._matcher.match(
                        alert_type=alert_type,
                        assessment=matcher_assessment,
                        incidents=candidates_after,
                    )
                    _logger.info(
                        "Rerun match after lock for %s: %s",
                        assessment_id,
                        result.decision.value,
                    )

            if result.decision is IncidentMatchDecision.NEW_INCIDENT:
                return await self._handle_new(
                    alert_type=alert_type,
                    assessment_id=assessment_id,
                    summary=summary,
                    evidence_message_ids=evidence_message_ids,
                    child_user_id=child_user_id,
                    chat_id=chat_id,
                    child_name=child_name,
                    now=now,
                    related_incident_id=None,
                )

        if result.decision is IncidentMatchDecision.SAME_INCIDENT:
            assert result.incident_id is not None
            return await self._handle_same(
                incident_id=result.incident_id,
                assessment_id=assessment_id,
                summary=summary,
                evidence_message_ids=evidence_message_ids,
                now=now,
            )

        if result.decision is IncidentMatchDecision.SIGNIFICANT_ESCALATION:
            assert result.incident_id is not None
            return await self._handle_escalation(
                incident_id=result.incident_id,
                assessment_id=assessment_id,
                summary=summary,
                evidence_message_ids=evidence_message_ids,
                child_user_id=child_user_id,
                chat_id=chat_id,
                child_name=child_name,
                alert_type=alert_type,
                now=now,
            )

        # Should not reach here
        return "unknown"

    async def _handle_new(
        self,
        *,
        alert_type: str,
        assessment_id: str,
        summary: str,
        evidence_message_ids: tuple[str, ...],
        child_user_id: str,
        chat_id: str,
        child_name: str,
        now: datetime,
        related_incident_id: str | None,
    ) -> str:
        incident_id = str(uuid.uuid4())
        incident = AlertedIncident(
            id=incident_id,
            child_user_id=child_user_id,
            chat_id=chat_id,
            alert_type=alert_type,
            summary=summary,
            severity_summary=summary,
            notified_severity_summary=summary,
            evidence_message_ids=evidence_message_ids,
            assessment_ids=(assessment_id,),
            last_seen_at=now,
            closed_at=None,
            related_incident_id=related_incident_id,
            notification_version=1,
            created_at=now,
            updated_at=now,
        )

        # Schedule alert for each guardian
        links = await self._links.list_active_for_child(child_user_id)
        if not links:
            _logger.info(
                "No active guardian links for child %s — incident saved without notification",
                child_user_id,
            )
            await self._incidents.create(incident)
            return "no_active_link"

        any_scheduled = False
        for link in links:
            guardian_phone = await self._users.get_phone_by_id(link.guardian_user_id)
            if not guardian_phone:
                _logger.warning(
                    "No phone for guardian %s — skipping",
                    link.guardian_user_id,
                )
                continue

            request_id = (
                f"guard-incident:{incident_id}:{link.guardian_user_id}:v1"
            )
            template_name = (
                self._security_template_name
                if alert_type == "security"
                else self._exposure_template_name
            )
            template_language = (
                self._security_template_language
                if alert_type == "security"
                else self._exposure_template_language
            )
            payload: dict[str, object] = {
                "kind": (
                    "guard_parent_attention"
                    if alert_type == "security"
                    else "guard_exposure_alert"
                ),
                "chat_id": guardian_phone,
                "guardian_user_id": link.guardian_user_id,
                "child_user_id": child_user_id,
                "incident_id": incident_id,
                "assessment_id": assessment_id,
                "evidence_message_ids": list(evidence_message_ids),
                "idempotency_key": f"guard-incident:{request_id}",
                "message": "",
                "template": {
                    "name": template_name,
                    "language": template_language,
                    "body_params": [child_name, summary],
                },
            }
            try:
                _action, _created = await self._scheduling.create_once(
                    request_id=request_id,
                    user_id=link.guardian_user_id,
                    type=ScheduledActionType.SEND_BOT_MESSAGE,
                    execute_at_utc=now,
                    timezone_name="UTC",
                    payload=payload,
                )
                any_scheduled = True
            except Exception:
                _logger.exception(
                    "Failed to schedule alert for guardian %s on incident %s",
                    link.guardian_user_id,
                    incident_id,
                )
                raise

        # Save incident (only after scheduling succeeds)
        await self._incidents.create(incident)
        return "scheduled" if any_scheduled else "no_guardian_phone"

    async def _handle_same(
        self,
        *,
        incident_id: str,
        assessment_id: str,
        summary: str,
        evidence_message_ids: tuple[str, ...],
        now: datetime,
    ) -> str:
        incident = await self._incidents.get(incident_id)
        if incident is None:
            _logger.warning(
                "Matcher returned SAME for unknown incident %s — treating as suppressed",
                incident_id,
            )
            return "suppressed"

        updated = replace(
            incident,
            summary=summary,
            last_seen_at=now,
            evidence_message_ids=tuple(
                set(incident.evidence_message_ids) | set(evidence_message_ids)
            ),
            assessment_ids=tuple(
                set(incident.assessment_ids) | {assessment_id}
            ),
            updated_at=now,
        )
        await self._incidents.update(updated)
        return "suppressed"

    async def _handle_escalation(
        self,
        *,
        incident_id: str,
        assessment_id: str,
        summary: str,
        evidence_message_ids: tuple[str, ...],
        child_user_id: str,
        chat_id: str,
        child_name: str,
        alert_type: str,
        now: datetime,
    ) -> str:
        incident = await self._incidents.get(incident_id)
        if incident is None:
            _logger.warning(
                "Matcher returned ESCALATION for unknown incident %s — treating as suppressed",
                incident_id,
            )
            return "suppressed"

        # The matcher already determined this is materially more severe.
        # Update severity_summary and schedule escalation alert.
        new_version = incident.notification_version + 1
        updated = replace(
            incident,
            summary=summary,
            severity_summary=summary,
            notified_severity_summary=summary,
            last_seen_at=now,
            evidence_message_ids=tuple(
                set(incident.evidence_message_ids) | set(evidence_message_ids)
            ),
            assessment_ids=tuple(
                set(incident.assessment_ids) | {assessment_id}
            ),
            notification_version=new_version,
            updated_at=now,
        )
        await self._incidents.update(updated)

        # Schedule escalation alert for each guardian
        links = await self._links.list_active_for_child(child_user_id)
        any_scheduled = False
        for link in links:
            guardian_phone = await self._users.get_phone_by_id(link.guardian_user_id)
            if not guardian_phone:
                continue

            request_id = (
                f"guard-incident:{incident_id}:{link.guardian_user_id}:v{new_version}"
            )
            template_name = (
                self._security_template_name
                if alert_type == "security"
                else self._exposure_template_name
            )
            template_language = (
                self._security_template_language
                if alert_type == "security"
                else self._exposure_template_language
            )
            payload: dict[str, object] = {
                "kind": (
                    "guard_parent_attention"
                    if alert_type == "security"
                    else "guard_exposure_alert"
                ),
                "chat_id": guardian_phone,
                "guardian_user_id": link.guardian_user_id,
                "child_user_id": child_user_id,
                "incident_id": incident_id,
                "assessment_id": assessment_id,
                "evidence_message_ids": list(evidence_message_ids),
                "idempotency_key": f"guard-incident:{request_id}",
                "message": "",
                "template": {
                    "name": template_name,
                    "language": template_language,
                    "body_params": [child_name, summary],
                },
            }
            try:
                _action, _created = await self._scheduling.create_once(
                    request_id=request_id,
                    user_id=link.guardian_user_id,
                    type=ScheduledActionType.SEND_BOT_MESSAGE,
                    execute_at_utc=now,
                    timezone_name="UTC",
                    payload=payload,
                )
                any_scheduled = True
            except Exception:
                _logger.exception(
                    "Failed to schedule escalation for guardian %s on incident %s",
                    link.guardian_user_id,
                    incident_id,
                )
                raise

        return "escalation_scheduled" if any_scheduled else "escalation_no_guardian_phone"

    async def _auto_close_stale(
        self,
        child_user_id: str,
        chat_id: str,
        alert_type: str,
        now: datetime,
    ) -> None:
        active = await self._incidents.list_active(child_user_id, chat_id, alert_type)
        threshold = now - timedelta(hours=self._close_after_hours)
        for inc in active:
            if inc.last_seen_at < threshold:
                _logger.info(
                    "Auto-closing stale incident %s (last_seen_at=%s)",
                    inc.id,
                    inc.last_seen_at.isoformat(),
                )
                await self._incidents.close(inc.id, now)

    async def reconcile_pending(self, *, limit: int) -> int:
        # Delegate to existing scheduler — the tracker doesn't own delivery state.
        # The scheduled_actions table + scheduler handle retries.
        return 0

    async def mark_evaluation_failed(self, *, assessment_id: str, error: str) -> None:
        _logger.error(
            "Incident tracker evaluation failed for assessment %s: %s",
            assessment_id,
            error,
        )

    async def validate_before_send(self, payload: dict[str, object]) -> bool:
        guardian_user_id = payload.get("guardian_user_id")
        child_user_id = payload.get("child_user_id")
        if not isinstance(guardian_user_id, str) or not isinstance(child_user_id, str):
            return False
        link = await self._links.get_active_for_pair(
            guardian_user_id=guardian_user_id,
            child_user_id=child_user_id,
        )
        return link is not None

    @staticmethod
    def _evidence_overlaps(
        a: tuple[str, ...], b: tuple[str, ...]
    ) -> bool:
        return bool(set(a) & set(b))


def _to_candidate(incident: AlertedIncident) -> IncidentCandidate:
    return IncidentCandidate(
        id=incident.id,
        alert_type=incident.alert_type,
        issue_summary=incident.summary,
        severity_summary=incident.severity_summary,
        first_seen_at=incident.created_at,
        last_seen_at=incident.last_seen_at,
        evidence_message_ids=incident.evidence_message_ids,
    )
