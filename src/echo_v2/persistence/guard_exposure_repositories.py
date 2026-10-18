"""Persistence contracts for Exposure Guard shadow assessments."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Protocol

from echo_v2.domain.guard import GuardChatState
from echo_v2.domain.guard_exposure import ExposureAssessment
from echo_v2.persistence.guard_repositories import GuardChatStateRepository


class GuardExposureAssessmentRepository(Protocol):
    async def save(self, assessment: ExposureAssessment) -> str: ...

    async def get(self, assessment_id: str) -> ExposureAssessment | None: ...

    async def list_for_children(
        self,
        *,
        child_user_ids: list[str],
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        chat_id: str | None = None,
        chat_name: str | None = None,
        decision: str | None = None,
        limit: int = 50,
        before: tuple[datetime, str] | None = None,
    ) -> list[ExposureAssessment]: ...

    async def count_for_children(
        self,
        *,
        child_user_ids: list[str],
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        chat_id: str | None = None,
        chat_name: str | None = None,
        decision: str | None = None,
    ) -> dict[str, int]: ...

    async def commit_if_current(
        self,
        *,
        child_user_id: str,
        chat_id: str,
        target_version: int,
        assessment: ExposureAssessment,
    ) -> tuple[str, str | None]: ...


class InMemoryGuardExposureAssessmentRepository:
    def __init__(self, state_repo: GuardChatStateRepository | None = None) -> None:
        self.assessments: list[ExposureAssessment] = []
        self._state = state_repo

    async def save(self, assessment: ExposureAssessment) -> str:
        assessment_id = assessment.id or f"guard-exposure-{len(self.assessments) + 1}"
        saved = replace(
            assessment,
            id=assessment_id,
            created_at=assessment.created_at or datetime.now(timezone.utc),
        )
        self.assessments.append(saved)
        return assessment_id

    async def get(self, assessment_id: str) -> ExposureAssessment | None:
        return next((item for item in self.assessments if item.id == assessment_id), None)

    async def list_for_children(
        self,
        *,
        child_user_ids: list[str],
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        chat_id: str | None = None,
        chat_name: str | None = None,
        decision: str | None = None,
        limit: int = 50,
        before: tuple[datetime, str] | None = None,
    ) -> list[ExposureAssessment]:
        allowed = set(child_user_ids)
        rows = [
            item for item in self.assessments
            if item.child_user_id in allowed
            and (created_after is None or (item.created_at or datetime.min.replace(tzinfo=timezone.utc)) >= created_after)
            and (created_before is None or (item.created_at or datetime.max.replace(tzinfo=timezone.utc)) < created_before)
            and (chat_id is None or item.chat_id == chat_id)
            and _matches_chat_name(item.chat_name, chat_name)
            and _matches_decision(item, decision)
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
                item for item in rows
                if (item.created_at or datetime.min.replace(tzinfo=timezone.utc), item.id or "") < before
            ]
        return rows[: max(0, limit)]

    async def count_for_children(
        self,
        *,
        child_user_ids: list[str],
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        chat_id: str | None = None,
        chat_name: str | None = None,
        decision: str | None = None,
    ) -> dict[str, int]:
        rows = await self.list_for_children(
            child_user_ids=child_user_ids,
            created_after=created_after,
            created_before=created_before,
            chat_id=chat_id,
            chat_name=chat_name,
            decision=decision,
            limit=len(self.assessments),
        )
        return {
            "analyzed": len(rows),
            "yes": sum(item.significant_exposure is True for item in rows),
            "no": sum(item.significant_exposure is False for item in rows),
            "unknown": sum(item.significant_exposure is None for item in rows),
        }

    async def commit_if_current(
        self,
        *,
        child_user_id: str,
        chat_id: str,
        target_version: int,
        assessment: ExposureAssessment,
    ) -> tuple[str, str | None]:
        if self._state is not None:
            state: GuardChatState | None = await self._state.get(child_user_id, chat_id)
            if state is None or state.activity_version != target_version:
                return "stale", None
        assessment_id = await self.save(replace(assessment, target_version=target_version))
        return "committed", assessment_id


def _matches_chat_name(chat_value: str | None, chat_name: str | None) -> bool:
    return not chat_name or chat_name.strip().casefold() in (chat_value or "").casefold()


def _matches_decision(item: ExposureAssessment, decision: str | None) -> bool:
    if decision is None:
        return True
    return {
        "yes": item.significant_exposure is True,
        "no": item.significant_exposure is False,
        "unknown": item.significant_exposure is None,
    }.get(decision, False)


__all__ = [
    "GuardExposureAssessmentRepository",
    "InMemoryGuardExposureAssessmentRepository",
]
