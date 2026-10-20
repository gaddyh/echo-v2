"""Read-only Baseline B telemetry for the Guard Activity page."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, cast

from echo_v2.domain.chat import Message
from echo_v2.domain.guard import GuardChatState
from echo_v2.domain.guard_exposure import ExposureAssessment
from echo_v2.domain.guard_simple import SafetyAssessment
from echo_v2.domain.parent_attention import (
    ParentAttentionDeliveryOutcome,
    ParentAttentionOutcomeStatus,
)
from echo_v2.domain.scheduling import ScheduledAction
from echo_v2.persistence.chat_repositories import MessageRepository
from echo_v2.persistence.guard_exposure_repositories import (
    GuardExposureAssessmentRepository,
)
from echo_v2.persistence.guard_repositories import (
    GuardChatStateRepository,
    GuardianChildLinkRepository,
)
from echo_v2.persistence.guard_simple_repositories import (
    GuardSimpleAssessmentRepository,
)
from echo_v2.persistence.parent_attention_delivery import (
    ParentAttentionDeliveryOutcomeRepository,
)
from echo_v2.persistence.scheduled_actions import ScheduledActionRepository
from echo_v2.services.waiting_list_token_service import WaitingListTokenService

_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 50


class UserInfoResolver(Protocol):
    async def get_user_info_by_id(self, user_id: str) -> tuple[str, str | None] | None: ...


@dataclass(frozen=True)
class GuardActivityResponse:
    guardian_user_id: str
    generated_at: datetime
    summary: dict[str, int]
    children: tuple[dict[str, str], ...]
    analyses: tuple[dict[str, Any], ...]
    next_cursor: str | None


class GuardSimpleTelemetryService:
    def __init__(
        self,
        *,
        token_service: WaitingListTokenService,
        links: GuardianChildLinkRepository,
        users: UserInfoResolver,
        chat_states: GuardChatStateRepository,
        assessments: GuardSimpleAssessmentRepository,
        outcomes: ParentAttentionDeliveryOutcomeRepository,
        actions: ScheduledActionRepository,
        messages: MessageRepository,
        exposure_assessments: GuardExposureAssessmentRepository | None = None,
    ) -> None:
        self._tokens = token_service
        self._links = links
        self._users = users
        self._chat_states = chat_states
        self._assessments = assessments
        self._exposure_assessments = exposure_assessments
        self._outcomes = outcomes
        self._actions = actions
        self._messages = messages

    async def list_activity(
        self,
        *,
        session_id: str,
        child_user_id: str | None = None,
        chat_id: str | None = None,
        chat_name: str | None = None,
        decision: str | None = None,
        analyzer: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = _DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
        admin: bool = False,
    ) -> GuardActivityResponse | None:
        resolved = await self._tokens.resolve_session(session_id)
        if resolved is None:
            return None
        links = (
            await self._links.list_all_active()
            if admin
            else await self._links.list_active_for_guardian(resolved.user_id)
        )
        linked_child_ids = [link.child_user_id for link in links]
        child_ids = linked_child_ids
        if child_user_id is not None:
            child_ids = [child_user_id] if child_user_id in linked_child_ids else []
        now = datetime.now(timezone.utc)
        start = date_from or now - timedelta(hours=24)
        end = date_to or now
        if end <= start:
            end = start + timedelta(microseconds=1)
        normalized_decision = _normalize_decision(decision)
        before = _decode_cursor(cursor) if cursor else None
        page_size = max(1, min(limit, _MAX_PAGE_SIZE))
        child_rows = [] if analyzer == "exposure" else await self._assessments.list_for_children(
            child_user_ids=child_ids,
            created_after=start,
            created_before=end,
            chat_id=chat_id,
            chat_name=chat_name,
            decision=normalized_decision,
            limit=page_size + 1,
            before=before,
        )
        exposure_rows = []
        if analyzer != "child" and self._exposure_assessments is not None:
            exposure_rows = await self._exposure_assessments.list_for_children(
                child_user_ids=child_ids,
                created_after=start,
                created_before=end,
                chat_id=chat_id,
                decision=normalized_decision,
                chat_name=chat_name,
                limit=page_size + 1,
                before=before,
            )
        all_rows = [(item, "child") for item in child_rows] + [(item, "exposure") for item in exposure_rows]
        all_rows.sort(key=lambda pair: (pair[0].created_at or datetime.min.replace(tzinfo=timezone.utc), pair[0].id or ""), reverse=True)
        has_more = len(all_rows) > page_size
        all_rows = all_rows[:page_size]
        rows = [item for item, _ in all_rows]
        outcome_rows, states, child_names, parent_names = await asyncio.gather(
            self._outcomes.list_for_assessments(
                [assessment.id for assessment in rows if assessment.id is not None]
            ),
            self._chat_states.list_for_children(child_ids),
            self._child_names(linked_child_ids),
            self._user_names([link.guardian_user_id for link in links]),
        )
        outcomes: dict[str, list[ParentAttentionDeliveryOutcome]] = {}
        for outcome in outcome_rows:
            outcomes.setdefault(outcome.assessment_id, []).append(outcome)
        state_by_chat = {(state.child_user_id, state.chat_id): state for state in states}
        analyses = tuple(
            _activity_view(
                cast(SafetyAssessment, assessment),
                outcomes.get(assessment.id, []) if assessment.id is not None else [],
                state_by_chat.get((assessment.child_user_id, assessment.chat_id)),
                child_names.get(assessment.child_user_id),
            ) if kind == "child" else _exposure_activity_view(
                cast(ExposureAssessment, assessment),
                state_by_chat.get((assessment.child_user_id, assessment.chat_id)),
                child_names.get(assessment.child_user_id),
            )
            for assessment, kind in all_rows
        )
        child_counts, exposure_counts, notification_count = await asyncio.gather(
            self._assessments.count_for_children(
                child_user_ids=child_ids,
                created_after=start,
                created_before=end,
                chat_id=chat_id,
                chat_name=chat_name,
                decision=normalized_decision,
            ) if analyzer != "exposure" else _empty_counts(),
            self._exposure_assessments.count_for_children(
                child_user_ids=child_ids,
                created_after=start,
                created_before=end,
                chat_id=chat_id,
                chat_name=chat_name,
                decision=normalized_decision,
            ) if self._exposure_assessments is not None and analyzer != "child" else _empty_counts(),
            self._outcomes.count_sent(
                child_user_ids=child_ids,
                created_after=start,
                created_before=end,
                chat_id=chat_id,
            ),
        )
        counts = {
            key: child_counts[key] + exposure_counts[key]
            for key in ("analyzed", "yes", "no", "unknown")
        }
        counts["notifications"] = notification_count
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            if last.id is not None and last.created_at is not None:
                next_cursor = _encode_cursor(last.created_at, last.id)
        return GuardActivityResponse(
            guardian_user_id=resolved.user_id,
            generated_at=now,
            summary=counts,
            children=tuple(
                {
                    "id": link.child_user_id,
                    "name": child_names.get(link.child_user_id, link.child_user_id),
                    "parent_name": parent_names.get(link.guardian_user_id, link.guardian_user_id),
                }
                for link in links
            ),
            analyses=analyses,
            next_cursor=next_cursor,
        )

    async def get_analysis(
        self, *, session_id: str, assessment_id: str, admin: bool = False
    ) -> dict[str, Any] | None:
        authorized = await self._authorized_child_ids(session_id, admin=admin)
        if authorized is None:
            return None
        assessment = await self._assessments.get(assessment_id)
        if assessment is None:
            exposure = await self._exposure_assessments.get(assessment_id) if self._exposure_assessments else None
            if exposure is None or exposure.child_user_id not in authorized:
                return None
            return await _exposure_detail(exposure)
        if assessment.child_user_id not in authorized:
            return None
        states = await self._chat_states.list_for_children([assessment.child_user_id])
        state = next(
            (
                item
                for item in states
                if item.chat_id == assessment.chat_id
            ),
            None,
        )
        outcome_rows = await self._outcomes.list_for_assessments([assessment_id])
        outcome = outcome_rows[0] if outcome_rows else None
        action = await self._action_for(outcome)
        evidence = await self._messages.list_by_ids(
            user_id=assessment.child_user_id,
            chat_id=assessment.chat_id,
            message_ids=set(assessment.evidence_message_ids),
        )
        evidence_by_id = {message.id: message for message in evidence}
        child_name = assessment.child_name or assessment.child_user_id
        return {
            "id": assessment_id,
            "decision": _decision(assessment),
            "child_user_id": assessment.child_user_id,
            "child_name": child_name,
            "chat_id": assessment.chat_id,
            "chat_name": assessment.chat_name or (state.chat_name if state else None) or assessment.chat_id,
            "chat_type": assessment.chat_type,
            "created_at": assessment.created_at,
            "summary": assessment.summary,
            "reason": assessment.reason,
            "evidence_message_ids": list(assessment.evidence_message_ids),
            "evidence_messages": [
                _message_view(evidence_by_id[message_id], cited=True)
                if message_id in evidence_by_id
                else {"id": message_id, "unavailable": True, "cited": True}
                for message_id in assessment.evidence_message_ids
            ],
            "context_message_count": len(assessment.input_message_ids),
            "window_start": assessment.window_start,
            "window_end": assessment.window_end,
            "truncated": assessment.truncated,
            "omitted_message_count": assessment.omitted_message_count,
            "identity_verified": assessment.identity_verified,
            "delivery": _delivery_view(outcome, action),
            "queue": _queue_view(state),
            "context_available": bool(assessment.input_message_ids)
            or assessment.window_start is not None,
        }

    async def get_context(
        self, *, session_id: str, assessment_id: str, admin: bool = False
    ) -> dict[str, Any] | None:
        authorized = await self._authorized_child_ids(session_id, admin=admin)
        if authorized is None:
            return None
        assessment = await self._assessments.get(assessment_id)
        if assessment is None:
            exposure = await self._exposure_assessments.get(assessment_id) if self._exposure_assessments else None
            if exposure is None or exposure.child_user_id not in authorized:
                return None
            return {
                "trust": "Exposure context is intentionally hidden",
                "messages": [],
                "missing_message_ids": list(exposure.input_message_ids),
                "evidence_message_ids": list(exposure.evidence_message_ids),
            }
        if assessment.child_user_id not in authorized:
            return None
        if assessment.input_message_ids:
            messages = await self._messages.list_by_ids(
                user_id=assessment.child_user_id,
                chat_id=assessment.chat_id,
                message_ids=set(assessment.input_message_ids),
            )
            by_id = {message.id: message for message in messages}
            views = [
                _message_view(by_id[message_id], cited=message_id in assessment.evidence_message_ids)
                if message_id in by_id
                else {
                    "id": message_id,
                    "unavailable": True,
                    "cited": message_id in assessment.evidence_message_ids,
                }
                for message_id in assessment.input_message_ids
            ]
            current_hash = _message_hash([by_id[message_id] for message_id in assessment.input_message_ids if message_id in by_id])
            trust = (
                "exact message selection, verified content"
                if current_hash == assessment.input_content_hash
                else "exact message selection, content changed"
            )
            return {
                "trust": trust,
                "messages": views,
                "missing_message_ids": [
                    message_id for message_id in assessment.input_message_ids if message_id not in by_id
                ],
                "content_hash": assessment.input_content_hash,
                "current_content_hash": current_hash,
                "truncated": assessment.truncated,
                "omitted_message_count": assessment.omitted_message_count,
            }
        if assessment.window_start is None or assessment.window_end is None:
            return {
                "trust": "best-effort reconstruction, historical",
                "messages": [],
                "missing_message_ids": [],
                "truncated": assessment.truncated,
                "omitted_message_count": assessment.omitted_message_count,
            }
        window = await self._messages.list_for_chat_window(
            user_id=assessment.child_user_id,
            chat_id=assessment.chat_id,
            since=assessment.window_start,
            until=assessment.window_end + timedelta(microseconds=1),
            limit=max(100, len(assessment.evidence_message_ids)),
        )
        evidence_ids = set(assessment.evidence_message_ids)
        return {
            "trust": "best-effort reconstruction, historical",
            "messages": [_message_view(message, cited=message.id in evidence_ids) for message in window.messages],
            "missing_message_ids": [],
            "truncated": window.truncated,
            "omitted_message_count": window.omitted_message_count,
        }

    async def _authorized_child_ids(
        self, session_id: str, *, admin: bool = False
    ) -> set[str] | None:
        resolved = await self._tokens.resolve_session(session_id)
        if resolved is None:
            return None
        links = (
            await self._links.list_all_active()
            if admin
            else await self._links.list_active_for_guardian(resolved.user_id)
        )
        return {link.child_user_id for link in links}

    async def _user_names(self, user_ids: list[str]) -> dict[str, str]:
        unique_ids = list(dict.fromkeys(user_ids))
        infos = await asyncio.gather(
            *(self._users.get_user_info_by_id(user_id) for user_id in unique_ids)
        )
        return {
            user_id: (info[1] if info and info[1] else user_id)
            for user_id, info in zip(unique_ids, infos)
        }

    async def _child_names(self, child_ids: list[str]) -> dict[str, str]:
        return await self._user_names(child_ids)

    async def _action_for(
        self, outcome: ParentAttentionDeliveryOutcome | None
    ) -> ScheduledAction | None:
        if outcome is None or outcome.scheduled_action_id is None:
            return None
        return await self._actions.get(outcome.scheduled_action_id)


async def _empty_counts() -> dict[str, int]:
    return {"analyzed": 0, "yes": 0, "no": 0, "unknown": 0}


def _exposure_activity_view(
    assessment: ExposureAssessment,
    state: GuardChatState | None,
    child_name: str | None,
) -> dict[str, Any]:
    return {
        "id": assessment.id,
        "analyzer": "exposure",
        "created_at": assessment.created_at,
        "decision": _exposure_decision(assessment),
        "category": assessment.category.value if assessment.category else None,
        "child_user_id": assessment.child_user_id,
        "child_name": child_name or assessment.child_user_id,
        "chat_id": assessment.chat_id,
        "chat_name": assessment.chat_name or (state.chat_name if state else None) or assessment.chat_id,
        "chat_type": assessment.chat_type,
        "message_count": len(assessment.input_message_ids) or None,
        "summary": assessment.summary,
        "reason": assessment.reason,
        "evidence_message_ids": list(assessment.evidence_message_ids),
        "delivery": {"status": "shadow", "label": "Shadow mode · No parent notification"},
        "context_available": False,
    }


async def _exposure_detail(assessment: ExposureAssessment) -> dict[str, Any]:
    return {
        "id": assessment.id,
        "analyzer": "exposure",
        "decision": _exposure_decision(assessment),
        "category": assessment.category.value if assessment.category else None,
        "child_user_id": assessment.child_user_id,
        "chat_id": assessment.chat_id,
        "chat_name": assessment.chat_name,
        "chat_type": assessment.chat_type,
        "created_at": assessment.created_at,
        "summary": assessment.summary,
        "reason": assessment.reason,
        "evidence_message_ids": list(assessment.evidence_message_ids),
        "evidence_messages": [],
        "context_message_count": len(assessment.input_message_ids),
        "context_available": False,
        "delivery": {"status": "shadow", "label": "Shadow mode · No parent notification"},
    }


def _exposure_decision(assessment: ExposureAssessment) -> str:
    if assessment.significant_exposure is True:
        return "yes"
    if assessment.significant_exposure is False:
        return "no"
    return "unknown"


def _activity_view(
    assessment: SafetyAssessment,
    outcome_list: list[ParentAttentionDeliveryOutcome] | None,
    state: GuardChatState | None,
    child_name: str | None,
) -> dict[str, Any]:
    deliveries = [
        _delivery_view(outcome, None) for outcome in (outcome_list or [])
    ]
    # Pick the most informative outcome for the aggregate delivery field.
    primary = _pick_primary_outcome(outcome_list or [])
    return {
        "id": assessment.id,
        "analyzer": "child",
        "created_at": assessment.created_at,
        "decision": _decision(assessment),
        "child_user_id": assessment.child_user_id,
        "child_name": assessment.child_name or child_name or assessment.child_user_id,
        "chat_id": assessment.chat_id,
        "chat_name": assessment.chat_name or (state.chat_name if state else None) or assessment.chat_id,
        "chat_type": assessment.chat_type,
        "message_count": len(assessment.input_message_ids) or None,
        "summary": assessment.summary,
        "delivery": _delivery_view(primary, None),
        "deliveries": deliveries,
    }


def _pick_primary_outcome(
    outcome_list: list[ParentAttentionDeliveryOutcome],
) -> ParentAttentionDeliveryOutcome | None:
    """Pick the most informative outcome for the aggregate delivery field."""
    if not outcome_list:
        return None
    # Prefer non-placeholder outcomes (guardian_user_id != "").
    real = [o for o in outcome_list if o.guardian_user_id]
    if real:
        # Prefer sent > scheduled > others.
        for status in (
            ParentAttentionOutcomeStatus.SENT,
            ParentAttentionOutcomeStatus.SCHEDULED,
            ParentAttentionOutcomeStatus.FAILED,
            ParentAttentionOutcomeStatus.INDETERMINATE,
            ParentAttentionOutcomeStatus.NOT_SENT,
        ):
            for outcome in real:
                if outcome.status is status:
                    return outcome
        return real[0]
    return outcome_list[0]


def _delivery_view(
    outcome: ParentAttentionDeliveryOutcome | None,
    action: ScheduledAction | None,
) -> dict[str, Any]:
    if outcome is None:
        return {
            "status": "unknown",
            "label": "Unknown (historical)",
            "reason": "historical_outcome_unavailable",
        }
    status = outcome.status.value
    if outcome.status is ParentAttentionOutcomeStatus.NOT_SENT:
        if outcome.reason == "pending_evaluation":
            label = "Pending evaluation"
        elif outcome.reason == "already_notified":
            label = "Skipped · already notified"
        elif outcome.reason == "not_parent_attention":
            label = "Not applicable"
        else:
            label = "Skipped"
    elif outcome.status is ParentAttentionOutcomeStatus.SCHEDULED:
        label = "Scheduled"
    elif outcome.status is ParentAttentionOutcomeStatus.SENT:
        label = "Sent"
    elif outcome.status is ParentAttentionOutcomeStatus.INDETERMINATE:
        label = "Unconfirmed"
    else:
        label = "Failed"
    return {
        "status": status,
        "label": label,
        "reason": outcome.reason,
        "scheduled_action_id": outcome.scheduled_action_id,
        "provider_message_id": outcome.provider_message_id,
        "scheduled_at": outcome.scheduled_at,
        "sent_at": outcome.sent_at,
        "failed_at": outcome.failed_at,
        "action_status": action.status.value if action is not None else None,
        "action_error": action.error if action is not None else None,
    }


def _queue_view(state: GuardChatState | None) -> dict[str, Any] | None:
    if state is None:
        return None
    return {
        "activity_version": state.activity_version,
        "last_analyzed_version": state.last_analyzed_version,
        "pending": state.next_analysis_at is not None,
        "next_analysis_at": state.next_analysis_at,
        "last_analysis_at": state.last_analysis_at,
    }


def _decision(assessment: SafetyAssessment) -> str:
    if assessment.parent_should_know is True:
        return "yes"
    if assessment.parent_should_know is False:
        return "no"
    return "unknown"


def _message_view(message: Message, *, cited: bool) -> dict[str, Any]:
    return {
        "id": message.id,
        "direction": message.direction.value,
        "sender": message.sender_name or message.sender_id or "Unknown",
        "text": message.text or "",
        "timestamp": message.timestamp,
        "cited": cited,
    }


def _normalize_decision(value: str | None) -> str | None:
    if value in {None, "", "yes", "no", "unknown"}:
        return value or None
    return "invalid"


def _encode_cursor(created_at: datetime, assessment_id: str) -> str:
    raw = json.dumps([created_at.isoformat(), assessment_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, str] | None:
    try:
        padded = value + "=" * (-len(value) % 4)
        created_at, assessment_id = json.loads(base64.urlsafe_b64decode(padded))
        parsed = datetime.fromisoformat(created_at)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed, str(assessment_id)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _message_hash(messages: list[Message]) -> str:
    payload = [
        {
            "id": message.id,
            "text": message.text or "",
            "timestamp": message.timestamp.isoformat() if message.timestamp else None,
        }
        for message in messages
    ]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = ["GuardActivityResponse", "GuardSimpleTelemetryService"]
