"""Opt-in Exposure notification delivery for Baseline B."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from echo_v2.domain.guard_exposure import ExposureAssessment
from echo_v2.domain.guard_simple import SafetyAssessment
from echo_v2.domain.parent_attention import ParentAttentionOutcomeStatus
from echo_v2.domain.scheduling import (
    ScheduledAction,
    ScheduledActionStatus,
    ScheduledActionType,
)
from echo_v2.persistence.guard_exposure_delivery import (
    ExposureDeliveryOutcome,
    ExposureDeliveryRepository,
    ExposureDeliveryState,
)
from echo_v2.persistence.guard_repositories import GuardianChildLinkRepository
from echo_v2.persistence.guard_simple_repositories import (
    GuardSimpleAssessmentRepository,
)
from echo_v2.persistence.scheduled_actions import ScheduledActionRepository
from echo_v2.persistence.user_repository import PostgresUserRepository


class SchedulingCreator(Protocol):
    async def create_once(
        self, *, request_id: str, user_id: str, type: ScheduledActionType,
        execute_at_utc: datetime, timezone_name: str, payload: dict[str, object],
    ) -> tuple[ScheduledAction, bool]: ...

    async def cancel(self, action_id: str, user_id: str) -> bool: ...


class ExposureDeliveryService:
    def __init__(
        self,
        *,
        links: GuardianChildLinkRepository,
        users: PostgresUserRepository,
        security_assessments: GuardSimpleAssessmentRepository,
        delivery: ExposureDeliveryRepository,
        actions: ScheduledActionRepository,
        scheduling: SchedulingCreator,
        template_name: str = "guard_exposure_alert_he",
        template_language: str = "he",
        enabled: bool = False,
    ) -> None:
        self._links = links
        self._users = users
        self._security_assessments = security_assessments
        self._delivery = delivery
        self._actions = actions
        self._scheduling = scheduling
        self._template_name = template_name
        self._template_language = template_language
        self._enabled = enabled

    async def evaluate_committed(
        self,
        *,
        assessment: ExposureAssessment,
        assessment_id: str,
        security_assessment: SafetyAssessment | None = None,
    ) -> str:
        now = datetime.now(timezone.utc)
        outcome = await self._ensure_outcome(assessment, assessment_id, now)
        if not self._enabled:
            await self._save(outcome, status=ParentAttentionOutcomeStatus.NOT_SENT, reason="disabled")
            return "disabled"
        if assessment.significant_exposure is not True:
            await self._save(outcome, status=ParentAttentionOutcomeStatus.NOT_SENT, reason="not_exposure")
            return "not_exposure"

        security_rows = await self._security_assessments.list_for_chat(
            child_user_id=assessment.child_user_id, chat_id=assessment.chat_id, limit=100
        )
        if security_assessment is not None:
            security_rows.append(security_assessment)
        if any(
            row.parent_should_know is True
            and _overlaps(assessment.evidence_message_ids, row.evidence_message_ids)
            for row in security_rows
        ):
            await self._save(outcome, status=ParentAttentionOutcomeStatus.NOT_SENT, reason="security_precedence")
            return "security_precedence"

        duplicates = await self._delivery.list_overlapping(
            child_user_id=assessment.child_user_id,
            chat_id=assessment.chat_id,
            evidence_message_ids=assessment.evidence_message_ids,
        )
        if any(item.status in {
            ParentAttentionOutcomeStatus.SCHEDULED,
            ParentAttentionOutcomeStatus.SENT,
        } for item in duplicates if item.assessment_id != assessment_id):
            await self._save(outcome, status=ParentAttentionOutcomeStatus.NOT_SENT, reason="already_notified")
            return "duplicate_evidence"

        link = await self._links.get_active_for_child(assessment.child_user_id)
        if link is None:
            await self._save(outcome, status=ParentAttentionOutcomeStatus.NOT_SENT, reason="no_active_link")
            return "no_active_link"
        guardian_phone = await self._users.get_phone_by_id(link.guardian_user_id)
        if guardian_phone is None:
            await self._save(outcome, status=ParentAttentionOutcomeStatus.NOT_SENT, reason="no_guardian_phone")
            return "no_guardian_phone"

        request_id = f"guard-exposure:{assessment.child_user_id}:{assessment.chat_id}:{assessment_id}"
        action_id = str(uuid5(NAMESPACE_URL, f"echo:send:{link.guardian_user_id}:{request_id}"))
        reserved = await self._delivery.reserve_state(
            ExposureDeliveryState(
                child_user_id=assessment.child_user_id,
                chat_id=assessment.chat_id,
                last_scheduled_assessment_id=assessment_id,
                last_scheduled_evidence_ids=tuple(assessment.evidence_message_ids),
                last_action_id=action_id,
                updated_at=now,
            )
        )
        if not reserved:
            await self._save(
                outcome,
                status=ParentAttentionOutcomeStatus.NOT_SENT,
                reason="already_notified",
            )
            return "duplicate_evidence"
        action, _created = await self._scheduling.create_once(
            request_id=request_id,
            user_id=link.guardian_user_id,
            type=ScheduledActionType.SEND_BOT_MESSAGE,
            execute_at_utc=now,
            timezone_name="UTC",
            payload={
                "kind": "guard_exposure_alert",
                "chat_id": guardian_phone,
                "guardian_user_id": link.guardian_user_id,
                "child_user_id": assessment.child_user_id,
                "exposure_assessment_id": assessment_id,
                "evidence_message_ids": list(assessment.evidence_message_ids),
                "expires_at": (now + timedelta(hours=2)).isoformat(),
                "idempotency_key": f"guard-exposure:{request_id}",
                "message": "",
                "template": {
                    "name": self._template_name,
                    "language": self._template_language,
                    "body_params": [assessment.chat_name or assessment.chat_id, assessment.summary],
                },
            },
        )
        await self._save(
            outcome,
            status=ParentAttentionOutcomeStatus.SCHEDULED,
            reason="scheduled",
            scheduled_action_id=action.id,
            scheduled_at=now,
        )
        return "scheduled"

    async def cancel_overlapping_security(self, assessment: SafetyAssessment) -> int:
        if not self._enabled or assessment.parent_should_know is not True:
            return 0
        pending = await self._delivery.list_overlapping(
            child_user_id=assessment.child_user_id,
            chat_id=assessment.chat_id,
            evidence_message_ids=assessment.evidence_message_ids,
            pending_only=True,
        )
        cancelled = 0
        for outcome in pending:
            if outcome.scheduled_action_id is None:
                continue
            action = await self._actions.get(outcome.scheduled_action_id)
            if action is None:
                continue
            if action.status is ScheduledActionStatus.IN_PROGRESS:
                await self._save(
                    outcome,
                    status=ParentAttentionOutcomeStatus.NOT_SENT,
                    reason="security_precedence",
                    scheduled_action_id=outcome.scheduled_action_id,
                )
                cancelled += 1
                continue
            if action.status is not ScheduledActionStatus.PENDING:
                continue
            if await self._scheduling.cancel(action.id, action.user_id):
                await self._save(
                    outcome,
                    status=ParentAttentionOutcomeStatus.NOT_SENT,
                    reason="security_precedence",
                    scheduled_action_id=outcome.scheduled_action_id,
                )
                cancelled += 1
        return cancelled

    async def validate_before_send(self, payload: dict[str, object]) -> bool:
        if not self._enabled:
            return False
        assessment_id = str(payload.get("exposure_assessment_id", ""))
        if not assessment_id:
            return False
        outcome = await self._delivery.get_outcome(assessment_id)
        return outcome is not None and outcome.status is ParentAttentionOutcomeStatus.SCHEDULED

    async def mark_evaluation_failed(self, *, assessment_id: str, error: str) -> None:
        outcome = await self._delivery.get_outcome(assessment_id)
        if outcome is not None:
            await self._save(
                outcome,
                status=ParentAttentionOutcomeStatus.FAILED,
                reason=error[:500],
                failed_at=datetime.now(timezone.utc),
            )

    async def reconcile_pending(self, *, limit: int) -> int:
        touched = 0
        for outcome in await self._delivery.list_reconcilable(limit=limit):
            if outcome.scheduled_action_id is None:
                await self._save(
                    outcome,
                    status=ParentAttentionOutcomeStatus.INDETERMINATE,
                    reason="scheduled_action_missing",
                )
                touched += 1
                continue
            action = await self._actions.get(outcome.scheduled_action_id)
            if action is None or action.status in {
                ScheduledActionStatus.PENDING,
                ScheduledActionStatus.IN_PROGRESS,
            }:
                continue
            if action.status is ScheduledActionStatus.SUCCEEDED:
                provider_message_id = _provider_message_id(action)
                if provider_message_id is None:
                    await self._save(
                        outcome,
                        status=ParentAttentionOutcomeStatus.INDETERMINATE,
                        reason="provider_result_missing",
                    )
                else:
                    await self._save(
                        outcome,
                        status=ParentAttentionOutcomeStatus.SENT,
                        reason="provider_confirmed",
                        provider_message_id=provider_message_id,
                        sent_at=action.executed_at or datetime.now(timezone.utc),
                    )
            elif action.status is ScheduledActionStatus.CANCELLED:
                await self._save(
                    outcome,
                    status=ParentAttentionOutcomeStatus.NOT_SENT,
                    reason=action.error or "suppressed",
                )
            elif action.status is ScheduledActionStatus.FAILED:
                await self._save(
                    outcome,
                    status=ParentAttentionOutcomeStatus.FAILED,
                    reason=action.error or "scheduled_action_failed",
                    failed_at=action.executed_at or datetime.now(timezone.utc),
                )
            elif action.status is ScheduledActionStatus.INDETERMINATE:
                await self._save(
                    outcome,
                    status=ParentAttentionOutcomeStatus.INDETERMINATE,
                    reason=action.error or "provider_outcome_indeterminate",
                )
            touched += 1
        return touched

    async def _ensure_outcome(
        self, assessment: ExposureAssessment, assessment_id: str, now: datetime
    ) -> ExposureDeliveryOutcome:
        current = await self._delivery.get_outcome(assessment_id)
        if current is not None:
            return current
        outcome = ExposureDeliveryOutcome(
            assessment_id=assessment_id,
            child_user_id=assessment.child_user_id,
            chat_id=assessment.chat_id,
            evidence_message_ids=tuple(assessment.evidence_message_ids),
            status=ParentAttentionOutcomeStatus.NOT_SENT,
            reason="pending_evaluation",
            created_at=now,
            updated_at=now,
        )
        await self._delivery.save_outcome(outcome)
        return outcome

    async def _save(
        self,
        outcome: ExposureDeliveryOutcome,
        *,
        status: ParentAttentionOutcomeStatus,
        reason: str,
        scheduled_action_id: str | None = None,
        provider_message_id: str | None = None,
        scheduled_at: datetime | None = None,
        sent_at: datetime | None = None,
        failed_at: datetime | None = None,
    ) -> None:
        await self._delivery.save_outcome(
            replace(
                outcome,
                status=status,
                reason=reason,
                scheduled_action_id=(
                    scheduled_action_id
                    if scheduled_action_id is not None
                    else outcome.scheduled_action_id
                ),
                provider_message_id=(
                    provider_message_id
                    if provider_message_id is not None
                    else outcome.provider_message_id
                ),
                scheduled_at=scheduled_at or outcome.scheduled_at,
                sent_at=sent_at or outcome.sent_at,
                failed_at=failed_at or outcome.failed_at,
                updated_at=datetime.now(timezone.utc),
            )
        )


def _provider_message_id(action: ScheduledAction) -> str | None:
    if not action.result:
        return None
    value = action.result.get("provider_message_id")
    return str(value) if value else None


def _overlaps(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return bool(set(left).intersection(right))


__all__ = ["ExposureDeliveryService"]
