"""Tests for the focused Guard alert orchestration path."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.domain.guard import GuardAnalysisRecord
from echo_v2.persistence.guard_alert_repositories import InMemoryGuardAlertRepository
from echo_v2.persistence.guard_repositories import InMemoryGuardianChildLinkRepository
from echo_v2.persistence.scheduled_actions import InMemoryScheduledActionRepository
from echo_v2.services.guard_alert_service import GuardAlertService
from echo_v2.services.guard_taxonomy import GuardDecision

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


class FakeUsers:
    async def get_phone_by_id(self, user_id: str) -> str | None:
        return "+972500000001" if user_id == "guardian" else None


class FakeTokens:
    async def issue(self, user_id: str) -> tuple[str, str]:
        return f"session-{user_id}", f"token-{user_id}"


class FakeAnalyses:
    def __init__(self, records: list[GuardAnalysisRecord] | None = None) -> None:
        self.records = records or []

    async def list_needing_alert_evaluation(self, *, since, limit=20):
        return [
            item
            for item in self.records
            if item.created_at is not None and item.created_at >= since
        ][:limit]


class FakeUow:
    def __init__(self, alerts, actions) -> None:
        self.guard_alerts = alerts
        self.scheduled_actions = actions

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb


class FakeUowFactory:
    def __init__(self, alerts, actions) -> None:
        self.uow = FakeUow(alerts, actions)

    def __call__(self) -> FakeUow:
        return self.uow


def record(
    *,
    result_id: str,
    version: int,
    at: datetime,
    signals: tuple[str, ...] = ("location_request",),
    categories: tuple[str, ...] = ("suspicious_contact",),
) -> GuardAnalysisRecord:
    return GuardAnalysisRecord(
        id=result_id,
        child_user_id="child",
        connection_id="connection",
        chat_id="chat@c.us",
        target_version=version,
        signals=signals,
        categories=categories,
        confidence=0.95,
        reason="test evidence",
        evidence_message_ids=(f"m{version}",),
        decision=GuardDecision.CONCERNING if signals else GuardDecision.NONE,
        model="test",
        prompt_version="test",
        analyzer_version="test",
        taxonomy_version="v1",
        created_at=at,
        diagnostics={
            "cumulative_signals": list(signals),
            "cumulative_categories": list(categories),
        },
    )


async def setup_service() -> tuple[GuardAlertService, InMemoryGuardAlertRepository, InMemoryScheduledActionRepository, InMemoryGuardianChildLinkRepository]:
    alerts = InMemoryGuardAlertRepository()
    actions = InMemoryScheduledActionRepository()
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian", child_user_id="child", now=BASE)
    service = GuardAlertService(
        links=links,
        users=FakeUsers(),
        token_service=FakeTokens(),
        unit_of_work=FakeUowFactory(alerts, actions),
        alerts=alerts,
        analyses=FakeAnalyses(),
        base_url="https://example.test",
        enabled=True,
    )
    return service, alerts, actions, links


async def test_actionable_result_creates_one_alert_and_scheduled_action() -> None:
    service, alerts, actions, _links = await setup_service()

    assert await service.evaluate_committed(
        record=record(result_id="result-1", version=1, at=BASE),
        result_id="result-1",
        now=BASE,
    ) == "scheduled"
    pending = await actions.list_pending("guardian")
    assert len(pending) == 1
    assert len(alerts.alerts) == 1
    assert pending[0].payload["idempotency_key"].startswith("guard:alert:")
    alert = next(iter(alerts.alerts.values()))
    assert alert.metadata == {
        "guardian_phone": "+972500000001",
        "review_token": "token-guardian",
        "session_id": "session-guardian",
    }
    assert pending[0].payload["template"]["url_suffix"] == "token-guardian"

    assert await service.evaluate_committed(
        record=record(result_id="result-1", version=1, at=BASE),
        result_id="result-1",
        now=BASE,
    ) == "already_evaluated"
    assert len(await actions.list_pending("guardian")) == 1
    assert len(alerts.alerts) == 1


async def test_one_negative_analysis_does_not_close_episode() -> None:
    service, alerts, _actions, _links = await setup_service()
    await service.evaluate_committed(
        record=record(result_id="result-1", version=1, at=BASE),
        result_id="result-1",
        now=BASE,
    )

    negative = record(
        result_id="result-2",
        version=2,
        at=BASE + timedelta(minutes=31),
        signals=(),
        categories=(),
    )
    negative = replace(negative, decision=GuardDecision.NONE)
    assert await service.evaluate_committed(
        record=negative,
        result_id="result-2",
        now=BASE + timedelta(minutes=31),
    ) == "no_action"
    state = await alerts.list_threshold_states(child_user_id="child", chat_id="chat@c.us")
    assert state[0].status.value == "active"


async def test_reconciliation_evaluates_committed_result() -> None:
    service, alerts, actions, _links = await setup_service()
    pending = record(result_id="recovered-result", version=1, at=datetime.now(timezone.utc))
    service._analyses = FakeAnalyses([pending])

    assert await service.reconcile_pending(limit=5) == 1
    assert len(alerts.alerts) == 1
    assert len(await actions.list_pending("guardian")) == 1


async def test_disabled_or_unlinked_result_does_not_schedule() -> None:
    service, alerts, actions, links = await setup_service()
    service._enabled = False
    assert await service.evaluate_committed(
        record=record(result_id="disabled", version=1, at=BASE),
        result_id="disabled",
        now=BASE,
    ) == "disabled"
    service._enabled = True
    link = links._links[("guardian", "child")]
    links._links[("guardian", "child")] = replace(link, safety_enabled_at=None)
    assert await service.evaluate_committed(
        record=record(result_id="unlinked", version=1, at=BASE),
        result_id="unlinked",
        now=BASE,
    ) == "no_active_link"
    assert not alerts.alerts
    assert not await actions.list_pending("guardian")


async def test_urgent_alert_uses_short_ttl() -> None:
    service, alerts, _actions, _links = await setup_service()
    detected = datetime.now(timezone.utc)
    urgent = record(
        result_id="urgent-result",
        version=1,
        at=detected,
        signals=("self_harm_expression",),
        categories=("distress",),
    )
    urgent = replace(urgent, decision=GuardDecision.URGENT)
    assert await service.evaluate_committed(
        record=urgent,
        result_id="urgent-result",
        now=detected,
    ) == "scheduled"
    alert = next(iter(alerts.alerts.values()))
    assert alert.kind.value == "urgent"
    assert alert.expires_at == detected + timedelta(minutes=15)


async def test_rollout_and_guardian_destination_gates() -> None:
    service, alerts, actions, links = await setup_service()
    service._rollout_started_at = BASE + timedelta(hours=1)
    assert await service.evaluate_committed(
        record=record(result_id="before-rollout", version=1, at=BASE),
        result_id="before-rollout",
        now=BASE,
    ) == "before_rollout"

    service._rollout_started_at = None
    link = links._links[("guardian", "child")]
    links._links[("guardian", "child")] = replace(link, guardian_user_id="unknown")
    assert await service.evaluate_committed(
        record=record(result_id="no-phone", version=1, at=BASE),
        result_id="no-phone",
        now=BASE,
    ) == "no_guardian_phone"
    assert not alerts.alerts
    assert not await actions.list_pending("guardian")


async def test_threshold_state_version_guards_and_same_family_path() -> None:
    service, alerts, actions, _links = await setup_service()
    await service.evaluate_committed(
        record=record(result_id="result-1", version=1, at=BASE),
        result_id="result-1",
        now=BASE,
    )
    key = ("child", "chat@c.us", "suspicious_contact_location_or_meeting")
    alerts.states[key] = replace(alerts.states[key], last_processed_version=0)
    assert await service.evaluate_committed(
        record=record(result_id="result-2", version=2, at=BASE + timedelta(minutes=1)),
        result_id="result-2",
        now=BASE + timedelta(minutes=1),
    ) == "no_action"
    alerts.states[key] = replace(alerts.states[key], last_processed_version=3)
    assert await service.evaluate_committed(
        record=record(result_id="result-3", version=2, at=BASE + timedelta(minutes=2)),
        result_id="result-3",
        now=BASE + timedelta(minutes=2),
    ) == "no_action"
    assert len(alerts.alerts) == 1
    assert len(await actions.list_pending("guardian")) == 1


async def test_same_episode_escalates_from_notify_to_urgent_once() -> None:
    service, alerts, actions, _links = await setup_service()
    await service.evaluate_committed(
        record=record(result_id="notify", version=1, at=BASE),
        result_id="notify",
        now=BASE,
    )
    urgent = record(
        result_id="urgent", version=2, at=BASE + timedelta(minutes=1),
        signals=("meeting_request", "secrecy_request"),
        categories=("suspicious_contact",),
    )
    urgent = replace(urgent, decision=GuardDecision.URGENT)
    assert await service.evaluate_committed(
        record=urgent,
        result_id="urgent",
        now=BASE + timedelta(minutes=1),
    ) == "scheduled"
    assert len(alerts.alerts) == 2
    assert len(await actions.list_pending("guardian")) == 2


async def test_same_episode_reanalysis_does_not_create_second_alert() -> None:
    service, alerts, actions, _links = await setup_service()
    await service.evaluate_committed(
        record=record(result_id="result-1", version=1, at=BASE),
        result_id="result-1",
        now=BASE,
    )
    assert await service.evaluate_committed(
        record=record(result_id="result-2", version=2, at=BASE + timedelta(minutes=1)),
        result_id="result-2",
        now=BASE + timedelta(minutes=1),
    ) == "no_action"
    assert len(alerts.alerts) == 1
    assert len(await actions.list_pending("guardian")) == 1


async def test_pre_send_validation_rejects_missing_or_malformed_payload() -> None:
    service, _alerts, _actions, _links = await setup_service()
    assert await service.validate_before_send({}) is False
    assert await service.validate_before_send(
        {
            "guardian_user_id": "guardian",
            "child_user_id": "child",
            "expires_at": "not-a-date",
        }
    ) is False


async def test_pre_send_check_rejects_expired_alert_and_revoked_link() -> None:
    service, alerts, actions, links = await setup_service()
    detected = datetime.now(timezone.utc) + timedelta(hours=1)
    await service.evaluate_committed(
        record=record(result_id="result-1", version=1, at=detected),
        result_id="result-1",
        now=detected,
    )
    action = (await actions.list_pending("guardian"))[0]
    assert await service.validate_before_send(action.payload) is True

    expired = dict(action.payload)
    expired["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    assert await service.validate_before_send(expired) is False
    assert next(iter(alerts.alerts.values())).status.value == "expired"

    link = links._links[("guardian", "child")]
    links._links[("guardian", "child")] = replace(link, safety_enabled_at=None)
    assert await service.validate_before_send(action.payload) is False


async def test_two_guardians_one_without_phone_still_schedules_other() -> None:
    """Two guardians: one has a phone, the other doesn't. Only the one with phone is scheduled."""
    alerts = InMemoryGuardAlertRepository()
    actions = InMemoryScheduledActionRepository()
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian", child_user_id="child", now=BASE)
    await links.upsert_active(guardian_user_id="guardian-2", child_user_id="child", now=BASE)
    service = GuardAlertService(
        links=links,
        users=FakeUsers(),  # returns phone only for "guardian", None for "guardian-2"
        token_service=FakeTokens(),
        unit_of_work=FakeUowFactory(alerts, actions),
        alerts=alerts,
        analyses=FakeAnalyses(),
        base_url="https://example.test",
        enabled=True,
    )
    result = await service.evaluate_committed(
        record=record(result_id="result-1", version=1, at=BASE),
        result_id="result-1",
        now=BASE,
    )
    assert result == "scheduled"
    # Guardian-1 has an alert; guardian-2 has NO_ACTION evaluation (no phone).
    eval_1 = await alerts.get_evaluation("result-1", "guardian")
    eval_2 = await alerts.get_evaluation("result-1", "guardian-2")
    assert eval_1 is not None and eval_1.outcome.name == "SCHEDULED"
    assert eval_2 is not None and eval_2.outcome.name == "NO_ACTION"
    assert len(alerts.alerts) == 1  # only one alert for guardian-1
