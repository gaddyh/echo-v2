"""Minimal parent-attention notification scheduling for Baseline B."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from echo_v2.domain.guard_simple import SafetyAssessment
from echo_v2.domain.parent_attention import ParentAttentionDeliveryState
from echo_v2.domain.scheduling import ScheduledAction, ScheduledActionType
from echo_v2.persistence.guard_repositories import GuardianChildLinkRepository
from echo_v2.persistence.parent_attention_delivery import (
    ParentAttentionDeliveryRepository,
)
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
        scheduling: SchedulingCreator,
        template_name: str = "guard_parent_attention_v1",
        template_language: str = "he",
    ) -> None:
        self._links = links
        self._users = users
        self._delivery = delivery
        self._scheduling = scheduling
        self._template_name = template_name
        self._template_language = template_language

    async def evaluate_committed(
        self, *, assessment: SafetyAssessment, assessment_id: str
    ) -> str:
        if assessment.parent_should_know is not True:
            return "not_parent_attention"
        current = await self._delivery.get(
            child_user_id=assessment.child_user_id,
            chat_id=assessment.chat_id,
        )
        current_evidence = set(current.last_delivered_evidence_ids) if current else set()
        new_evidence = set(assessment.evidence_message_ids) - current_evidence
        if current is not None and current.resolved_at is None and not new_evidence:
            return "duplicate_evidence"

        link = await self._links.get_active_for_child(assessment.child_user_id)
        if link is None:
            return "no_active_link"
        guardian_phone = await self._users.get_phone_by_id(link.guardian_user_id)
        if guardian_phone is None:
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
                last_delivered_at=now,
                last_action_id=action.id,
                resolved_at=None,
            )
        )
        return "scheduled"

    async def reconcile_pending(self, *, limit: int) -> int:
        del limit
        return 0

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


__all__ = ["ParentAttentionDeliveryService", "SchedulingCreator"]
