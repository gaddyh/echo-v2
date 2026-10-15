from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.domain.chat import Message
from echo_v2.domain.guard import GuardAnalysisRecord
from echo_v2.domain.guard_alert import (
    GuardAlert,
    GuardAlertEvaluation,
    GuardAlertEvaluationOutcome,
    GuardAlertKind,
    GuardAlertStatus,
)
from echo_v2.domain.guard_operations import (
    GuardAnalysisAttempt,
    GuardAnalysisAttemptStatus,
)
from echo_v2.domain.scheduling import (
    ScheduledAction,
    ScheduledActionStatus,
    ScheduledActionType,
)
from echo_v2.persistence.chat_repositories import InMemoryMessageRepository
from echo_v2.persistence.guard_alert_repositories import InMemoryGuardAlertRepository
from echo_v2.persistence.guard_operations import (
    InMemoryGuardAnalysisTelemetryRepository,
)
from echo_v2.persistence.guard_repositories import (
    InMemoryGuardAnalysisRepository,
    InMemoryGuardChatStateRepository,
    InMemoryGuardianChildLinkRepository,
)
from echo_v2.persistence.scheduled_actions import InMemoryScheduledActionRepository
from echo_v2.persistence.waiting_list_tokens import InMemoryWaitingListSessionRepository
from echo_v2.ports.whatsapp import MessageDirection
from echo_v2.services.guard_operations_service import (
    GuardOperationsService,
    _analysis_view,
    _attempt_event,
    _delivery_view,
    _policy_event,
    _policy_view,
    _timeline_sort_key,
)
from echo_v2.services.guard_taxonomy import GuardDecision
from echo_v2.services.waiting_list_token_service import WaitingListTokenService

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


def record(version: int, chat_id: str, decision: GuardDecision) -> GuardAnalysisRecord:
    return GuardAnalysisRecord(
        id=f"result-{chat_id}-{version}",
        child_user_id="child",
        connection_id="connection",
        chat_id=chat_id,
        target_version=version,
        signals=("repeated_targeting",) if decision != GuardDecision.NONE else (),
        categories=("bullying",) if decision != GuardDecision.NONE else (),
        confidence=0.82,
        reason="A repeated pattern was detected.",
        evidence_message_ids=(f"message-{chat_id}-{version}",) if decision != GuardDecision.NONE else (),
        decision=decision,
        model="test-model",
        prompt_version="prompt-v1",
        analyzer_version="analyzer-v1",
        taxonomy_version="taxonomy-v1",
        created_at=NOW + timedelta(minutes=version),
        pending_since=NOW,
        scheduled_for=NOW + timedelta(seconds=30),
        diagnostics={
            "participant_roles": [
                {"canonical_id": "sender", "role": "other", "display_name": "Other"}
            ],
            "cumulative_signals": ["repeated_targeting"],
            "cumulative_categories": ["bullying"],
            "participant_impact": [{"actor": "other", "target": "child"}],
        },
    )


async def setup() -> tuple[GuardOperationsService, str, InMemoryGuardAlertRepository]:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    session_id, _ = await tokens.issue(user_id="guardian")
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian", child_user_id="child", now=NOW)
    states = InMemoryGuardChatStateRepository()
    await states.upsert_on_message(child_user_id="child", chat_id="class@g.us", observed_at=NOW, next_analysis_at=NOW + timedelta(minutes=1), pending_since=NOW, chat_name="Class group", is_group=True)
    await states.upsert_on_message(child_user_id="child", chat_id="friend@c.us", observed_at=NOW, next_analysis_at=NOW + timedelta(minutes=1), pending_since=NOW, chat_name="Friend", is_group=False)
    analyses = InMemoryGuardAnalysisRepository()
    group_record = record(1, "class@g.us", GuardDecision.CONCERNING)
    await analyses.save(group_record)
    alerts = InMemoryGuardAlertRepository()
    await alerts.save_evaluation(GuardAlertEvaluation(analysis_result_id=group_record.id or "", child_user_id="child", chat_id="class@g.us", target_version=1, policy_version="policy-v1", outcome=GuardAlertEvaluationOutcome.NO_ACTION, evaluated_at=NOW + timedelta(minutes=1)))
    telemetry = InMemoryGuardAnalysisTelemetryRepository()
    actions = InMemoryScheduledActionRepository()
    messages = InMemoryMessageRepository()
    await messages.save(Message(id="message-class@g.us-1", user_id="child", connection_id="connection", chat_id="class@g.us", provider_message_id="provider-1", direction=MessageDirection.INBOUND, sender_id="sender", sender_name="Other", timestamp=NOW, text="<evidence>"))
    for index in range(100):
        await messages.save(Message(
            id=f"noise-{index}", user_id="child", connection_id="connection",
            chat_id="class@g.us", provider_message_id=f"noise-provider-{index}",
            direction=MessageDirection.INBOUND, sender_id="sender", timestamp=NOW + timedelta(seconds=index + 1),
            text="Recent noise",
        ))
    return GuardOperationsService(token_service=tokens, links=links, chat_states=states, analyses=analyses, alerts=alerts, telemetry=telemetry, actions=actions, messages=messages), session_id, alerts


async def test_dashboard_includes_unanalysed_group_and_participant_diagnostics() -> None:
    service, session_id, _ = await setup()
    response = await service.list_dashboard(session_id=session_id)
    assert response is not None
    assert response.summary["monitored_chats"] == 2
    group = next(chat for chat in response.chats if chat["is_group"])
    assert group["chat_name"] == "Class group"
    assert group["latest_analysis"]["participant_diagnostics"][0]["role"] == "other"
    assert group["latest_analysis"]["participant_impact"][0]["target"] == "child"
    assert group["latest_analysis"]["evidence_messages"][0]["text"] == "<evidence>"
    private = next(chat for chat in response.chats if not chat["is_group"])
    assert private["analysis_count"] == 0
    assert private["timeline"][0]["kind"] == "queue"
    await service._links.upsert_active(
        guardian_user_id="guardian", child_user_id="child-2", now=NOW
    )
    await service._chat_states.upsert_on_message(
        child_user_id="child-2", chat_id="other@c.us", observed_at=NOW,
        next_analysis_at=NOW + timedelta(minutes=1), pending_since=NOW,
        chat_name="Other", is_group=False,
    )
    scoped = await service.list_dashboard(session_id=session_id, child_user_id="child")
    assert scoped is not None
    assert all(chat["child_user_id"] == "child" for chat in scoped.chats)


async def test_dashboard_groups_decisions_and_uses_latest_analysis_for_policy() -> None:
    service, session_id, _ = await setup()
    await service._analyses.save(record(2, "class@g.us", GuardDecision.WATCH))
    await service._analyses.save(record(3, "class@g.us", GuardDecision.CONCERNING))

    response = await service.list_dashboard(session_id=session_id, chat_id="class@g.us")

    assert response is not None
    chat = response.chats[0]
    assert [group["decision"] for group in chat["decision_groups"]] == [
        "concerning", "watch", "concerning"
    ]
    assert [group["analysis_count"] for group in chat["decision_groups"]] == [1, 1, 1]
    assert chat["decision_groups"][0]["evidence_message_count"] == 1
    assert chat["decision_groups"][0]["evidence_messages"][0]["text"] == "<evidence>"
    assert chat["latest_analysis"]["target_version"] == 3
    assert chat["latest_policy"] is None


async def test_dashboard_scopes_children_and_normalizes_not_actionable() -> None:
    service, session_id, _ = await setup()
    assert await service.list_dashboard(session_id="expired") is None
    response = await service.list_dashboard(session_id=session_id, child_user_id="not-linked")
    assert response is not None and response.chats == ()
    response = await service.list_dashboard(session_id=session_id, mode="group", decision="concerning", category="bullying", chat_id="class@g.us", limit=1, offset=0)
    assert response is not None
    assert response.chats[0]["latest_analysis"]["policy"]["reason"] == "not_actionable"
    assert response.chats[0]["latest_delivery"] is None
    assert (await service.list_dashboard(session_id=session_id, decision="urgent")).chats == ()


async def test_dashboard_exposes_delivery_suppression() -> None:
    service, session_id, alerts = await setup()
    alert = GuardAlert(id="alert-1", analysis_result_id="result-class@g.us-1", guardian_user_id="guardian", child_user_id="child", chat_id="class@g.us", kind=GuardAlertKind.NOTIFY, categories=("bullying",), summary="summary", policy_reason="threshold", policy_version="policy-v1", status=GuardAlertStatus.EXPIRED, scheduled_action_id="action-1", logical_event_key="event-1", threshold_family="bullying_established", episode_start_version=1, detected_at=NOW, expires_at=NOW + timedelta(hours=1), created_at=NOW)
    await alerts.save_alert(alert)
    alerts.evaluations["result-class@g.us-1"] = GuardAlertEvaluation(analysis_result_id="result-class@g.us-1", child_user_id="child", chat_id="class@g.us", target_version=1, policy_version="policy-v1", outcome=GuardAlertEvaluationOutcome.SCHEDULED, evaluated_at=NOW, alert_id="alert-1")
    await service._actions.save(ScheduledAction(id="action-1", user_id="guardian", type=ScheduledActionType.SEND_BOT_MESSAGE, execute_at_utc=NOW, timezone="UTC", status=ScheduledActionStatus.CANCELLED, payload={"kind": "guard_alert"}))
    response = await service.list_dashboard(session_id=session_id)
    assert response is not None
    assert response.chats[0]["latest_delivery"]["reason"] == "delivery_suppressed"
    assert response.chats[0]["latest_delivery"]["result_present"] is False
    assert {event["kind"] for event in response.chats[0]["timeline"]} >= {"alert", "delivery"}


async def test_operations_view_helpers_cover_policy_delivery_and_attempt_states() -> None:
    no_action = GuardAlertEvaluation(analysis_result_id="result", child_user_id="child", chat_id="chat", target_version=1, policy_version="p", outcome=GuardAlertEvaluationOutcome.NO_ACTION, evaluated_at=NOW)
    suppressed = replace(no_action, analysis_result_id="result-2", target_version=2, outcome=GuardAlertEvaluationOutcome.SUPPRESSED)
    scheduled = replace(no_action, analysis_result_id="result-3", target_version=3, outcome=GuardAlertEvaluationOutcome.SCHEDULED, alert_id="alert")
    assert _policy_view(None, None) is None
    assert _policy_view(no_action, None)["reason"] == "not_actionable"
    assert _policy_view(suppressed, None)["reason"] == "policy_suppressed"
    assert _policy_event(scheduled, None)["status"] == "scheduled"
    alert = GuardAlert(id="alert", analysis_result_id="result-3", guardian_user_id="guardian", child_user_id="child", chat_id="chat", kind=GuardAlertKind.NOTIFY, categories=(), summary="summary", policy_reason="reason", policy_version="p", status=GuardAlertStatus.SCHEDULED, scheduled_action_id="action", logical_event_key="event", threshold_family="family", episode_start_version=3, detected_at=NOW, expires_at=NOW + timedelta(hours=1), created_at=NOW)
    action = ScheduledAction(id="action", user_id="guardian", type=ScheduledActionType.SEND_BOT_MESSAGE, execute_at_utc=NOW, timezone="UTC", status=ScheduledActionStatus.SUCCEEDED, payload={}, result={"provider_message_id": "provider"}, executed_at=NOW + timedelta(seconds=2))
    delivery = _delivery_view(alert, action)
    assert delivery is not None and delivery["reason"] == "scheduled" and delivery["result_present"] is True
    attempt = GuardAnalysisAttempt(id="attempt", child_user_id="child", chat_id="chat", target_version=1, status=GuardAnalysisAttemptStatus.STARTED, started_at=NOW)
    assert _attempt_event(attempt)["finished_at"] is None


async def test_operations_filter_and_legacy_diagnostics_fallbacks() -> None:
    chat = {"mode": "group", "overdue": False, "last_message_at": NOW, "latest_analysis": {"decision": "watch", "categories": ["bullying"], "created_at": NOW}}
    matches = GuardOperationsService._matches
    assert not matches(chat, decision=None, category="distress", mode=None, overdue=None, date_from=None, date_to=None)
    assert not matches(chat, decision=None, category=None, mode="private", overdue=None, date_from=None, date_to=None)
    assert not matches(chat, decision=None, category=None, mode=None, overdue=True, date_from=None, date_to=None)
    assert not matches(chat, decision=None, category=None, mode=None, overdue=None, date_from=NOW.date() + timedelta(days=1), date_to=None)
    assert not matches(chat, decision=None, category=None, mode=None, overdue=None, date_from=None, date_to=NOW.date() - timedelta(days=1))
    legacy = replace(record(1, "chat@c.us", GuardDecision.WATCH), diagnostics={"participant_roles": "legacy", "participant_impact": "legacy"})
    view = _analysis_view(legacy, None, None, None, {})
    assert view["participant_diagnostics"] == []
    assert view["participant_impact"] == []
    assert _timeline_sort_key({"kind": "unknown", "at": NOW, "target_version": 1})[2] == 9
