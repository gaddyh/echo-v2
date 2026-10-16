"""Read-only Baseline B telemetry for the Guard Activity page."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from echo_v2.domain.chat import Message
from echo_v2.domain.guard import GuardChatState
from echo_v2.domain.guard_simple import SafetyAssessment
from echo_v2.domain.parent_attention import (
    ParentAttentionDeliveryOutcome,
    ParentAttentionOutcomeStatus,
)
from echo_v2.domain.scheduling import ScheduledAction
from echo_v2.persistence.chat_repositories import MessageRepository
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
    ) -> None:
        self._tokens = token_service
        self._links = links
        self._users = users
        self._chat_states = chat_states
        self._assessments = assessments
        self._outcomes = outcomes
        self._actions = actions
        self._messages = messages

    async def list_activity(
        self,
        *,
        session_id: str,
        child_user_id: str | None = None,
        chat_id: str | None = None,
        decision: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = _DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> GuardActivityResponse | None:
        resolved = await self._tokens.resolve_session(session_id)
        if resolved is None:
            return None
        links = await self._links.list_active_for_guardian(resolved.user_id)
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
        rows = await self._assessments.list_for_children(
            child_user_ids=child_ids,
            created_after=start,
            created_before=end,
            chat_id=chat_id,
            decision=normalized_decision,
            limit=page_size + 1,
            before=before,
        )
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        outcome_rows = await self._outcomes.list_for_assessments(
            [assessment.id for assessment in rows if assessment.id is not None]
        )
        outcomes = {outcome.assessment_id: outcome for outcome in outcome_rows}
        states = await self._chat_states.list_for_children(child_ids)
        state_by_chat = {(state.child_user_id, state.chat_id): state for state in states}
        child_names = await self._child_names(child_ids)
        analyses = tuple(
            _activity_view(
                assessment,
                outcomes.get(assessment.id or ""),
                state_by_chat.get((assessment.child_user_id, assessment.chat_id)),
                child_names.get(assessment.child_user_id),
            )
            for assessment in rows
        )
        counts = await self._assessments.count_for_children(
            child_user_ids=child_ids,
            created_after=start,
            created_before=end,
            chat_id=chat_id,
            decision=normalized_decision,
        )
        counts["notifications"] = await self._outcomes.count_sent(
            child_user_ids=child_ids,
            created_after=start,
            created_before=end,
            chat_id=chat_id,
        )
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
                {"id": child_id, "name": child_names.get(child_id, child_id)}
                for child_id in child_ids
            ),
            analyses=analyses,
            next_cursor=next_cursor,
        )

    async def get_analysis(
        self, *, session_id: str, assessment_id: str
    ) -> dict[str, Any] | None:
        authorized = await self._authorized_child_ids(session_id)
        if authorized is None:
            return None
        assessment = await self._assessments.get(assessment_id)
        if assessment is None or assessment.child_user_id not in authorized:
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
        outcome = await self._outcomes.get(assessment_id)
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
        self, *, session_id: str, assessment_id: str
    ) -> dict[str, Any] | None:
        authorized = await self._authorized_child_ids(session_id)
        if authorized is None:
            return None
        assessment = await self._assessments.get(assessment_id)
        if assessment is None or assessment.child_user_id not in authorized:
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

    async def _authorized_child_ids(self, session_id: str) -> set[str] | None:
        resolved = await self._tokens.resolve_session(session_id)
        if resolved is None:
            return None
        links = await self._links.list_active_for_guardian(resolved.user_id)
        return {link.child_user_id for link in links}

    async def _child_names(self, child_ids: list[str]) -> dict[str, str]:
        names: dict[str, str] = {}
        for child_id in child_ids:
            info = await self._users.get_user_info_by_id(child_id)
            names[child_id] = (info[1] if info and info[1] else child_id)
        return names

    async def _action_for(
        self, outcome: ParentAttentionDeliveryOutcome | None
    ) -> ScheduledAction | None:
        if outcome is None or outcome.scheduled_action_id is None:
            return None
        return await self._actions.get(outcome.scheduled_action_id)


def _activity_view(
    assessment: SafetyAssessment,
    outcome: ParentAttentionDeliveryOutcome | None,
    state: GuardChatState | None,
    child_name: str | None,
) -> dict[str, Any]:
    return {
        "id": assessment.id,
        "created_at": assessment.created_at,
        "decision": _decision(assessment),
        "child_user_id": assessment.child_user_id,
        "child_name": assessment.child_name or child_name or assessment.child_user_id,
        "chat_id": assessment.chat_id,
        "chat_name": assessment.chat_name or (state.chat_name if state else None) or assessment.chat_id,
        "chat_type": assessment.chat_type,
        "message_count": len(assessment.input_message_ids) or None,
        "summary": assessment.summary,
        "delivery": _delivery_view(outcome, None),
    }


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
