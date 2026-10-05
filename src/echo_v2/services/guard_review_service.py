"""Guardian-scoped operator review of immutable Guard shadow results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from echo_v2.domain.chat import Message
from echo_v2.domain.guard import GuardAnalysisRecord
from echo_v2.domain.guard_feedback import GuardAnalysisFeedback, GuardFeedbackLabel
from echo_v2.persistence.chat_repositories import MessageRepository
from echo_v2.persistence.guard_feedback import GuardFeedbackRepository
from echo_v2.persistence.guard_repositories import (
    GuardAnalysisRepository,
    GuardianChildLinkRepository,
)
from echo_v2.services.waiting_list_token_service import WaitingListTokenService


@dataclass(frozen=True)
class GuardEvidenceMessage:
    id: str
    direction: str
    sender: str | None
    text: str
    timestamp: str


@dataclass(frozen=True)
class GuardReviewEntry:
    id: str
    child_user_id: str
    chat_id: str
    chat_name: str | None
    created_at: str
    target_version: int
    previous_target_version: int | None
    previous_decision: str | None
    decision: str
    categories: tuple[str, ...]
    signals: tuple[str, ...]
    confidence: float
    reason: str
    evidence_message_ids: tuple[str, ...]
    evidence_messages: tuple[GuardEvidenceMessage, ...]
    is_group: bool
    schedule_reason: str | None
    pending_since: str | None
    scheduled_for: str | None
    scheduled_delay_seconds: float | None
    actual_delay_seconds: float | None
    scheduler_lag_seconds: float | None
    activity_delta: int | None
    feedback: GuardAnalysisFeedback | None


@dataclass(frozen=True)
class GuardReviewResponse:
    guardian_user_id: str
    results: list[GuardReviewEntry]
    total_results: int


class GuardReviewService:
    def __init__(
        self,
        *,
        token_service: WaitingListTokenService,
        links: GuardianChildLinkRepository,
        analyses: GuardAnalysisRepository,
        feedback: GuardFeedbackRepository,
        messages: MessageRepository,
    ) -> None:
        self._tokens = token_service
        self._links = links
        self._analyses = analyses
        self._feedback = feedback
        self._messages = messages

    async def list_reviews(
        self,
        *,
        session_id: str,
        decision: str | None = None,
        category: str | None = None,
        chat_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> GuardReviewResponse | None:
        guardian_id, child_ids = await self._authorized_children(session_id)
        if guardian_id is None:
            return None
        records = await self._analyses.list_for_children(child_user_ids=child_ids)
        records = [
            record for record in records
            if self._matches(record, decision, category, chat_id, date_from, date_to)
        ]
        feedback = await self._feedback.get_for_results([self._result_id(record) for record in records])
        messages_by_chat: dict[tuple[str, str], dict[str, Message]] = {}
        for record in records:
            key = (record.child_user_id, record.chat_id)
            if key not in messages_by_chat:
                messages = await self._messages.list_recent_for_chat(
                    user_id=record.child_user_id, chat_id=record.chat_id, limit=100
                )
                messages_by_chat[key] = {message.id: message for message in messages}
        previous: dict[tuple[str, str], GuardAnalysisRecord | None] = {}
        prior_by_id: dict[str, GuardAnalysisRecord | None] = {}
        for record in sorted(
                records,
                key=lambda item: (
                    item.created_at or datetime.min.replace(tzinfo=timezone.utc),
                    item.target_version,
                ),
            ):
            key = (record.child_user_id, record.chat_id)
            prior_by_id[self._result_id(record)] = previous.get(key)
            previous[key] = record
        entries = [
            self._entry(
                record,
                prior_by_id[self._result_id(record)],
                feedback.get(self._result_id(record)),
                messages_by_chat[(record.child_user_id, record.chat_id)],
            )
            for record in records
        ]
        return GuardReviewResponse(guardian_user_id=guardian_id, results=entries, total_results=len(entries))

    async def set_feedback(
        self, *, session_id: str, result_id: str, label: GuardFeedbackLabel, note: str | None
    ) -> GuardAnalysisFeedback | None:
        guardian_id, child_ids = await self._authorized_children(session_id)
        if guardian_id is None:
            return None
        records = await self._analyses.list_for_children(child_user_ids=child_ids)
        if not any(self._result_id(record) == result_id for record in records):
            return None
        return await self._feedback.set_feedback(
            result_id=result_id, reviewer_user_id=guardian_id, label=label, note=note
        )

    async def _authorized_children(self, session_id: str) -> tuple[str | None, list[str]]:
        resolved = await self._tokens.resolve_session(session_id)
        if resolved is None:
            return None, []
        links = await self._links.list_active_for_guardian(resolved.user_id)
        return resolved.user_id, [link.child_user_id for link in links]

    @staticmethod
    def _result_id(record: GuardAnalysisRecord) -> str:
        return record.id or ""

    @staticmethod
    def _matches(
        record: GuardAnalysisRecord,
        decision: str | None,
        category: str | None,
        chat_id: str | None,
        date_from: datetime | None,
        date_to: datetime | None,
    ) -> bool:
        created = record.created_at
        return (
            (decision is None or record.decision.value == decision)
            and (category is None or category in record.categories)
            and (chat_id is None or record.chat_id == chat_id)
            and (date_from is None or (created is not None and created >= date_from))
            and (date_to is None or (created is not None and created <= date_to))
        )

    @classmethod
    def _entry(
        cls, record: GuardAnalysisRecord, prior: GuardAnalysisRecord | None,
        feedback: GuardAnalysisFeedback | None, messages: dict[str, Message],
    ) -> GuardReviewEntry:
        pending = record.pending_since
        scheduled = record.scheduled_for
        created = record.created_at
        scheduled_delay = (scheduled - pending).total_seconds() if scheduled and pending else None
        actual_delay = (created - pending).total_seconds() if created and pending else None
        lag = (created - scheduled).total_seconds() if created and scheduled else None
        return GuardReviewEntry(
            id=cls._result_id(record), child_user_id=record.child_user_id, chat_id=record.chat_id,
            chat_name=record.chat_name, created_at=created.isoformat() if created else "", target_version=record.target_version,
            previous_target_version=prior.target_version if prior else None,
            previous_decision=prior.decision.value if prior else None,
            decision=record.decision.value, categories=record.categories, signals=record.signals,
            confidence=record.confidence, reason=record.reason,
            evidence_message_ids=record.evidence_message_ids,
            evidence_messages=tuple(
                GuardEvidenceMessage(
                    id=message.id, direction=message.direction.value,
                    sender=message.sender_name or message.sender_id, text=message.text or "",
                    timestamp=message.timestamp.isoformat(),
                )
                for message_id in record.evidence_message_ids
                if (message := messages.get(message_id)) is not None
            ),
            is_group=record.chat_id.endswith("@g.us"), schedule_reason=record.schedule_reason,
            pending_since=pending.isoformat() if pending else None,
            scheduled_for=scheduled.isoformat() if scheduled else None,
            scheduled_delay_seconds=scheduled_delay, actual_delay_seconds=actual_delay,
            scheduler_lag_seconds=lag,
            activity_delta=record.target_version - prior.target_version if prior else None,
            feedback=feedback,
        )
