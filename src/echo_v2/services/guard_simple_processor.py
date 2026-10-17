"""Calendar-day processor for the independent Baseline B Guard pipeline."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import replace
from datetime import datetime, time, timezone
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from echo_v2.domain.guard_simple import (
    ChildRole,
    PriorConcern,
    SafetyAssessment,
    SimpleConversation,
)
from echo_v2.persistence.chat_repositories import MessageRepository
from echo_v2.persistence.guard_simple_repositories import (
    GuardSimpleAssessmentRepository,
)
from echo_v2.services.guard_conversation import SimpleGuardConversationBuilder
from echo_v2.services.guard_simple_analyzer import SimpleSafetyAnalyzer
from echo_v2.services.media_enrichment import MediaEnrichmentService

_logger = logging.getLogger("echo_v2.services.guard_simple_processor")

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
        enrichment_service: MediaEnrichmentService | None = None,
        max_messages: int = DEFAULT_SIMPLE_GUARD_MESSAGE_CAP,
    ) -> None:
        if max_messages < 1:
            raise ValueError("max_messages must be positive")
        self._analyzer = analyzer
        self._assessments = assessment_repo
        self._timezone_resolver = timezone_resolver
        self._builder = conversation_builder or SimpleGuardConversationBuilder(
            message_repo, max_messages=max_messages, enrichment_service=enrichment_service
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
        _logger.info(
            "guard simple processor start child_user_id=%s chat_id=%s as_of=%s",
            child_user_id, chat_id, observed_at,
        )
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
        _logger.info(
            "guard simple build conversation child_user_id=%s chat_id=%s timezone=%s prior_concern=%s",
            child_user_id, chat_id, timezone_name, prior.id if prior is not None else None,
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
        _logger.info(
            "guard simple conversation built child_user_id=%s chat_id=%s messages=%d usable=%s incomplete_media=%s identity_verified=%s",
            child_user_id, chat_id, len(conversation.messages), conversation.usable_evidence,
            conversation.incomplete_media_evidence, conversation.identity_verified,
        )
        if conversation.incomplete_media_evidence and not conversation.usable_evidence:
            current = SafetyAssessment(
                parent_should_know=None,
                child_role=ChildRole.UNKNOWN,
                summary="Media evidence is not available yet.",
                evidence_message_ids=(),
                reason="incomplete_media_evidence",
                model="none",
                prompt_version="none",
                analyzer_version="none",
                diagnostics={"llm_skipped": True},
            )
        else:
            _logger.info("guard simple analyzer start child_user_id=%s chat_id=%s", child_user_id, chat_id)
            current = await self._analyzer.analyze(conversation)
            _logger.info(
                "guard simple analyzer complete child_user_id=%s chat_id=%s parent_should_know=%s evidence_count=%d",
                child_user_id, chat_id, current.parent_should_know, len(current.evidence_message_ids),
            )
        parent_should_know = current.parent_should_know
        child_role = current.child_role
        summary = current.summary
        reason = current.reason
        evidence_ids = current.evidence_message_ids
        if conversation.incomplete_media_evidence and parent_should_know is not True:
            parent_should_know = None
            reason = "incomplete_media_evidence"
            summary = "Media evidence is incomplete; the conversation will be retried."
        if not conversation.messages:
            parent_should_know = None
            child_role = ChildRole.UNKNOWN
            summary = "There are no messages in the observed window."
            reason = "The current-day context is empty, so a reliable determination is not possible."
            evidence_ids = ()
        identity_verified = conversation.identity_verified and bool(child_name.strip())
        identity_reason = conversation.identity_reason
        if not identity_verified:
            parent_should_know = None
            child_role = ChildRole.UNKNOWN
            summary = "The monitored child's identity could not be verified in this context."
            reason = identity_reason or "The child's configured name is unavailable."
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
            input_message_ids=conversation.input_message_ids,
            input_content_hash=_conversation_input_hash(conversation),
            child_user_id=child_user_id,
            child_name=child_name,
            connection_id=connection_id,
            chat_id=chat_id,
            chat_type=conversation.chat_type,
            chat_name=conversation.chat_name,
            timezone=conversation.timezone,
            window_start=conversation.window_start,
            window_end=conversation.window_end,
            truncated=conversation.truncated,
            omitted_message_count=conversation.omitted_message_count,
            identity_verified=identity_verified,
            identity_reason=identity_reason,
            created_at=datetime.now(timezone.utc),
            diagnostics={
                **current.diagnostics,
                "incomplete_media_evidence": conversation.incomplete_media_evidence,
                "retryable_media_evidence": any(
                    message.enrichment_status in {"pending", "processing", "failed"}
                    for message in conversation.messages
                    if message.enrichment_status is not None
                ),
                "next_media_retry_at": min(
                    (
                        message.next_retry_at
                        for message in conversation.messages
                        if message.next_retry_at is not None
                    ),
                    default=None,
                ),
                "prior_concern_id": prior.id if prior is not None else None,
                "prior_concern_parent_should_know": (
                    prior.parent_should_know if prior is not None else None
                ),
            },
        )
        return assessment

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


def _conversation_input_hash(conversation: SimpleConversation) -> str:
    messages = conversation.messages
    payload = [
        {
            "id": message.id,
            "text": message.text,
            "timestamp": message.timestamp.isoformat() if message.timestamp else None,
            "enrichment_version": message.enrichment_version,
            "enrichment_status": message.enrichment_status,
            "enrichment_error": message.enrichment_error,
        }
        for message in messages
    ]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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
