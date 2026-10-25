"""Incident Tracker — semantic deduplication memory for parent notifications.

On each new YES assessment, the tracker compares against previously alerted
incidents using the IncidentMatcher (Layer 1). NEW → send + save. SAME →
suppress. ESCALATION → update + send if not already notified.

The tracker is thin: it does not manage incident lifecycle beyond a simple
inactivity timeout. It reuses existing delivery infrastructure (scheduling,
templates, retries) for actual notification delivery.

Concurrency & atomicity
-----------------------
When a ``unit_of_work_factory`` is provided (production), the tracker uses the
existing :class:`PostgresUnitOfWork` to persist the incident and its scheduled
actions in a single transaction. A PostgreSQL advisory lock
(``pg_advisory_xact_lock``) is acquired on the UoW session before the decision
and held until the transaction commits, preventing concurrent workers from
creating duplicate incidents. The LLM matcher is called *before* acquiring the
lock (to avoid blocking other workers during the LLM round-trip), then
candidates are reloaded and the matcher is re-run against the current incident
state *under* the lock if the state changed.

For the pilot, if scheduling fails the entire transaction rolls back and the
error is logged. There is no reconciliation or retry queue — the alert may be
missed if no new assessment arrives. This is an explicitly accepted limitation.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from echo_v2.domain.alerted_incident import AlertedIncident
from echo_v2.domain.guard_exposure import ExposureAssessment
from echo_v2.domain.guard_simple import SafetyAssessment
from echo_v2.domain.scheduling import (
    ScheduledAction,
    ScheduledActionStatus,
    ScheduledActionType,
)
from echo_v2.persistence.alerted_incident_repository import (
    AlertedIncidentRepository,
)
from echo_v2.persistence.guard_repositories import GuardianChildLinkRepository
from echo_v2.persistence.scheduled_actions import ScheduledActionRepository
from echo_v2.services.incident_matcher import (
    IncidentCandidate,
    IncidentMatchDecision,
    IncidentMatcher,
    IncidentMatchResult,
    MatcherAssessment,
)

_logger = logging.getLogger("echo_v2.services.incident_tracker")

__all__ = [
    "IncidentEvaluator",
    "IncidentTracker",
    "UserPhoneResolver",
]


# Type alias for the UoW factory (avoids importing PostgresUnitOfWork here
# to keep the module importable in unit-test contexts without Postgres).
UnitOfWorkFactory = Callable[[], AbstractAsyncContextManager[Any]]


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
        scheduling: ScheduledActionRepository,
        unit_of_work_factory: UnitOfWorkFactory | None = None,
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
        self._uow_factory = unit_of_work_factory
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
            evidence_timestamp=assessment.last_evidence_at,
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
            evidence_timestamp=assessment.window_end,
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
        evidence_timestamp: datetime | None,
    ) -> str:
        now = datetime.now(timezone.utc)
        last_seen = evidence_timestamp or now

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

        matcher_assessment = MatcherAssessment(
            summary=summary,
            reason=reason,
            evidence_message_ids=evidence_message_ids,
            child_user_id=child_user_id,
            chat_id=chat_id,
            created_at=now,
        )

        # No candidates → NEW without LLM call
        if not all_candidates:
            result = IncidentMatchResult(
                decision=IncidentMatchDecision.NEW_INCIDENT,
                incident_id=None,
                reason="No existing incidents to match against.",
            )
        else:
            # Phase 1: call matcher (outside lock) to avoid blocking other
            # workers during the LLM round-trip.
            candidates = tuple(_to_candidate(inc) for inc in all_candidates)
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

        # Phase 2: acquire lock + (re-run matcher if state changed) + write.
        # When a UoW factory is available, the lock and the write share one
        # transaction. Otherwise (unit tests) the standalone repos are used.
        if self._uow_factory is not None:
            return await self._decide_and_write_uow(
                uow_factory=self._uow_factory,
                alert_type=alert_type,
                assessment_id=assessment_id,
                summary=summary,
                reason=reason,
                evidence_message_ids=evidence_message_ids,
                child_user_id=child_user_id,
                chat_id=chat_id,
                child_name=child_name,
                now=now,
                last_seen=last_seen,
                matcher_assessment=matcher_assessment,
                all_candidates=all_candidates,
                result=result,
            )
        return await self._decide_and_write_standalone(
            alert_type=alert_type,
            assessment_id=assessment_id,
            summary=summary,
            reason=reason,
            evidence_message_ids=evidence_message_ids,
            child_user_id=child_user_id,
            chat_id=chat_id,
            child_name=child_name,
            now=now,
            last_seen=last_seen,
            matcher_assessment=matcher_assessment,
            all_candidates=all_candidates,
            result=result,
        )

    # ------------------------------------------------------------------
    # UoW path (production + integration tests)
    # ------------------------------------------------------------------

    async def _decide_and_write_uow(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        alert_type: str,
        assessment_id: str,
        summary: str,
        reason: str,
        evidence_message_ids: tuple[str, ...],
        child_user_id: str,
        chat_id: str,
        child_name: str,
        now: datetime,
        last_seen: datetime,
        matcher_assessment: MatcherAssessment,
        all_candidates: list[AlertedIncident],
        result: IncidentMatchResult,
    ) -> str:
        async with uow_factory() as uow:
            incidents_repo = uow.alerted_incidents
            scheduling_repo = uow.scheduled_actions
            async with incidents_repo.acquire_lock(child_user_id, chat_id, alert_type):
                # Reload candidates under the lock — current incident state.
                active_after = await incidents_repo.list_active(
                    child_user_id, chat_id, alert_type
                )
                closed_after = await incidents_repo.list_recently_closed(
                    child_user_id,
                    chat_id,
                    alert_type,
                    now - timedelta(days=self._recurrence_link_window_days),
                )
                all_after = active_after + closed_after

                # Re-run the matcher if the incident state changed while we
                # waited for the lock — compare against current state, not
                # just candidate count.
                if _candidates_differ(all_candidates, all_after):
                    candidates_after = tuple(
                        _to_candidate(inc) for inc in all_after
                    )
                    result = await self._matcher.match(
                        alert_type=alert_type,
                        assessment=matcher_assessment,
                        incidents=candidates_after,
                    )
                    _logger.info(
                        "Rerun match under lock for %s: %s",
                        assessment_id,
                        result.decision.value,
                    )

                return await self._dispatch_decision(
                    result=result,
                    incidents_repo=incidents_repo,
                    scheduling_repo=scheduling_repo,
                    alert_type=alert_type,
                    assessment_id=assessment_id,
                    summary=summary,
                    evidence_message_ids=evidence_message_ids,
                    child_user_id=child_user_id,
                    chat_id=chat_id,
                    child_name=child_name,
                    now=now,
                    last_seen=last_seen,
                )
            # Lock context exits (no commit — UoW owns the transaction)
        # UoW commits → incident + schedules + lock release all atomic

    # ------------------------------------------------------------------
    # Standalone path (unit tests — in-memory repos)
    # ------------------------------------------------------------------

    async def _decide_and_write_standalone(
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
        now: datetime,
        last_seen: datetime,
        matcher_assessment: MatcherAssessment,
        all_candidates: list[AlertedIncident],
        result: IncidentMatchResult,
    ) -> str:
        async with self._incidents.acquire_lock(child_user_id, chat_id, alert_type):
            # Reload candidates under the lock.
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

            if _candidates_differ(all_candidates, all_after):
                candidates_after = tuple(_to_candidate(inc) for inc in all_after)
                result = await self._matcher.match(
                    alert_type=alert_type,
                    assessment=matcher_assessment,
                    incidents=candidates_after,
                )
                _logger.info(
                    "Rerun match under lock for %s: %s",
                    assessment_id,
                    result.decision.value,
                )

            return await self._dispatch_decision(
                result=result,
                incidents_repo=self._incidents,
                scheduling_repo=self._scheduling,
                alert_type=alert_type,
                assessment_id=assessment_id,
                summary=summary,
                evidence_message_ids=evidence_message_ids,
                child_user_id=child_user_id,
                chat_id=chat_id,
                child_name=child_name,
                now=now,
                last_seen=last_seen,
            )

    # ------------------------------------------------------------------
    # Decision dispatch (shared by both paths)
    # ------------------------------------------------------------------

    async def _dispatch_decision(
        self,
        *,
        result: IncidentMatchResult,
        incidents_repo: AlertedIncidentRepository,
        scheduling_repo: ScheduledActionRepository,
        alert_type: str,
        assessment_id: str,
        summary: str,
        evidence_message_ids: tuple[str, ...],
        child_user_id: str,
        chat_id: str,
        child_name: str,
        now: datetime,
        last_seen: datetime,
    ) -> str:
        if result.decision is IncidentMatchDecision.NEW_INCIDENT:
            return await self._handle_new(
                incidents_repo=incidents_repo,
                scheduling_repo=scheduling_repo,
                alert_type=alert_type,
                assessment_id=assessment_id,
                summary=summary,
                evidence_message_ids=evidence_message_ids,
                child_user_id=child_user_id,
                chat_id=chat_id,
                child_name=child_name,
                last_seen=last_seen,
                related_incident_id=None,
            )

        if result.decision is IncidentMatchDecision.SAME_INCIDENT:
            assert result.incident_id is not None
            return await self._handle_same(
                incidents_repo=incidents_repo,
                scheduling_repo=scheduling_repo,
                incident_id=result.incident_id,
                assessment_id=assessment_id,
                summary=summary,
                evidence_message_ids=evidence_message_ids,
                child_user_id=child_user_id,
                chat_id=chat_id,
                child_name=child_name,
                alert_type=alert_type,
                now=now,
                last_seen=last_seen,
            )

        if result.decision is IncidentMatchDecision.SIGNIFICANT_ESCALATION:
            assert result.incident_id is not None
            return await self._handle_escalation(
                incidents_repo=incidents_repo,
                scheduling_repo=scheduling_repo,
                incident_id=result.incident_id,
                assessment_id=assessment_id,
                summary=summary,
                evidence_message_ids=evidence_message_ids,
                child_user_id=child_user_id,
                chat_id=chat_id,
                child_name=child_name,
                alert_type=alert_type,
                now=now,
                last_seen=last_seen,
            )

        # Should not reach here
        return "unknown"  # pragma: no cover

    # ------------------------------------------------------------------
    # NEW incident
    # ------------------------------------------------------------------

    async def _handle_new(
        self,
        *,
        incidents_repo: AlertedIncidentRepository,
        scheduling_repo: ScheduledActionRepository,
        alert_type: str,
        assessment_id: str,
        summary: str,
        evidence_message_ids: tuple[str, ...],
        child_user_id: str,
        chat_id: str,
        child_name: str,
        last_seen: datetime,
        related_incident_id: str | None,
    ) -> str:
        # Resolve guardians + phones BEFORE creating the incident.
        links = await self._links.list_active_for_child(child_user_id)
        eligible = await self._resolve_eligible_guardians(links)

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
            last_seen_at=last_seen,
            closed_at=None,
            related_incident_id=related_incident_id,
            notification_version=1,
            created_at=last_seen,
            updated_at=last_seen,
        )

        if not eligible:
            # No eligible guardian — still save the incident for future
            # deduplication, but skip scheduled alert delivery.
            _logger.info(
                "No eligible guardian for child %s — incident saved without alert",
                child_user_id,
            )
            await incidents_repo.create(incident)
            return "no_active_link" if not links else "no_guardian_phone"

        # Build scheduled actions for each eligible guardian.
        actions = [
            self._build_scheduled_action(
                request_id=f"guard-incident:{incident_id}:{guardian_id}:v1",
                user_id=guardian_id,
                execute_at=last_seen,
                payload=self._build_payload(
                    alert_type=alert_type,
                    guardian_phone=phone,
                    guardian_user_id=guardian_id,
                    child_user_id=child_user_id,
                    incident_id=incident_id,
                    assessment_id=assessment_id,
                    evidence_message_ids=evidence_message_ids,
                    child_name=child_name,
                    summary=summary,
                    request_id=f"guard-incident:{incident_id}:{guardian_id}:v1",
                ),
            )
            for guardian_id, phone in eligible
        ]

        # Persist incident + schedules atomically (UoW owns the transaction
        # boundary in production; in-memory repos are trivially atomic).
        await incidents_repo.create(incident)
        for action in actions:
            await scheduling_repo.create_once(action)
        return "recurrence_scheduled" if related_incident_id is not None else "scheduled"

    # ------------------------------------------------------------------
    # SAME incident
    # ------------------------------------------------------------------

    async def _handle_same(
        self,
        *,
        incidents_repo: AlertedIncidentRepository,
        scheduling_repo: ScheduledActionRepository,
        incident_id: str,
        assessment_id: str,
        summary: str,
        evidence_message_ids: tuple[str, ...],
        child_user_id: str,
        chat_id: str,
        child_name: str,
        alert_type: str,
        now: datetime,
        last_seen: datetime,
    ) -> str:
        incident = await incidents_repo.get(incident_id)
        if incident is None:
            _logger.warning(
                "Matcher returned SAME for unknown incident %s — treating as suppressed",
                incident_id,
            )
            return "suppressed"

        # Closed incident + SAME → recurrence: create a new linked incident.
        if incident.closed_at is not None:
            _logger.info(
                "Incident %s is closed — creating recurrence",
                incident_id,
            )
            return await self._handle_new(
                incidents_repo=incidents_repo,
                scheduling_repo=scheduling_repo,
                alert_type=alert_type,
                assessment_id=assessment_id,
                summary=summary,
                evidence_message_ids=evidence_message_ids,
                child_user_id=child_user_id,
                chat_id=chat_id,
                child_name=child_name,
                last_seen=last_seen,
                related_incident_id=incident_id,
            )

        # Active incident → suppress and update context.
        updated = replace(
            incident,
            summary=summary,
            last_seen_at=last_seen,
            evidence_message_ids=tuple(
                set(incident.evidence_message_ids) | set(evidence_message_ids)
            ),
            assessment_ids=tuple(
                set(incident.assessment_ids) | {assessment_id}
            ),
            updated_at=now,
        )
        await incidents_repo.update(updated)
        return "suppressed"

    # ------------------------------------------------------------------
    # Escalation
    # ------------------------------------------------------------------

    async def _handle_escalation(
        self,
        *,
        incidents_repo: AlertedIncidentRepository,
        scheduling_repo: ScheduledActionRepository,
        incident_id: str,
        assessment_id: str,
        summary: str,
        evidence_message_ids: tuple[str, ...],
        child_user_id: str,
        chat_id: str,
        child_name: str,
        alert_type: str,
        now: datetime,
        last_seen: datetime,
    ) -> str:
        incident = await incidents_repo.get(incident_id)
        if incident is None:
            _logger.warning(
                "Matcher returned ESCALATION for unknown incident %s — treating as suppressed",
                incident_id,
            )
            return "suppressed"

        # Resolve guardians + phones BEFORE updating the incident.
        links = await self._links.list_active_for_child(child_user_id)
        eligible = await self._resolve_eligible_guardians(links)
        if not eligible:
            _logger.info(
                "No eligible guardian for escalation on incident %s — not updated",
                incident_id,
            )
            return "escalation_no_guardian_phone" if links else "escalation_no_active_link"

        # The matcher already determined this is materially more severe.
        new_version = incident.notification_version + 1
        updated = replace(
            incident,
            summary=summary,
            severity_summary=summary,
            notified_severity_summary=summary,
            last_seen_at=last_seen,
            evidence_message_ids=tuple(
                set(incident.evidence_message_ids) | set(evidence_message_ids)
            ),
            assessment_ids=tuple(
                set(incident.assessment_ids) | {assessment_id}
            ),
            notification_version=new_version,
            updated_at=now,
        )

        # Build scheduled actions for version N+1.
        actions = [
            self._build_scheduled_action(
                request_id=f"guard-incident:{incident_id}:{guardian_id}:v{new_version}",
                user_id=guardian_id,
                execute_at=last_seen,
                payload=self._build_payload(
                    alert_type=alert_type,
                    guardian_phone=phone,
                    guardian_user_id=guardian_id,
                    child_user_id=child_user_id,
                    incident_id=incident_id,
                    assessment_id=assessment_id,
                    evidence_message_ids=evidence_message_ids,
                    child_name=child_name,
                    summary=summary,
                    request_id=f"guard-incident:{incident_id}:{guardian_id}:v{new_version}",
                ),
            )
            for guardian_id, phone in eligible
        ]

        # Persist update + schedules atomically. If scheduling fails, the
        # UoW rolls back — version is NOT bumped.
        await incidents_repo.update(updated)
        for action in actions:
            await scheduling_repo.create_once(action)
        return "escalation_scheduled"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _resolve_eligible_guardians(
        self, links: list[Any]
    ) -> list[tuple[str, str]]:
        """Return [(guardian_user_id, phone), ...] for guardians with a phone."""
        eligible: list[tuple[str, str]] = []
        for link in links:
            phone = await self._users.get_phone_by_id(link.guardian_user_id)
            if phone:
                eligible.append((link.guardian_user_id, phone))
        return eligible

    def _build_scheduled_action(
        self,
        *,
        request_id: str,
        user_id: str,
        execute_at: datetime,
        payload: dict[str, object],
    ) -> ScheduledAction:
        """Build a ScheduledAction with a deterministic id (uuid5)."""
        action_id = str(uuid5(NAMESPACE_URL, f"echo:send:{user_id}:{request_id}"))
        return ScheduledAction(
            id=action_id,
            user_id=user_id,
            type=ScheduledActionType.SEND_BOT_MESSAGE,
            execute_at_utc=execute_at,
            timezone="UTC",
            status=ScheduledActionStatus.PENDING,
            payload=payload,
        )

    def _build_payload(
        self,
        *,
        alert_type: str,
        guardian_phone: str,
        guardian_user_id: str,
        child_user_id: str,
        incident_id: str,
        assessment_id: str,
        evidence_message_ids: tuple[str, ...],
        child_name: str,
        summary: str,
        request_id: str,
    ) -> dict[str, object]:
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
        return {
            "kind": (
                "guard_parent_attention"
                if alert_type == "security"
                else "guard_exposure_alert"
            ),
            "chat_id": guardian_phone,
            "guardian_user_id": guardian_user_id,
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
        # No reconciliation for the pilot. The scheduled_actions table +
        # scheduler handle delivery retries. If tracking fails (UoW rollback),
        # the alert may be missed — accepted pilot limitation.
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


def _candidates_differ(
    before: list[AlertedIncident], after: list[AlertedIncident]
) -> bool:
    """Check whether the incident candidate set changed.

    Compares by incident ID set (order-independent). A new incident appearing
    while we waited for the lock changes the set — the matcher must be re-run
    against the current state.
    """
    before_ids = {inc.id for inc in before}
    after_ids = {inc.id for inc in after}
    return before_ids != after_ids


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
