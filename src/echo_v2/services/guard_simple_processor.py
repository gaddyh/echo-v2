"""Calendar-day processor for the independent Baseline B Guard pipeline."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timezone
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from echo_v2.domain.guard_simple import (
    ChildRole,
    PriorConcern,
    SafetyAssessment,
)
from echo_v2.persistence.chat_repositories import MessageRepository
from echo_v2.persistence.guard_simple_repositories import (
    GuardSimpleAssessmentRepository,
)
from echo_v2.services.guard_conversation import SimpleGuardConversationBuilder
from echo_v2.services.guard_simple_analyzer import SimpleSafetyAnalyzer

DEFAULT_SIMPLE_GUARD_TIMEZONE = "Asia/Jerusalem"
DEFAULT_SIMPLE_GUARD_MESSAGE_CAP = 100


class ChildTimezoneResolver(Protocol):
    async def get_timezone(self, child_user_id: str) -> str | None: ...


class GuardSimpleProcessor:
    def __init__(
        self,
        *,
        message_repo: MessageRepository,
        analyzer: SimpleSafetyAnalyzer,
        assessment_repo: GuardSimpleAssessmentRepository,
        timezone_resolver: ChildTimezoneResolver | None = None,
        conversation_builder: SimpleGuardConversationBuilder | None = None,
        max_messages: int = DEFAULT_SIMPLE_GUARD_MESSAGE_CAP,
    ) -> None:
        if max_messages < 1:
            raise ValueError("max_messages must be positive")
        self._analyzer = analyzer
        self._assessments = assessment_repo
        self._timezone_resolver = timezone_resolver
        self._builder = conversation_builder or SimpleGuardConversationBuilder(
            message_repo, max_messages=max_messages
        )

    async def process(
        self,
        *,
        child_user_id: str,
        connection_id: str,
        chat_id: str,
        child_name: str,
        as_of: datetime | None = None,
    ) -> SafetyAssessment:
        observed_at = _aware_utc(as_of or datetime.now(timezone.utc))
        timezone_name = await self._timezone(child_user_id)
        local_zone = ZoneInfo(timezone_name)
        local_now = observed_at.astimezone(local_zone)
        current_day_start = datetime.combine(
            local_now.date(), time.min, tzinfo=local_zone
        ).astimezone(timezone.utc)
        prior = await self._assessments.latest_unresolved(
            child_user_id=child_user_id,
            chat_id=chat_id,
            before=current_day_start,
        )
        conversation = await self._builder.build(
            child_user_id=child_user_id,
            child_name=child_name,
            connection_id=connection_id,
            chat_id=chat_id,
            timezone_name=timezone_name,
            as_of=observed_at,
            prior_concern=_prior_concern(prior),
        )
        current = await self._analyzer.analyze(conversation)
        parent_should_know = current.parent_should_know
        child_role = current.child_role
        summary = current.summary
        reason = current.reason
        evidence_ids = current.evidence_message_ids
        if not conversation.messages:
            parent_should_know = None
            child_role = ChildRole.UNKNOWN
            summary = "There are no messages in the observed window."
            reason = "The current-day context is empty, so a reliable determination is not possible."
            evidence_ids = ()
        if not conversation.identity_verified:
            parent_should_know = None
            child_role = ChildRole.UNKNOWN
            summary = "The monitored child's identity could not be verified in this context."
            reason = conversation.identity_reason or "Sender identity is uncertain."
            evidence_ids = ()
        evidence_by_id = {message.id: message for message in conversation.messages}
        evidence_timestamps = tuple(
            message.timestamp
            for message_id in evidence_ids
            if message_id in evidence_by_id
            for message in (evidence_by_id[message_id],)
            if message.timestamp is not None
        )
        current_evidence_at = max(evidence_timestamps, default=None)
        first_detected_at = (
            prior.first_detected_at if prior is not None else observed_at
        ) if parent_should_know is True else None
        last_evidence_at = (
            current_evidence_at
            if parent_should_know is True
            else None
        )
        assessment = replace(
            current,
            parent_should_know=parent_should_know,
            first_detected_at=first_detected_at,
            last_evidence_at=last_evidence_at,
            child_role=child_role,
            summary=summary,
            reason=reason,
            evidence_message_ids=evidence_ids,
            child_user_id=child_user_id,
            connection_id=connection_id,
            chat_id=chat_id,
            chat_type=conversation.chat_type,
            timezone=conversation.timezone,
            window_start=conversation.window_start,
            window_end=conversation.window_end,
            truncated=conversation.truncated,
            omitted_message_count=conversation.omitted_message_count,
            identity_verified=conversation.identity_verified,
            identity_reason=conversation.identity_reason,
            created_at=datetime.now(timezone.utc),
            diagnostics={
                **current.diagnostics,
                "prior_concern_id": prior.id if prior is not None else None,
                "prior_concern_parent_should_know": (
                    prior.parent_should_know if prior is not None else None
                ),
            },
        )
        assessment_id = await self._assessments.save(assessment)
        return replace(assessment, id=assessment_id)

    async def _timezone(self, child_user_id: str) -> str:
        if self._timezone_resolver is None:
            return DEFAULT_SIMPLE_GUARD_TIMEZONE
        configured = await self._timezone_resolver.get_timezone(child_user_id)
        if not configured:
            return DEFAULT_SIMPLE_GUARD_TIMEZONE
        try:
            ZoneInfo(configured)
        except (ZoneInfoNotFoundError, ValueError):
            return DEFAULT_SIMPLE_GUARD_TIMEZONE
        return configured


def _prior_concern(assessment: SafetyAssessment | None) -> PriorConcern | None:
    if assessment is None or assessment.parent_should_know is not True:
        return None
    return PriorConcern(
        parent_should_know=True,
        child_role=assessment.child_role,
        summary=assessment.summary,
        reason=assessment.reason,
        evidence_message_ids=assessment.evidence_message_ids,
        created_at=assessment.created_at or datetime.now(timezone.utc),
        first_detected_at=assessment.first_detected_at
        or assessment.created_at
        or datetime.now(timezone.utc),
        last_evidence_at=assessment.last_evidence_at,
        assessment_id=assessment.id,
    )


def _aware_utc(value: datetime) -> datetime:
    return (value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)).astimezone(
        timezone.utc
    )


__all__ = [
    "DEFAULT_SIMPLE_GUARD_MESSAGE_CAP",
    "DEFAULT_SIMPLE_GUARD_TIMEZONE",
    "ChildTimezoneResolver",
    "GuardSimpleProcessor",
]
