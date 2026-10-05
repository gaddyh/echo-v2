from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from echo_v2.app.guard_debug_routes import build_guard_debug_router
from echo_v2.domain.chat import Message
from echo_v2.domain.guard import GuardAnalysisRecord
from echo_v2.domain.guard_feedback import GuardFeedbackLabel
from echo_v2.persistence.chat_repositories import InMemoryMessageRepository
from echo_v2.persistence.guard_feedback import InMemoryGuardFeedbackRepository
from echo_v2.persistence.guard_repositories import (
    InMemoryGuardAnalysisRepository,
    InMemoryGuardianChildLinkRepository,
)
from echo_v2.persistence.waiting_list_tokens import InMemoryWaitingListSessionRepository
from echo_v2.ports.whatsapp import MessageDirection
from echo_v2.services.guard_review_service import GuardReviewService
from echo_v2.services.guard_taxonomy import GuardDecision
from echo_v2.services.waiting_list_token_service import WaitingListTokenService

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)


def record(version: int, decision: GuardDecision, *, created: datetime) -> GuardAnalysisRecord:
    return GuardAnalysisRecord(
        id=f"result-{version}", child_user_id="child", connection_id="connection",
        chat_id="chat@c.us", target_version=version, signals=("signal",), chat_name="Shay Chat",
        categories=("category",), confidence=0.8, reason="reason",
        evidence_message_ids=(f"message-{version}",), decision=decision,
        model="model", prompt_version="prompt", analyzer_version="analyzer",
        taxonomy_version="taxonomy", created_at=created, schedule_reason="watch_with_new_activity",
        pending_since=created - timedelta(seconds=60), scheduled_for=created - timedelta(seconds=5),
    )


async def setup() -> tuple[GuardReviewService, str, InMemoryGuardFeedbackRepository]:
    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    session_id, _ = await tokens.issue(user_id="guardian")
    links = InMemoryGuardianChildLinkRepository()
    await links.upsert_active(guardian_user_id="guardian", child_user_id="child", now=NOW)
    analyses = InMemoryGuardAnalysisRepository()
    await analyses.save(record(1, GuardDecision.NONE, created=NOW - timedelta(minutes=2)))
    await analyses.save(record(2, GuardDecision.WATCH, created=NOW))
    messages = InMemoryMessageRepository()
    for version in (1, 2):
        await messages.save(Message(
            id=f"message-{version}", user_id="child", connection_id="connection",
            chat_id="chat@c.us", provider_message_id=f"provider-{version}",
            direction=MessageDirection.INBOUND, sender_id="sender", timestamp=NOW,
            text=f"Evidence {version}",
        ))
    feedback = InMemoryGuardFeedbackRepository()
    return GuardReviewService(
        token_service=tokens, links=links, analyses=analyses,
        feedback=feedback, messages=messages,
    ), session_id, feedback


async def test_review_is_guardian_scoped_and_derives_progression() -> None:
    service, session_id, _ = await setup()
    result = await service.list_reviews(session_id=session_id, category="category")
    assert result is not None
    assert result.total_results == 2
    newest = result.results[0]
    assert newest.previous_decision == "none"
    assert newest.activity_delta == 1
    assert newest.scheduled_delay_seconds == 55
    assert newest.scheduler_lag_seconds == 5
    assert newest.evidence_messages[0].text == "Evidence 2"


async def test_filters_and_feedback_edits() -> None:
    service, session_id, feedback = await setup()
    result = await service.list_reviews(session_id=session_id, decision="watch", chat_id="chat@c.us")
    assert result is not None and len(result.results) == 1
    saved = await service.set_feedback(
        session_id=session_id, result_id="result-2", label=GuardFeedbackLabel.FALSE_POSITIVE, note="first"
    )
    assert saved is not None and saved.label == GuardFeedbackLabel.FALSE_POSITIVE
    await service.set_feedback(
        session_id=session_id, result_id="result-2", label=GuardFeedbackLabel.CORRECT, note="changed"
    )
    assert (await feedback.list_events("result-2"))[1].old_label == GuardFeedbackLabel.FALSE_POSITIVE
    assert (await feedback.get_for_results(["result-2"]))["result-2"].note == "changed"


async def test_invalid_and_unauthorized_feedback() -> None:
    service, session_id, _ = await setup()
    assert await service.list_reviews(session_id="missing") is None
    assert await service.set_feedback(
        session_id="missing", result_id="result-2", label=GuardFeedbackLabel.CORRECT, note=None
    ) is None
    assert await service.set_feedback(
        session_id=session_id, result_id="missing", label=GuardFeedbackLabel.CORRECT, note=None
    ) is None


async def test_route_auth_and_feedback() -> None:
    service, session_id, _ = await setup()
    app = FastAPI()
    app.include_router(build_guard_debug_router(service=service, token_service=service._tokens))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        assert (await client.get("/api/debug/guard")).status_code == 401
        assert (await client.post("/api/debug/guard/feedback", json={"result_id": "x", "label": "correct"})).status_code == 401
        assert (await client.get("/api/debug/guard", cookies={"wls": "missing"})).status_code == 401
        assert (await client.get("/debug/guard")).status_code == 200
        expired = await client.get("/q/guard/not-a-token")
        assert expired.status_code == 200
        _new_session, raw_token = await service._tokens.issue("guardian")
        token_response = await client.get(f"/q/guard/{raw_token}")
        assert token_response.status_code == 303
        assert "/debug/guard" in token_response.headers["location"]
        response = await client.get("/api/debug/guard", cookies={"wls": session_id})
        assert response.status_code == 200
        assert response.json()["results"][0]["previous_decision"] == "none"
        assert response.json()["results"][0]["evidence_messages"][0]["text"] == "Evidence 2"
        response = await client.post(
            "/api/debug/guard/feedback", cookies={"wls": session_id},
            json={"result_id": "result-2", "label": "correct", "note": "ok"},
        )
        assert response.status_code == 200
        assert (await client.get("/debug/guard")).status_code == 200
