"""Live Baseline B worker using the existing Guard activity queue."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Protocol

from echo_v2.domain.guard import GuardChatState
from echo_v2.domain.guard_exposure import ExposureAssessment
from echo_v2.domain.guard_simple import SafetyAssessment
from echo_v2.persistence.guard_exposure_repositories import (
    GuardExposureAssessmentRepository,
)
from echo_v2.persistence.guard_repositories import GuardChatStateRepository
from echo_v2.persistence.guard_simple_repositories import (
    GuardSimpleAssessmentCommitRepository,
)
from echo_v2.services.guard_simple_processor import GuardSimpleProcessor

_logger = logging.getLogger("echo_v2.services.guard_simple_worker")


class ChildNameResolver(Protocol):
    async def get_user_info_by_id(self, user_id: str) -> tuple[str, str | None] | None: ...


class ParentAttentionDelivery(Protocol):
    async def evaluate_committed(
        self, *, assessment: SafetyAssessment, assessment_id: str
    ) -> str: ...

    async def reconcile_pending(self, *, limit: int) -> int: ...

    async def mark_evaluation_failed(self, *, assessment_id: str, error: str) -> None: ...


class ExposureDelivery(Protocol):
    async def cancel_overlapping_security(self, assessment: SafetyAssessment) -> int: ...

    async def evaluate_committed(
        self, *, assessment: ExposureAssessment, assessment_id: str,
        security_assessment: SafetyAssessment | None = None,
    ) -> str: ...

    async def reconcile_pending(self, *, limit: int) -> int: ...

    async def mark_evaluation_failed(self, *, assessment_id: str, error: str) -> None: ...


class IncidentEvaluator(Protocol):
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


class GuardSimpleAnalysisWorker:
    def __init__(
        self,
        *,
        state_repo: GuardChatStateRepository,
        processor: GuardSimpleProcessor,
        commit_repo: GuardSimpleAssessmentCommitRepository,
        users: ChildNameResolver,
        exposure_repo: GuardExposureAssessmentRepository | None = None,
        delivery: ParentAttentionDelivery | None = None,
        exposure_delivery: ExposureDelivery | None = None,
        incident_evaluator: IncidentEvaluator | None = None,
        poll_interval_seconds: float = 10.0,
        excluded_chat_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._state = state_repo
        self._processor = processor
        self._commit = commit_repo
        self._exposure = exposure_repo
        self._users = users
        self._delivery = delivery
        self._exposure_delivery = exposure_delivery
        self._incident_evaluator = incident_evaluator
        self._poll_interval = poll_interval_seconds
        self._excluded = excluded_chat_ids

    def set_delivery(self, delivery: ParentAttentionDelivery) -> None:
        self._delivery = delivery

    def set_exposure_delivery(self, delivery: ExposureDelivery) -> None:
        self._exposure_delivery = delivery

    def set_incident_evaluator(self, evaluator: IncidentEvaluator) -> None:
        self._incident_evaluator = evaluator

    async def run_once(self, *, limit: int = 20) -> bool:
        due = await self._state.list_due(datetime.now(timezone.utc), limit=limit)
        if due:
            _logger.info("guard simple poll due_count=%d limit=%d", len(due), limit)
        touched = False
        for chat in due:
            if chat.chat_id in self._excluded:
                touched = True
                continue
            try:
                await self._process(chat)
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "error processing Baseline B chat %s/%s",
                    chat.child_user_id,
                    chat.chat_id,
                )
            touched = True
        if self._incident_evaluator is not None:
            # Incident Tracker is active — skip legacy reconciliation to avoid
            # duplicate alerts from the old evidence-diff delivery path.
            # Operators must drain pending legacy delivery records before
            # enabling the tracker (see AGENTS.md cutover policy).
            recovered = await self._incident_evaluator.reconcile_pending(limit=limit)
            touched = touched or recovered > 0
        else:
            if self._delivery is not None:
                recovered = await self._delivery.reconcile_pending(limit=limit)
                touched = touched or recovered > 0
            if self._exposure_delivery is not None:
                recovered = await self._exposure_delivery.reconcile_pending(limit=limit)
                touched = touched or recovered > 0
        return touched

    async def run_loop(self) -> None:  # pragma: no cover - lifecycle loop
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("Baseline B worker loop error")
            await asyncio.sleep(self._poll_interval)

    async def _process(self, chat: GuardChatState) -> str:
        if not chat.connection_id:
            raise ValueError("Baseline B chat has no monitored connection")
        user_info = await self._users.get_user_info_by_id(chat.child_user_id)
        child_name = user_info[1] if user_info and user_info[1] else ""
        _logger.info(
            "guard simple analysis start child_user_id=%s chat_id=%s version=%d",
            chat.child_user_id, chat.chat_id, chat.activity_version,
        )
        if self._exposure is not None and chat.is_group:
            assessment, exposure = await self._processor.process_with_exposure(
                child_user_id=chat.child_user_id,
                connection_id=chat.connection_id,
                chat_id=chat.chat_id,
                child_name=child_name,
                target_version=chat.activity_version,
            )
        else:
            assessment = await self._processor.process(
                child_user_id=chat.child_user_id,
                connection_id=chat.connection_id,
                chat_id=chat.chat_id,
                child_name=child_name,
            )
            exposure = None
        assessment = replace(assessment, chat_type="group" if chat.is_group else "private")
        _logger.info(
            "guard simple assessment prepared child_user_id=%s chat_id=%s parent_should_know=%s reason=%s input_count=%d",
            chat.child_user_id, chat.chat_id, assessment.parent_should_know,
            assessment.reason, len(assessment.input_message_ids),
        )
        status, assessment_id = await self._commit.commit_if_current(
            child_user_id=chat.child_user_id,
            chat_id=chat.chat_id,
            target_version=chat.activity_version,
            assessment=assessment,
        )
        exposure_id: str | None = None
        if (
            exposure is not None
            and self._exposure is not None
            and status in {"committed", "completed", "deferred"}
        ):
            exposure_status, exposure_id = await self._exposure.commit_if_current(
                child_user_id=chat.child_user_id,
                chat_id=chat.chat_id,
                target_version=chat.activity_version,
                assessment=exposure,
            )
            if exposure_status == "stale":
                _logger.info(
                    "Exposure assessment skipped as stale for %s/%s version=%d",
                    chat.child_user_id,
                    chat.chat_id,
                    chat.activity_version,
                )
        if status in {"committed", "completed"} and assessment_id and self._incident_evaluator is not None:
            try:
                await self._incident_evaluator.evaluate_security(
                    assessment=assessment,
                    assessment_id=assessment_id,
                )
            except Exception as exc:
                await self._incident_evaluator.mark_evaluation_failed(
                    assessment_id=assessment_id,
                    error=str(exc),
                )
                raise
        elif status in {"committed", "completed"} and assessment_id and self._delivery is not None:
            try:
                await self._delivery.evaluate_committed(
                    assessment=assessment,
                    assessment_id=assessment_id,
                )
            except Exception as exc:
                await self._delivery.mark_evaluation_failed(
                    assessment_id=assessment_id,
                    error=str(exc),
                )
                raise
        if exposure_id and exposure is not None:
            if self._incident_evaluator is not None:
                try:
                    await self._incident_evaluator.evaluate_exposure(
                        assessment=exposure,
                        assessment_id=exposure_id,
                        security_assessment=assessment,
                    )
                except Exception as exc:
                    await self._incident_evaluator.mark_evaluation_failed(
                        assessment_id=exposure_id,
                        error=str(exc),
                    )
                    raise
            elif self._exposure_delivery is not None:
                try:
                    await self._exposure_delivery.evaluate_committed(
                        assessment=exposure,
                        assessment_id=exposure_id,
                        security_assessment=assessment,
                    )
                except Exception as exc:
                    await self._exposure_delivery.mark_evaluation_failed(
                        assessment_id=exposure_id,
                        error=str(exc),
                    )
                    raise
        _logger.info(
            "Baseline B analysis %s for %s/%s version=%d result=%s parent_should_know=%s",
            status,
            chat.child_user_id,
            chat.chat_id,
            chat.activity_version,
            assessment_id,
            assessment.parent_should_know,
        )
        return status


__all__ = [
    "ChildNameResolver",
    "GuardSimpleAnalysisWorker",
    "IncidentEvaluator",
    "ParentAttentionDelivery",
]
