from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.domain.chat import Message
from echo_v2.domain.guard_simple import ChildRole, SafetyAssessment
from echo_v2.domain.parent_attention import (
    ParentAttentionDeliveryOutcome,
    ParentAttentionOutcomeStatus,
)
from echo_v2.domain.scheduling import (
    ScheduledAction,
    ScheduledActionStatus,
    ScheduledActionType,
)
from echo_v2.persistence.chat_repositories import InMemoryMessageRepository
from echo_v2.persistence.guard_repositories import (
    InMemoryGuardChatStateRepository,
    InMemoryGuardianChildLinkRepository,
)
from echo_v2.persistence.guard_simple_repositories import (
    InMemoryGuardSimpleAssessmentRepository,
)
from echo_v2.persistence.parent_attention_delivery import (
    InMemoryParentAttentionDeliveryOutcomeRepository,
)
from echo_v2.persistence.scheduled_actions import InMemoryScheduledActionRepository
from echo_v2.persistence.waiting_list_tokens import InMemoryWaitingListSessionRepository
from echo_v2.ports.whatsapp import MessageDirection
from echo_v2.services.guard_simple_telemetry_service import (
    GuardSimpleTelemetryService,
    _decision,
    _decode_cursor,
    _delivery_view,
    _encode_cursor,
    _message_hash,
    _queue_view,
)
from echo_v2.services.waiting_list_token_service import WaitingListTokenService

pytestmark = pytest.mark.asyncio
NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


class FakeUsers:
    async def get_user_info_by_id(self, user_id: str):
        return "+972501234567", "Shay"


def message(message_id: str, text: str) -> Message:
    return Message(
        id=message_id,
        user_id="child",
        connection_id="connection",
        chat_id="class@g.us",
        provider_message_id=f"provider-{message_id}",
        direction=MessageDirection.INBOUND,
        sender_id="sender",
        sender_name="Daniel",
        timestamp=NOW,
        text=text,
    )


def assessment(
    assessment_id: str,
    *,
    decision: bool | None,
    created_at: datetime = NOW,
    input_ids: tuple[str, ...] = (),
    input_hash: str | None = None,
) -> SafetyAssessment:
    return SafetyAssessment(
        id=assessment_id,
        parent_should_know=decision,
        child_role=ChildRole.TARGET if decision is True else ChildRole.UNKNOWN,
        summary="Repeated humiliation." if decision is True else "Ordinary conversation.",
        reason="Repeated humiliation despite requests to stop.",
        evidence_message_ids=input_ids[:1] if decision is True else (),
        input_message_ids=input_ids,
        input_content_hash=input_hash,
        child_user_id="child",
        child_name="Shay",
        connection_id="connection",
        chat_id="class@g.us",
        chat_type="group",
        chat_name="Class group",
        timezone="UTC",
        window_start=created_at - timedelta(hours=1),
        window_end=created_at,
        created_at=created_at,
    )


async def setup():
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    session_id, _ = await tokens.issue(user_id="guardian")
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian", child_user_id="child", now=NOW)
    states = InMemoryGuardChatStateRepository()
    await states.upsert_on_message(
        child_user_id="child",
        chat_id="class@g.us",
        observed_at=NOW,
        next_analysis_at=NOW + timedelta(minutes=1),
        pending_since=NOW,
        chat_name="Class group",
        is_group=True,
    )
    assessments = InMemoryGuardSimpleAssessmentRepository()
    messages = InMemoryMessageRepository()
    first = message("m1", "Repeated mocking")
    second = message("m2", "Please stop")
    await messages.save(first)
    await messages.save(second)
    exact_hash = _message_hash([first, second])
    await assessments.save(
        assessment("a1", decision=True, input_ids=("m1", "m2"), input_hash=exact_hash)
    )
    await assessments.save(assessment("a2", decision=False, created_at=NOW - timedelta(hours=2)))
    await assessments.save(assessment("a3", decision=None, created_at=NOW - timedelta(hours=3)))
    outcomes = InMemoryParentAttentionDeliveryOutcomeRepository()
    await outcomes.save(
        ParentAttentionDeliveryOutcome(
            assessment_id="a1",
            child_user_id="child",
            chat_id="class@g.us",
            status=ParentAttentionOutcomeStatus.SENT,
            reason="provider_confirmed",
            sent_at=NOW,
        )
    )
    service = GuardSimpleTelemetryService(
        token_service=tokens,
        links=links,
        users=FakeUsers(),
        chat_states=states,
        assessments=assessments,
        outcomes=outcomes,
        actions=InMemoryScheduledActionRepository(),
        messages=messages,
    )
    return service, session_id, messages


async def test_activity_lists_recent_rows_and_confirmed_counter() -> None:
    service, session_id, _ = await setup()

    response = await service.list_activity(session_id=session_id)

    assert response is not None
    assert response.summary == {"analyzed": 3, "yes": 1, "no": 1, "unknown": 1, "notifications": 1}
    assert [row["id"] for row in response.analyses] == ["a1", "a2", "a3"]
    assert response.analyses[0]["delivery"]["label"] == "Sent"
    assert response.analyses[1]["delivery"]["label"] == "Unknown (historical)"


async def test_activity_filters_and_cursor_are_scoped_to_linked_children() -> None:
    service, session_id, _ = await setup()

    response = await service.list_activity(
        session_id=session_id,
        decision="unknown",
        limit=1,
    )

    assert response is not None
    assert response.summary["unknown"] == 1
    assert len(response.analyses) == 1
    assert response.next_cursor is None
    unauthorized = await service.list_activity(session_id=session_id, child_user_id="other")
    assert unauthorized is not None and unauthorized.analyses == ()


async def test_activity_keeps_all_child_options_when_one_child_is_selected() -> None:
    service, session_id, _ = await setup()
    await service._links.upsert_active(
        guardian_user_id="guardian", child_user_id="child-2", now=NOW
    )
    second = replace(
        assessment("a2", decision=False),
        child_user_id="child-2",
        child_name="Noa",
    )
    await service._assessments.save(second)

    selected = await service.list_activity(session_id=session_id, child_user_id="child")
    switched = await service.list_activity(session_id=session_id, child_user_id="child-2")

    assert selected is not None and switched is not None
    assert [child["id"] for child in selected.children] == ["child", "child-2"]
    assert [child["id"] for child in switched.children] == ["child", "child-2"]
    assert {row["child_user_id"] for row in selected.analyses} == {"child"}
    assert {row["child_user_id"] for row in switched.analyses} == {"child-2"}


async def test_detail_and_context_report_exact_verified_content() -> None:
    service, session_id, _ = await setup()

    detail = await service.get_analysis(session_id=session_id, assessment_id="a1")
    context = await service.get_context(session_id=session_id, assessment_id="a1")

    assert detail is not None and detail["decision"] == "yes"
    assert detail["evidence_messages"][0]["cited"] is True
    assert context is not None
    assert context["trust"] == "exact message selection, verified content"
    assert [item["id"] for item in context["messages"]] == ["m1", "m2"]


async def test_activity_cursor_and_invalid_or_expired_queries() -> None:
    service, session_id, _ = await setup()
    response = await service.list_activity(session_id=session_id, limit=1)
    assert response is not None and response.next_cursor is not None
    next_page = await service.list_activity(session_id=session_id, limit=1, cursor=response.next_cursor)
    assert next_page is not None and next_page.analyses[0]["id"] == "a2"
    assert await service.list_activity(session_id=session_id, decision="bad") is not None
    assert await service.list_activity(session_id="expired") is None
    equal_bounds = await service.list_activity(session_id=session_id, date_from=NOW, date_to=NOW)
    assert equal_bounds is not None and equal_bounds.analyses[0]["id"] == "a1"
    assert await service.get_analysis(session_id="expired", assessment_id="a1") is None
    assert await service.get_context(session_id="expired", assessment_id="a1") is None


async def test_legacy_context_is_best_effort_and_missing_assessment_is_hidden() -> None:
    service, session_id, _ = await setup()

    context = await service.get_context(session_id=session_id, assessment_id="a2")
    missing = await service.get_analysis(session_id=session_id, assessment_id="not-found")

    assert context is not None
    assert context["trust"] == "best-effort reconstruction, historical"
    assert missing is None
    other = replace(assessment("other", decision=True), child_user_id="other")
    await service._assessments.save(other)
    assert await service.get_context(session_id=session_id, assessment_id="other") is None

    historical = assessment("legacy", decision=None, input_ids=(), input_hash=None)
    historical = replace(historical, window_start=None, window_end=None)
    await service._assessments.save(historical)
    empty_context = await service.get_context(session_id=session_id, assessment_id="legacy")
    assert empty_context is not None and empty_context["messages"] == []


async def test_context_detects_changed_and_missing_messages() -> None:
    service, session_id, messages = await setup()
    await messages.update_text("m1", "Edited text")
    changed = await service.get_context(session_id=session_id, assessment_id="a1")
    assert changed is not None
    assert changed["trust"] == "exact message selection, content changed"
    messages._messages.pop(("connection", "provider-m2"))
    missing = await service.get_context(session_id=session_id, assessment_id="a1")
    assert missing is not None and missing["missing_message_ids"] == ["m2"]


async def test_delivery_labels_and_helper_projections() -> None:
    action = ScheduledAction(
        id="action",
        user_id="guardian",
        type=ScheduledActionType.SEND_BOT_MESSAGE,
        execute_at_utc=NOW,
        timezone="UTC",
        status=ScheduledActionStatus.SUCCEEDED,
        payload={},
        error=None,
    )
    for status, reason, label in (
        (ParentAttentionOutcomeStatus.NOT_SENT, "pending_evaluation", "Pending evaluation"),
        (ParentAttentionOutcomeStatus.NOT_SENT, "already_notified", "Skipped · already notified"),
        (ParentAttentionOutcomeStatus.NOT_SENT, "not_parent_attention", "Not applicable"),
        (ParentAttentionOutcomeStatus.NOT_SENT, "disabled", "Skipped"),
        (ParentAttentionOutcomeStatus.SCHEDULED, "scheduled", "Scheduled"),
        (ParentAttentionOutcomeStatus.SENT, "provider_confirmed", "Sent"),
        (ParentAttentionOutcomeStatus.FAILED, "failed", "Failed"),
        (ParentAttentionOutcomeStatus.INDETERMINATE, "unknown", "Unconfirmed"),
    ):
        outcome = ParentAttentionDeliveryOutcome(
            assessment_id="a",
            child_user_id="child",
            chat_id="chat",
            status=status,
            reason=reason,
        )
        assert _delivery_view(outcome, action)["label"] == label
    assert _delivery_view(None, None)["status"] == "unknown"
    assert _queue_view(None) is None
    assert _decision(assessment("x", decision=True)) == "yes"
    assert _decision(assessment("x", decision=False)) == "no"
    assert _decision(assessment("x", decision=None)) == "unknown"
    cursor = _encode_cursor(NOW, "a")
    assert _decode_cursor(cursor) == (NOW, "a")
    assert _decode_cursor("bad") is None
    assert _decode_cursor("W10") is None
