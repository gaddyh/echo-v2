"""Persistence contracts for isolated Baseline B assessment history."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from echo_v2.domain.guard import GuardChatState
from echo_v2.domain.guard_simple import SafetyAssessment
from echo_v2.domain.parent_attention import (
    ParentAttentionDeliveryOutcome,
    ParentAttentionOutcomeStatus,
)
from echo_v2.persistence.guard_repositories import GuardChatStateRepository
from echo_v2.persistence.parent_attention_delivery import (
    ParentAttentionDeliveryOutcomeRepository,
)
from echo_v2.services.guard_taxonomy import GuardDecision


@runtime_checkable
class GuardSimpleAssessmentCommitRepository(Protocol):
    async def commit_if_current(
        self,
        *,
        child_user_id: str,
        chat_id: str,
        target_version: int,
        assessment: SafetyAssessment,
    ) -> tuple[str, str | None]: ...


class GuardSimpleAssessmentRepository(Protocol):
    async def save(self, assessment: SafetyAssessment) -> str: ...

    async def get(self, assessment_id: str) -> SafetyAssessment | None: ...

    async def list_for_chat(
        self, *, child_user_id: str, chat_id: str, limit: int = 100
    ) -> list[SafetyAssessment]: ...

    async def list_for_children(
        self,
        *,
        child_user_ids: list[str],
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        chat_id: str | None = None,
        decision: str | None = None,
        limit: int = 50,
        before: tuple[datetime, str] | None = None,
    ) -> list[SafetyAssessment]: ...

    async def count_for_children(
        self,
        *,
        child_user_ids: list[str],
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        chat_id: str | None = None,
        decision: str | None = None,
    ) -> dict[str, int]: ...

    async def latest_unresolved(
        self,
        *,
        child_user_id: str,
        chat_id: str,
        before: datetime | None = None,
    ) -> SafetyAssessment | None: ...

    async def resolve(
        self, *, assessment_id: str, resolved_by: str, resolved_at: datetime | None = None
    ) -> bool: ...


class InMemoryGuardSimpleAssessmentRepository:
    def __init__(self) -> None:
        self.assessments: list[SafetyAssessment] = []

    async def get(self, assessment_id: str) -> SafetyAssessment | None:
        return next(
            (assessment for assessment in self.assessments if assessment.id == assessment_id),
            None,
        )

    async def save(self, assessment: SafetyAssessment) -> str:
        assessment_id = assessment.id or f"guard-simple-{len(self.assessments) + 1}"
        saved = replace(
            assessment,
            id=assessment_id,
            created_at=assessment.created_at or datetime.now(timezone.utc),
        )
        self.assessments.append(saved)
        return assessment_id

    async def list_for_chat(
        self, *, child_user_id: str, chat_id: str, limit: int = 100
    ) -> list[SafetyAssessment]:
        rows = [
            assessment
            for assessment in self.assessments
            if assessment.child_user_id == child_user_id
            and assessment.chat_id == chat_id
        ]
        rows.sort(key=lambda item: item.created_at or datetime.min.replace(tzinfo=timezone.utc))
        return rows[-max(0, limit):]

    async def list_for_children(
        self,
        *,
        child_user_ids: list[str],
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        chat_id: str | None = None,
        decision: str | None = None,
        limit: int = 50,
        before: tuple[datetime, str] | None = None,
    ) -> list[SafetyAssessment]:
        allowed = set(child_user_ids)
        rows = [
            assessment
            for assessment in self.assessments
            if assessment.child_user_id in allowed
            and (created_after is None or (assessment.created_at or datetime.min.replace(tzinfo=timezone.utc)) >= created_after)
            and (created_before is None or (assessment.created_at or datetime.max.replace(tzinfo=timezone.utc)) < created_before)
            and (chat_id is None or assessment.chat_id == chat_id)
            and _matches_decision(assessment, decision)
        ]
        rows.sort(
            key=lambda item: (
                item.created_at or datetime.min.replace(tzinfo=timezone.utc),
                item.id or "",
            ),
            reverse=True,
        )
        if before is not None:
            rows = [
                item
                for item in rows
                if (
                    item.created_at or datetime.min.replace(tzinfo=timezone.utc),
                    item.id or "",
                ) < before
            ]
        return rows[: max(0, limit)]

    async def count_for_children(
        self,
        *,
        child_user_ids: list[str],
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        chat_id: str | None = None,
        decision: str | None = None,
    ) -> dict[str, int]:
        allowed = set(child_user_ids)
        rows = [
            assessment
            for assessment in self.assessments
            if assessment.child_user_id in allowed
            and (created_after is None or (assessment.created_at or datetime.min.replace(tzinfo=timezone.utc)) >= created_after)
            and (created_before is None or (assessment.created_at or datetime.max.replace(tzinfo=timezone.utc)) < created_before)
            and (chat_id is None or assessment.chat_id == chat_id)
            and _matches_decision(assessment, decision)
        ]
        return {
            "analyzed": len(rows),
            "yes": sum(assessment.parent_should_know is True for assessment in rows),
            "no": sum(assessment.parent_should_know is False for assessment in rows),
            "unknown": sum(assessment.parent_should_know is None for assessment in rows),
        }

    async def latest_unresolved(
        self,
        *,
        child_user_id: str,
        chat_id: str,
        before: datetime | None = None,
    ) -> SafetyAssessment | None:
        rows = [
            assessment
            for assessment in self.assessments
            if assessment.child_user_id == child_user_id
            and assessment.chat_id == chat_id
            and assessment.parent_should_know is True
            and assessment.resolved_at is None
            and (before is None or (assessment.created_at or before) < before)
        ]
        return max(
            rows,
            key=lambda item: item.created_at or datetime.min.replace(tzinfo=timezone.utc),
            default=None,
        )

    async def resolve(
        self, *, assessment_id: str, resolved_by: str, resolved_at: datetime | None = None
    ) -> bool:
        for index, assessment in enumerate(self.assessments):
            if assessment.id == assessment_id:
                self.assessments[index] = replace(
                    assessment,
                    resolved_at=resolved_at or datetime.now(timezone.utc),
                    resolved_by=resolved_by,
                )
                return True
        return False


class InMemoryGuardSimpleAssessmentCommitRepository:
    def __init__(
        self,
        state_repo: GuardChatStateRepository,
        assessment_repo: InMemoryGuardSimpleAssessmentRepository,
        outcome_repo: ParentAttentionDeliveryOutcomeRepository | None = None,
    ) -> None:
        self._state = state_repo
        self._assessments = assessment_repo
        self._outcomes = outcome_repo

    async def commit_if_current(
        self,
        *,
        child_user_id: str,
        chat_id: str,
        target_version: int,
        assessment: SafetyAssessment,
    ) -> tuple[str, str | None]:
        state: GuardChatState | None = await self._state.get(child_user_id, chat_id)
        if state is None or state.activity_version != target_version:
            return "stale", None
        assessment_id = await self._assessments.save(assessment)
        if self._outcomes is not None:
            now = datetime.now(timezone.utc)
            await self._outcomes.save(
                ParentAttentionDeliveryOutcome(
                    assessment_id=assessment_id,
                    child_user_id=assessment.child_user_id,
                    chat_id=assessment.chat_id,
                    status=ParentAttentionOutcomeStatus.NOT_SENT,
                    reason="pending_evaluation",
                    created_at=now,
                    updated_at=now,
                )
            )
        committed = await self._state.mark_processed(
            child_user_id,
            chat_id,
            target_version,
            decision=GuardDecision.NONE,
            analyzed_at=datetime.now(timezone.utc),
            parent_should_know=assessment.parent_should_know,
        )
        return ("committed", assessment_id) if committed else ("stale", None)


def _matches_decision(assessment: SafetyAssessment, decision: str | None) -> bool:
    if decision is None:
        return True
    return {
        "yes": assessment.parent_should_know is True,
        "no": assessment.parent_should_know is False,
        "unknown": assessment.parent_should_know is None,
    }.get(decision, False)


__all__ = [
    "GuardSimpleAssessmentCommitRepository",
    "GuardSimpleAssessmentRepository",
    "InMemoryGuardSimpleAssessmentCommitRepository",
    "InMemoryGuardSimpleAssessmentRepository",
]
