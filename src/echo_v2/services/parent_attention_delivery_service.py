"""Minimal parent-attention notification scheduling for Baseline B."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Protocol

from echo_v2.domain.guard_simple import SafetyAssessment
from echo_v2.domain.parent_attention import (
    ParentAttentionDeliveryOutcome,
    ParentAttentionDeliveryState,
    ParentAttentionOutcomeStatus,
)
from echo_v2.domain.scheduling import (
    ScheduledAction,
    ScheduledActionStatus,
    ScheduledActionType,
)
from echo_v2.persistence.guard_repositories import GuardianChildLinkRepository
from echo_v2.persistence.guard_simple_repositories import (
    GuardSimpleAssessmentRepository,
)
from echo_v2.persistence.parent_attention_delivery import (
    ParentAttentionDeliveryOutcomeRepository,
    ParentAttentionDeliveryRepository,
)
from echo_v2.persistence.scheduled_actions import ScheduledActionRepository
from echo_v2.persistence.user_repository import PostgresUserRepository


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
    ) -> tuple[ScheduledAction, bool]: ...


class ParentAttentionDeliveryService:
    def __init__(
        self,
        *,
        links: GuardianChildLinkRepository,
        users: PostgresUserRepository,
        delivery: ParentAttentionDeliveryRepository,
        outcomes: ParentAttentionDeliveryOutcomeRepository,
        assessments: GuardSimpleAssessmentRepository,
        actions: ScheduledActionRepository,
        scheduling: SchedulingCreator,
        template_name: str = "guard_parent_attention_v1",
        template_language: str = "he",
        enabled: bool = False,
    ) -> None:
        self._links = links
        self._users = users
        self._delivery = delivery
        self._outcomes = outcomes
        self._assessments = assessments
        self._actions = actions
        self._scheduling = scheduling
        self._template_name = template_name
        self._template_language = template_language
        self._enabled = enabled

    async def evaluate_committed(
        self, *, assessment: SafetyAssessment, assessment_id: str
    ) -> str:
        outcome = await self._ensure_outcome(assessment, assessment_id)
        if not self._enabled:
            await self._save_outcome(
                outcome,
                status=ParentAttentionOutcomeStatus.NOT_SENT,
                reason="disabled",
            )
            return "disabled"
        if assessment.parent_should_know is not True:
            await self._save_outcome(
                outcome,
                status=ParentAttentionOutcomeStatus.NOT_SENT,
                reason="not_parent_attention",
            )
            return "not_parent_attention"

        current = await self._delivery.get(
            child_user_id=assessment.child_user_id,
            chat_id=assessment.chat_id,
        )
        current_evidence = set(current.last_delivered_evidence_ids) if current else set()
        new_evidence = set(assessment.evidence_message_ids) - current_evidence
        if current is not None and current.resolved_at is None and not new_evidence:
            await self._save_outcome(
                outcome,
                status=ParentAttentionOutcomeStatus.NOT_SENT,
                reason="already_notified",
            )
            return "duplicate_evidence"

        link = await self._links.get_active_for_child(assessment.child_user_id)
        if link is None:
            await self._save_outcome(
                outcome,
                status=ParentAttentionOutcomeStatus.NOT_SENT,
                reason="no_active_link",
            )
            return "no_active_link"
        guardian_phone = await self._users.get_phone_by_id(link.guardian_user_id)
        if guardian_phone is None:
            await self._save_outcome(
                outcome,
                status=ParentAttentionOutcomeStatus.NOT_SENT,
                reason="no_guardian_phone",
            )
            return "no_guardian_phone"

        now = datetime.now(timezone.utc)
        description = assessment.summary or assessment.reason
        request_id = (
            f"guard-parent-attention:{assessment.child_user_id}:"
            f"{assessment.chat_id}:{assessment_id}"
        )
        action, _created = await self._scheduling.create_once(
            request_id=request_id,
            user_id=link.guardian_user_id,
            type=ScheduledActionType.SEND_BOT_MESSAGE,
            execute_at_utc=now,
            timezone_name="UTC",
            payload={
                "kind": "guard_parent_attention",
                "chat_id": guardian_phone,
                "guardian_user_id": link.guardian_user_id,
                "child_user_id": assessment.child_user_id,
                "assessment_id": assessment_id,
                "evidence_message_ids": list(assessment.evidence_message_ids),
                "idempotency_key": f"guard-parent-attention:{request_id}",
                "message": "",
                "template": {
                    "name": self._template_name,
                    "language": self._template_language,
                    "body_params": [
                        assessment.child_name,
                        description,
                        assessment.chat_name or assessment.chat_id,
                    ],
                },
            },
        )
        await self._delivery.save(
            ParentAttentionDeliveryState(
                child_user_id=assessment.child_user_id,
                chat_id=assessment.chat_id,
                last_delivered_assessment_id=assessment_id,
                last_delivered_evidence_ids=tuple(assessment.evidence_message_ids),
                last_scheduled_at=now,
                last_action_id=action.id,
                resolved_at=None,
            )
        )
        await self._save_outcome(
            outcome,
            status=ParentAttentionOutcomeStatus.SCHEDULED,
            reason="scheduled",
            scheduled_action_id=action.id,
            scheduled_at=now,
        )
        return "scheduled"

    async def mark_evaluation_failed(self, *, assessment_id: str, error: str) -> None:
        outcome = await self._outcomes.get(assessment_id)
        if outcome is None:
            return
        await self._outcomes.save(
            replace(
                outcome,
                status=ParentAttentionOutcomeStatus.FAILED,
                reason=error[:500],
                updated_at=datetime.now(timezone.utc),
                failed_at=datetime.now(timezone.utc),
            )
        )

    async def reconcile_pending(self, *, limit: int) -> int:
        touched = 0
        pending = await self._outcomes.list_pending_evaluation(limit=limit)
        for outcome in pending:
            assessment = await self._assessments.get(outcome.assessment_id)
            if assessment is None:
                await self.mark_evaluation_failed(
                    assessment_id=outcome.assessment_id,
                    error="assessment unavailable during delivery evaluation",
                )
            else:
                try:
                    await self.evaluate_committed(
                        assessment=assessment,
                        assessment_id=outcome.assessment_id,
                    )
                except Exception as exc:  # noqa: BLE001 - preserve pending telemetry on evaluation errors
                    await self.mark_evaluation_failed(
                        assessment_id=outcome.assessment_id,
                        error=str(exc),
                    )
            touched += 1

        for outcome in await self._outcomes.list_reconcilable(limit=limit):
            if outcome.scheduled_action_id is None:
                await self._save_outcome(
                    outcome,
                    status=ParentAttentionOutcomeStatus.INDETERMINATE,
                    reason="scheduled_action_missing",
                )
                touched += 1
                continue
            action = await self._actions.get(outcome.scheduled_action_id)
            if action is None:
                await self._save_outcome(
                    outcome,
                    status=ParentAttentionOutcomeStatus.INDETERMINATE,
                    reason="scheduled_action_missing",
                )
                touched += 1
                continue
            if action.status in {ScheduledActionStatus.PENDING, ScheduledActionStatus.IN_PROGRESS}:
                continue
            if action.status is ScheduledActionStatus.SUCCEEDED:
                provider_message_id = _provider_message_id(action)
                if provider_message_id is None:
                    await self._save_outcome(
                        outcome,
                        status=ParentAttentionOutcomeStatus.INDETERMINATE,
                        reason="provider_result_missing",
                    )
                else:
                    await self._save_outcome(
                        outcome,
                        status=ParentAttentionOutcomeStatus.SENT,
                        reason="provider_confirmed",
                        provider_message_id=provider_message_id,
                        sent_at=action.executed_at or datetime.now(timezone.utc),
                    )
            elif action.status is ScheduledActionStatus.CANCELLED:
                await self._save_outcome(
                    outcome,
                    status=ParentAttentionOutcomeStatus.NOT_SENT,
                    reason=action.error or "suppressed",
                )
            elif action.status is ScheduledActionStatus.FAILED:
                await self._save_outcome(
                    outcome,
                    status=ParentAttentionOutcomeStatus.FAILED,
                    reason=action.error or "scheduled_action_failed",
                    failed_at=action.executed_at or datetime.now(timezone.utc),
                )
            elif action.status is ScheduledActionStatus.INDETERMINATE:
                await self._save_outcome(
                    outcome,
                    status=ParentAttentionOutcomeStatus.INDETERMINATE,
                    reason=action.error or "provider_outcome_indeterminate",
                )
            touched += 1
        return touched

    async def validate_before_send(self, payload: dict[str, object]) -> bool:
        guardian_user_id = str(payload.get("guardian_user_id", ""))
        child_user_id = str(payload.get("child_user_id", ""))
        if not guardian_user_id or not child_user_id:
            return False
        return (
            await self._links.get_active_for_pair(
                guardian_user_id=guardian_user_id,
                child_user_id=child_user_id,
            )
        ) is not None

    async def _ensure_outcome(
        self, assessment: SafetyAssessment, assessment_id: str
    ) -> ParentAttentionDeliveryOutcome:
        current = await self._outcomes.get(assessment_id)
        if current is not None:
            return current
        now = datetime.now(timezone.utc)
        outcome = ParentAttentionDeliveryOutcome(
            assessment_id=assessment_id,
            child_user_id=assessment.child_user_id,
            chat_id=assessment.chat_id,
            status=ParentAttentionOutcomeStatus.NOT_SENT,
            reason="pending_evaluation",
            created_at=now,
            updated_at=now,
        )
        await self._outcomes.save(outcome)
        return outcome

    async def _save_outcome(
        self,
        outcome: ParentAttentionDeliveryOutcome,
        *,
        status: ParentAttentionOutcomeStatus,
        reason: str,
        scheduled_action_id: str | None = None,
        provider_message_id: str | None = None,
        scheduled_at: datetime | None = None,
        sent_at: datetime | None = None,
        failed_at: datetime | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        await self._outcomes.save(
            replace(
                outcome,
                status=status,
                reason=reason,
                scheduled_action_id=scheduled_action_id or outcome.scheduled_action_id,
                provider_message_id=provider_message_id or outcome.provider_message_id,
                updated_at=now,
                scheduled_at=scheduled_at or outcome.scheduled_at,
                sent_at=sent_at or outcome.sent_at,
                failed_at=failed_at or outcome.failed_at,
            )
        )


def _provider_message_id(action: ScheduledAction) -> str | None:
    if not action.result:
        return None
    value = action.result.get("provider_message_id")
    return str(value) if value else None


__all__ = ["ParentAttentionDeliveryService", "SchedulingCreator"]
