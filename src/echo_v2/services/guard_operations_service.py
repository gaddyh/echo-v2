"""Authorized, human-readable read model for Guard operations."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any

from echo_v2.domain.chat import Message
from echo_v2.domain.guard import GuardAnalysisRecord, GuardChatState
from echo_v2.domain.guard_alert import (
    GuardAlert,
    GuardAlertEvaluation,
    GuardAlertEvaluationOutcome,
)
from echo_v2.domain.guard_operations import GuardAnalysisAttempt
from echo_v2.persistence.chat_repositories import MessageRepository
from echo_v2.persistence.guard_operations import GuardAnalysisTelemetryRepository
from echo_v2.persistence.guard_repositories import (
    GuardAlertRepository,
    GuardAnalysisRepository,
    GuardChatStateRepository,
    GuardianChildLinkRepository,
)
from echo_v2.persistence.scheduled_actions import ScheduledActionRepository
from echo_v2.services.waiting_list_token_service import WaitingListTokenService

_DEFAULT_HISTORY_RECORDS = 500
_MAX_HISTORY_RECORDS = 5000


@dataclass(frozen=True)
class GuardOperationsResponse:
    guardian_user_id: str
    generated_at: datetime
    summary: dict[str, int]
    chats: tuple[dict[str, Any], ...]


class GuardOperationsService:
    def __init__(
        self,
        *,
        token_service: WaitingListTokenService,
        links: GuardianChildLinkRepository,
        chat_states: GuardChatStateRepository,
        analyses: GuardAnalysisRepository,
        alerts: GuardAlertRepository,
        telemetry: GuardAnalysisTelemetryRepository,
        actions: ScheduledActionRepository,
        messages: MessageRepository,
    ) -> None:
        self._tokens = token_service
        self._links = links
        self._chat_states = chat_states
        self._analyses = analyses
        self._alerts = alerts
        self._telemetry = telemetry
        self._actions = actions
        self._messages = messages

    async def list_dashboard(
        self,
        *,
        session_id: str,
        child_user_id: str | None = None,
        chat_id: str | None = None,
        decision: str | None = None,
        category: str | None = None,
        mode: str | None = None,
        overdue: bool | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> GuardOperationsResponse | None:
        resolved = await self._tokens.resolve_session(session_id)
        if resolved is None:
            return None
        links = await self._links.list_active_for_guardian(resolved.user_id)
        child_ids = [link.child_user_id for link in links]
        if child_user_id is not None and child_user_id not in child_ids:
            return GuardOperationsResponse(
                guardian_user_id=resolved.user_id,
                generated_at=datetime.now(timezone.utc),
                summary=_summary([]),
                chats=(),
            )
        states = await self._chat_states.list_for_children(child_ids)
        now = datetime.now(timezone.utc)
        chats: list[dict[str, Any]] = []
        for state in states:
            if child_user_id is not None and state.child_user_id != child_user_id:
                continue
            if chat_id is not None and state.chat_id != chat_id:
                continue
            chat = await self._build_chat(
                state,
                now,
                history_limit=(
                    _MAX_HISTORY_RECORDS if chat_id is not None else _DEFAULT_HISTORY_RECORDS
                ),
            )
            if not self._matches(
                chat,
                decision=decision,
                category=category,
                mode=mode,
                overdue=overdue,
                date_from=date_from,
                date_to=date_to,
            ):
                continue
            chats.append(chat)
        chats.sort(
            key=lambda chat: chat["last_message_at"] or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        page = chats[max(offset, 0): max(offset, 0) + max(min(limit, 200), 1)]
        return GuardOperationsResponse(
            guardian_user_id=resolved.user_id,
            generated_at=now,
            summary=_summary(chats),
            chats=tuple(page),
        )

    async def _build_chat(
        self, state: GuardChatState, now: datetime, *, history_limit: int
    ) -> dict[str, Any]:
        records = await self._analyses.list_for_chat(
            child_user_id=state.child_user_id,
            chat_id=state.chat_id,
            limit=history_limit,
        )
        attempts = await self._telemetry.list_for_chat(
            child_user_id=state.child_user_id,
            chat_id=state.chat_id,
            limit=500,
        )
        evaluations = await self._alerts.list_evaluations_for_chat(
            child_user_id=state.child_user_id,
            chat_id=state.chat_id,
            limit=500,
        )
        threshold_states = await self._alerts.list_threshold_states(
            child_user_id=state.child_user_id,
            chat_id=state.chat_id,
        )
        alerts = await self._alerts.list_alerts_for_chat(
            child_user_id=state.child_user_id,
            chat_id=state.chat_id,
            limit=100,
        )
        alerts_by_id = {alert.id: alert for alert in alerts}
        evaluations_by_result = {item.analysis_result_id: item for item in evaluations}
        actions_by_id = {}
        for alert in alerts:
            action = await self._actions.get(alert.scheduled_action_id)
            if action is not None:
                actions_by_id[alert.scheduled_action_id] = action
        evidence_message_ids = {
            message_id
            for record in records
            for message_id in record.evidence_message_ids
        }
        messages = await self._messages.list_by_ids(
            user_id=state.child_user_id,
            chat_id=state.chat_id,
            message_ids=evidence_message_ids,
        )
        message_by_id = {message.id: message for message in messages}
        timeline: list[dict[str, Any]] = []
        if state.pending_since is not None or state.last_message_at is not None:
            timeline.append(
                {
                    "kind": "queue",
                    "at": state.pending_since or state.last_message_at,
                    "status": "pending" if state.next_analysis_at else "idle",
                    "title": "Queued for Guard analysis",
                    "detail": state.next_analysis_reason or "Message activity observed",
                    "target_version": state.activity_version,
                }
            )
        for attempt in attempts:
            timeline.append(_attempt_event(attempt))
        analysis_views: list[dict[str, Any]] = []
        for record in records:
            evaluation = evaluations_by_result.get(record.id or "")
            alert_for_record = (
                alerts_by_id.get(evaluation.alert_id)
                if evaluation is not None and evaluation.alert_id is not None
                else None
            )
            action = (
                actions_by_id.get(alert_for_record.scheduled_action_id)
                if alert_for_record
                else None
            )
            analysis = _analysis_view(
                record, evaluation, alert_for_record, action, message_by_id
            )
            analysis_views.append(analysis)
            timeline.append(
                {
                    "kind": "analysis",
                    "at": record.created_at,
                    "status": record.decision.value,
                    "title": f"Analyzer decided {record.decision.value}",
                    "detail": record.reason,
                    "target_version": record.target_version,
                    "result_id": record.id,
                }
            )
            if evaluation is not None:
                timeline.append(_policy_event(evaluation, alert_for_record))
            if alert_for_record is not None:
                timeline.append(
                    {
                        "kind": "alert",
                        "at": alert_for_record.created_at,
                        "status": alert_for_record.status.value,
                        "title": f"{alert_for_record.kind.value.title()} alert created",
                        "detail": alert_for_record.policy_reason,
                        "target_version": record.target_version,
                        "alert_id": alert_for_record.id,
                    }
                )
                if action is not None:
                    timeline.append(
                        {
                            "kind": "delivery",
                            "at": action.executed_at or action.created_at,
                            "status": action.status.value,
                            "title": f"Delivery {action.status.value.replace('_', ' ')}",
                            "detail": action.error,
                            "target_version": record.target_version,
                            "alert_id": alert_for_record.id,
                            "action_id": action.id,
                        }
                    )
        timeline.sort(key=_timeline_sort_key)
        latest_record_id = records[-1].id if records else None
        latest_evaluation = evaluations_by_result.get(latest_record_id or "")
        latest_alert = (
            alerts_by_id.get(latest_evaluation.alert_id)
            if latest_evaluation is not None and latest_evaluation.alert_id is not None
            else None
        )
        latest_action = (
            actions_by_id.get(latest_alert.scheduled_action_id) if latest_alert else None
        )
        backlog = max(state.activity_version - state.last_analyzed_version, 0)
        is_overdue = bool(
            state.next_analysis_at is not None
            and state.next_analysis_at <= now
            and backlog > 0
        )
        latest_view = analysis_views[-1] if analysis_views else None
        decision_groups = self._decision_groups(analysis_views)
        return {
            "child_user_id": state.child_user_id,
            "chat_id": state.chat_id,
            "chat_name": state.chat_name or state.chat_id,
            "mode": "group" if state.is_group else "private",
            "is_group": state.is_group,
            "last_message_at": state.last_message_at,
            "activity_version": state.activity_version,
            "last_analyzed_version": state.last_analyzed_version,
            "last_decision": state.last_decision.value,
            "last_analysis_at": state.last_analysis_at,
            "pending_since": state.pending_since,
            "next_analysis_at": state.next_analysis_at,
            "next_analysis_reason": state.next_analysis_reason,
            "backlog": backlog,
            "overdue": is_overdue,
            "analysis_count": len(records),
            "analysis_history_complete": len(records) < _MAX_HISTORY_RECORDS,
            "attempt_count": len(attempts),
            "latest_attempt": asdict(attempts[-1]) if attempts else None,
            "latest_analysis": latest_view,
            "decision_groups": decision_groups,
            "latest_policy": _policy_view(latest_evaluation, latest_alert),
            "threshold_states": [asdict(state_item) for state_item in threshold_states],
            "latest_delivery": _delivery_view(latest_alert, latest_action),
            "participant_diagnostics": (
                latest_view.get("participant_diagnostics", []) if latest_view else []
            ),
            "timeline": timeline,
            "analyses": analysis_views,
        }


    @staticmethod
    def _decision_groups(analyses: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: list[dict[str, Any]] = []
        for analysis in analyses:
            decision = str(analysis["decision"])
            if not groups or groups[-1]["decision"] != decision:
                groups.append(
                    {
                        "decision": decision,
                        "start_version": analysis["target_version"],
                        "end_version": analysis["target_version"],
                        "start_at": analysis["created_at"],
                        "end_at": analysis["created_at"],
                        "analysis_count": 1,
                        "analysis_ids": [analysis["id"]],
                        "signals": list(analysis["signals"]),
                        "categories": list(analysis["categories"]),
                        "first_reason": analysis["reason"],
                        "latest_reason": analysis["reason"],
                        "evidence_analysis_count": (
                            1 if analysis["evidence_message_ids"] else 0
                        ),
                    }
                )
                continue
            group = groups[-1]
            group["end_version"] = analysis["target_version"]
            group["end_at"] = analysis["created_at"]
            group["analysis_count"] += 1
            group["analysis_ids"].append(analysis["id"])
            group["latest_reason"] = analysis["reason"]
            group["evidence_analysis_count"] += (
                1 if analysis["evidence_message_ids"] else 0
            )
            for field in ("signals", "categories"):
                group[field] = list(dict.fromkeys([*group[field], *analysis[field]]))
        return groups

    @staticmethod
    def _matches(
        chat: dict[str, Any], *, decision: str | None, category: str | None,
        mode: str | None, overdue: bool | None, date_from: date | None, date_to: date | None,
    ) -> bool:
        latest = chat.get("latest_analysis") or {}
        if decision and latest.get("decision") != decision:
            return False
        if category and category not in latest.get("categories", []):
            return False
        if mode and chat.get("mode") != mode:
            return False
        if overdue is not None and chat.get("overdue") is not overdue:
            return False
        latest_at = latest.get("created_at") or chat.get("last_message_at")
        if date_from and (latest_at is None or latest_at.date() < date_from):
            return False
        return not (date_to and (latest_at is None or latest_at.date() > date_to))


def _analysis_view(
    record: GuardAnalysisRecord,
    evaluation: GuardAlertEvaluation | None,
    alert: GuardAlert | None,
    action: Any,
    messages: dict[str, Message],
) -> dict[str, Any]:
    diagnostics = record.diagnostics or {}
    participant_diagnostics = diagnostics.get("participant_roles", [])
    if not isinstance(participant_diagnostics, list):
        participant_diagnostics = []
    participant_impact = diagnostics.get("participant_impact", [])
    if not isinstance(participant_impact, list):
        participant_impact = []
    return {
        "id": record.id,
        "created_at": record.created_at,
        "target_version": record.target_version,
        "decision": record.decision.value,
        "categories": list(record.categories),
        "signals": list(record.signals),
        "cumulative_signals": diagnostics.get("cumulative_signals", list(record.signals)),
        "cumulative_categories": diagnostics.get("cumulative_categories", list(record.categories)),
        "confidence": record.confidence,
        "reason": record.reason,
        "evidence_message_ids": list(record.evidence_message_ids),
        "evidence_messages": [
            _message_view(messages[message_id])
            for message_id in record.evidence_message_ids
            if message_id in messages
        ],
        "participant_diagnostics": participant_diagnostics,
        "participant_impact": participant_impact,
        "model": record.model,
        "prompt_version": record.prompt_version,
        "analyzer_version": record.analyzer_version,
        "taxonomy_version": record.taxonomy_version,
        "schedule_reason": record.schedule_reason,
        "pending_since": record.pending_since,
        "scheduled_for": record.scheduled_for,
        "policy": _policy_view(evaluation, alert),
        "delivery": _delivery_view(alert, action),
        "gold_scenario_id": diagnostics.get("gold_scenario_id"),
    }


def _message_view(message: Message) -> dict[str, Any]:
    return {
        "id": message.id,
        "direction": message.direction.value,
        "sender": message.sender_name or message.sender_id,
        "text": message.text or "",
        "timestamp": message.timestamp,
    }


def _policy_view(
    evaluation: GuardAlertEvaluation | None, alert: GuardAlert | None
) -> dict[str, Any] | None:
    if evaluation is None:
        return None
    if evaluation.outcome is GuardAlertEvaluationOutcome.NO_ACTION:
        reason = "not_actionable"
        detail = "The analysis did not reach an alert threshold."
    elif evaluation.outcome is GuardAlertEvaluationOutcome.SUPPRESSED:
        reason = "policy_suppressed"
        detail = "The analysis was actionable, but policy or episode deduplication suppressed a new alert."
    else:
        reason = "scheduled"
        detail = "Alert policy scheduled a parent notification."
    return {
        "outcome": evaluation.outcome.value,
        "reason": reason,
        "detail": detail,
        "evaluated_at": evaluation.evaluated_at,
        "policy_version": evaluation.policy_version,
        "alert_id": alert.id if alert else evaluation.alert_id,
        "threshold_family": alert.threshold_family if alert else None,
    }


def _delivery_view(alert: GuardAlert | None, action: Any) -> dict[str, Any] | None:
    if alert is None:
        return None
    action_status = action.status.value if action is not None else None
    suppressed = alert.status.value in {"suppressed", "expired"} or action_status == "cancelled"
    return {
        "alert_id": alert.id,
        "kind": alert.kind.value,
        "alert_status": alert.status.value,
        "action_id": alert.scheduled_action_id,
        "action_status": action_status,
        "reason": "delivery_suppressed" if suppressed else "scheduled",
        "executed_at": action.executed_at if action is not None else None,
        "created_at": action.created_at if action is not None else None,
        "error": action.error if action is not None else None,
        "result_present": bool(action is not None and action.result is not None),
    }


def _attempt_event(attempt: GuardAnalysisAttempt) -> dict[str, Any]:
    return {
        "kind": "analyzer_attempt",
        "at": attempt.finished_at or attempt.started_at,
        "status": attempt.status.value,
        "title": f"Analyzer attempt {attempt.status.value}",
        "detail": attempt.error_code,
        "target_version": attempt.target_version,
        "attempt_id": attempt.id,
        "started_at": attempt.started_at,
        "finished_at": attempt.finished_at,
        "result_id": attempt.result_id,
    }


def _policy_event(
    evaluation: GuardAlertEvaluation, alert: GuardAlert | None
) -> dict[str, Any]:
    view = _policy_view(evaluation, alert) or {}
    return {
        "kind": "policy",
        "at": evaluation.evaluated_at,
        "status": evaluation.outcome.value,
        "title": f"Alert policy: {view.get('reason', evaluation.outcome.value)}",
        "detail": view.get("detail"),
        "target_version": evaluation.target_version,
        "result_id": evaluation.analysis_result_id,
    }


def _timeline_sort_key(event: dict[str, Any]) -> tuple[datetime, int, int]:
    at = event.get("at") or datetime.min.replace(tzinfo=timezone.utc)
    priority = {
        "queue": 0,
        "analyzer_attempt": 1,
        "analysis": 2,
        "policy": 3,
        "alert": 4,
        "delivery": 5,
    }
    return at, int(event.get("target_version") or 0), priority.get(str(event.get("kind")), 9)


def _summary(chats: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "monitored_chats": len(chats),
        "group_chats": sum(chat["is_group"] for chat in chats),
        "private_chats": sum(not chat["is_group"] for chat in chats),
        "analyzed_chats": sum(chat["analysis_count"] > 0 for chat in chats),
        "pending_chats": sum(chat["backlog"] > 0 for chat in chats),
        "overdue_chats": sum(chat["overdue"] for chat in chats),
        "analyzer_failures": sum(
            sum(event.get("status") == "failed" for event in chat["timeline"] if event["kind"] == "analyzer_attempt")
            for chat in chats
        ),
        "alerts": sum(1 for chat in chats for event in chat["timeline"] if event["kind"] == "alert"),
        "delivery_failures": sum(
            chat.get("latest_delivery", {}).get("action_status") in {"failed", "indeterminate"}
            if chat.get("latest_delivery") else False
            for chat in chats
        ),
    }


__all__ = ["GuardOperationsResponse", "GuardOperationsService"]
