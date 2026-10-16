"""Persistence contracts for isolated Baseline B assessment history."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from echo_v2.domain.guard_simple import SafetyAssessment


@runtime_checkable
class GuardSimpleAssessmentRepository(Protocol):
    async def save(self, assessment: SafetyAssessment) -> str: ...

    async def list_for_chat(
        self, *, child_user_id: str, chat_id: str, limit: int = 100
    ) -> list[SafetyAssessment]: ...

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


__all__ = [
    "GuardSimpleAssessmentRepository",
    "InMemoryGuardSimpleAssessmentRepository",
]
