"""Postgres tests for Guard alert state and atomic creation."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from echo_v2.domain.guard_alert import (
    GuardAlert,
    GuardAlertEvaluation,
    GuardAlertEvaluationOutcome,
    GuardAlertKind,
    GuardAlertStatus,
)
from echo_v2.domain.scheduling import (
    ScheduledAction,
    ScheduledActionStatus,
    ScheduledActionType,
)
from echo_v2.persistence.unit_of_work import PostgresUnitOfWork
from tests.persistence.conftest import insert_user

pytestmark = pytest.mark.asyncio


async def test_guard_alert_and_action_commit_atomically(session_factory, clean_db) -> None:
    guardian = await insert_user(session_factory, "+972546610661")
    child = await insert_user(session_factory, "+972546610662")
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        connection_id = str(
            (
                await session.execute(
                    text(
                        "INSERT INTO whatsapp_connections "
                        "(user_id, provider, provider_connection_id, credentials, "
                        "webhook_token_hash, connection_status) "
                        "VALUES (:uid, 'baileys', :pid, :creds, :hash, 'connected') RETURNING id"
                    ),
                    {
                        "uid": child,
                        "pid": str(uuid.uuid4()),
                        "creds": b"x",
                        "hash": b"x",
                    },
                )
            ).scalar_one()
        )
        result_id = str(
            (
                await session.execute(
                    text(
                        "INSERT INTO guard_analysis_results "
                        "(child_user_id, connection_id, chat_id, target_version, signals, "
                        "categories, confidence, reason, evidence_message_ids, decision, "
                        "model, prompt_version, analyzer_version, taxonomy_version) "
                        "VALUES (:child, :connection, :chat, 1, :signals, :categories, "
                        "0.9, 'reason', :evidence, 'concerning', 'test', 'p', 'a', 'v1') "
                        "RETURNING id"
                    ),
                    {
                        "child": child,
                        "connection": connection_id,
                        "chat": "chat@c.us",
                        "signals": '["location_request"]',
                        "categories": '["suspicious_contact"]',
                        "evidence": '["m1"]',
                    },
                )
            ).scalar_one()
        )
        await session.commit()

    action = ScheduledAction(
        id=str(uuid.uuid4()),
        user_id=guardian,
        type=ScheduledActionType.SEND_BOT_MESSAGE,
        execute_at_utc=now,
        timezone="UTC",
        status=ScheduledActionStatus.PENDING,
        payload={"kind": "guard_alert", "chat_id": "+972546610661"},
    )
    alert = GuardAlert(
        id=str(uuid.uuid4()),
        analysis_result_id=result_id,
        guardian_user_id=guardian,
        child_user_id=child,
        chat_id="chat@c.us",
        kind=GuardAlertKind.NOTIFY,
        categories=("suspicious_contact",),
        summary="A potentially concerning pattern was detected.",
        policy_reason="threshold",
        policy_version="test",
        status=GuardAlertStatus.SCHEDULED,
        scheduled_action_id=action.id,
        logical_event_key="guard:test:event-1",
        threshold_family="suspicious_contact_location_or_meeting",
        episode_start_version=1,
        detected_at=now,
        expires_at=now + timedelta(hours=1),
        created_at=now,
    )

    async with PostgresUnitOfWork(session_factory) as uow:
        state = await uow.guard_alerts.get_threshold_state(
            child_user_id=child,
            chat_id="chat@c.us",
            threshold_family=alert.threshold_family,
        )
        assert state.status.value == "inactive"
        _, created = await uow.scheduled_actions.create_once(action)
        assert created is True
        assert await uow.guard_alerts.save_alert(alert) is True
        assert await uow.guard_alerts.save_evaluation(
            GuardAlertEvaluation(
                analysis_result_id=result_id,
                child_user_id=child,
                chat_id="chat@c.us",
                target_version=1,
                policy_version="test",
                outcome=GuardAlertEvaluationOutcome.SCHEDULED,
                evaluated_at=now,
                alert_id=alert.id,
            )
        ) is True

    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT a.id, s.id FROM guard_alerts a "
                    "JOIN scheduled_actions s ON s.id = a.scheduled_action_id "
                    "WHERE a.logical_event_key = 'guard:test:event-1'"
                )
            )
        ).first()
        assert row is not None
