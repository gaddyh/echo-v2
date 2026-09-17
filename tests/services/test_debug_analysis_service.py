"""Tests for the DebugAnalysisService and debug analysis routes.

Covers:
* list_analysis with valid session → chats + results grouped correctly.
* list_analysis with invalid/expired session → None.
* list_analysis with no data → empty response.
* list_analysis with results but no chat state row (edge case).
* GET /debug serves the HTML page.
* GET /api/debug/analysis with cookie → JSON response.
* GET /api/debug/analysis without cookie → 401.
* GET /api/debug/analysis with expired session → 401.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from echo_v2.app.waiting_list_routes import build_waiting_list_router
from echo_v2.domain.waiting_for_me import WaitingForMeDecision, WaitingForMeResult
from echo_v2.persistence.chat_repositories import (
    InMemoryChatStateRepository,
    InMemoryWaitingForMeResultRepository,
)
from echo_v2.persistence.waiting_list_tokens import (
    InMemoryWaitingListSessionRepository,
)
from echo_v2.ports.whatsapp import MessageDirection
from echo_v2.services.debug_analysis_service import DebugAnalysisService
from echo_v2.services.waiting_list_action_service import WaitingListActionService
from echo_v2.services.waiting_list_token_service import WaitingListTokenService

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 12, 6, 0, 0, tzinfo=timezone.utc)
USER_ID = "user-1"
CHAT_ID_A = "972501111111@c.us"
CHAT_ID_B = "972502222222@c.us"
BOT_PHONE = "972500000000"


def _make_app() -> tuple[
    FastAPI,
    WaitingListTokenService,
    DebugAnalysisService,
    InMemoryChatStateRepository,
    InMemoryWaitingForMeResultRepository,
]:
    chat_state_repo = InMemoryChatStateRepository()
    result_repo = InMemoryWaitingForMeResultRepository()
    session_repo = InMemoryWaitingListSessionRepository()
    token_service = WaitingListTokenService(session_repo)
    debug_service = DebugAnalysisService(
        token_service=token_service,
        chat_state_repo=chat_state_repo,
        result_repo=result_repo,
    )

    # Minimal WaitingListActionService stub — not used by debug routes but
    # required by build_waiting_list_router.
    from unittest.mock import AsyncMock

    fake_service = AsyncMock(spec=WaitingListActionService)

    router = build_waiting_list_router(
        token_service=token_service,
        waiting_list_service=fake_service,
        bot_phone=BOT_PHONE,
        debug_service=debug_service,
    )
    app = FastAPI()
    app.include_router(router)
    return app, token_service, debug_service, chat_state_repo, result_repo


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://test")


async def _seed_chats(
    chat_state_repo: InMemoryChatStateRepository,
    result_repo: InMemoryWaitingForMeResultRepository,
) -> None:
    """Seed two chats with analysis results."""
    await chat_state_repo.upsert_on_message(
        user_id=USER_ID,
        chat_id=CHAT_ID_A,
        direction=MessageDirection.INBOUND,
        observed_at=NOW,
        next_analysis_at=None,
        chat_name="שיוש",
    )
    await chat_state_repo.upsert_on_message(
        user_id=USER_ID,
        chat_id=CHAT_ID_B,
        direction=MessageDirection.OUTBOUND,
        observed_at=NOW,
        next_analysis_at=None,
        chat_name="מיכל",
    )
    await result_repo.save(
        user_id=USER_ID,
        chat_id=CHAT_ID_A,
        result=WaitingForMeResult(
            decision=WaitingForMeDecision.WAITING_FOR_ME,
            confidence=0.9,
            reason="שאלה פתוחה",
            summary="שיוש שואל מתי נדבר",
            target_version=1,
            model="gpt-4.1",
            prompt_version="v1",
            analyzer_version="2026-09-15.1",
        ),
    )
    await result_repo.save(
        user_id=USER_ID,
        chat_id=CHAT_ID_A,
        result=WaitingForMeResult(
            decision=WaitingForMeDecision.NOT_WAITING_FOR_ME,
            confidence=0.8,
            reason="נענתה",
            summary="שיוש קיבל תשובה",
            target_version=2,
        ),
    )
    await result_repo.save(
        user_id=USER_ID,
        chat_id=CHAT_ID_B,
        result=WaitingForMeResult(
            decision=WaitingForMeDecision.UNCERTAIN,
            confidence=0.5,
            reason="לא ברור",
            summary="מיכל שואלת על פגישה",
            target_version=1,
        ),
    )


# --- DebugAnalysisService unit tests ---


async def test_list_analysis_valid_session():
    _app, token_service, debug_service, chat_state_repo, result_repo = _make_app()
    await _seed_chats(chat_state_repo, result_repo)
    session_id, _raw = await token_service.issue(user_id=USER_ID)

    result = await debug_service.list_analysis(session_id=session_id)

    assert result is not None
    assert result.user_id == USER_ID
    assert result.total_results == 3
    assert len(result.chats) == 2

    # Chats ordered by last_message_at desc; both have same NOW so order
    # is by insertion (dict order). Find by chat_id.
    chat_a = next(c for c in result.chats if c.chat_id == CHAT_ID_A)
    chat_b = next(c for c in result.chats if c.chat_id == CHAT_ID_B)

    assert chat_a.chat_name == "שיוש"
    assert chat_a.last_direction == "inbound"
    assert chat_a.activity_version == 1
    assert len(chat_a.results) == 2

    # Results newest first.
    assert chat_a.results[0].decision == "not_waiting_for_me"
    assert chat_a.results[0].target_version == 2
    assert chat_a.results[1].decision == "waiting_for_me"
    assert chat_a.results[1].target_version == 1
    assert chat_a.results[1].model == "gpt-4.1"
    assert chat_a.results[1].prompt_version == "v1"
    assert chat_a.results[1].analyzer_version == "2026-09-15.1"

    assert chat_b.chat_name == "מיכל"
    assert chat_b.last_direction == "outbound"
    assert len(chat_b.results) == 1
    assert chat_b.results[0].decision == "uncertain"


async def test_list_analysis_invalid_session():
    _app, _token_service, debug_service, _chat_state_repo, _result_repo = _make_app()
    result = await debug_service.list_analysis(session_id="nonexistent")
    assert result is None


async def test_list_analysis_no_data():
    _app, token_service, debug_service, _chat_state_repo, _result_repo = _make_app()
    session_id, _raw = await token_service.issue(user_id=USER_ID)

    result = await debug_service.list_analysis(session_id=session_id)

    assert result is not None
    assert result.chats == []
    assert result.total_results == 0


async def test_list_analysis_results_without_chat_state():
    """Results exist for a chat_id that has no chats row."""
    _app, token_service, debug_service, _chat_state_repo, result_repo = _make_app()
    await result_repo.save(
        user_id=USER_ID,
        chat_id="orphan@c.us",
        result=WaitingForMeResult(
            decision=WaitingForMeDecision.WAITING_FOR_ME,
            target_version=1,
        ),
    )
    session_id, _raw = await token_service.issue(user_id=USER_ID)

    result = await debug_service.list_analysis(session_id=session_id)

    assert result is not None
    assert len(result.chats) == 1
    assert result.chats[0].chat_id == "orphan@c.us"
    assert result.chats[0].chat_name is None
    assert result.chats[0].last_message_at == ""
    assert result.chats[0].last_direction == ""
    assert result.chats[0].activity_version == 0
    assert len(result.chats[0].results) == 1
    assert result.total_results == 1


async def test_list_analysis_conversation_snapshot():
    """Conversation snapshot is passed through to the result entry."""
    _app, token_service, debug_service, _chat_state_repo, result_repo = _make_app()
    snapshot = {"messages": [{"from": "them", "text": "hello"}]}
    await result_repo.save(
        user_id=USER_ID,
        chat_id=CHAT_ID_A,
        result=WaitingForMeResult(
            decision=WaitingForMeDecision.WAITING_FOR_ME,
            target_version=1,
            conversation_snapshot=snapshot,
        ),
    )
    session_id, _raw = await token_service.issue(user_id=USER_ID)

    result = await debug_service.list_analysis(session_id=session_id)

    assert result is not None
    assert result.chats[0].results[0].conversation_snapshot == snapshot


# --- Route tests ---


async def test_debug_page_serves_html():
    app, _, _, _, _ = _make_app()
    async with _client(app) as client:
        resp = await client.get("/debug")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Debug" in resp.text


async def test_debug_analysis_without_cookie_returns_401():
    app, _, _, _, _ = _make_app()
    async with _client(app) as client:
        resp = await client.get("/api/debug/analysis")
    assert resp.status_code == 401


async def test_debug_analysis_with_expired_session_returns_401():
    app, _, _, _, _ = _make_app()
    async with _client(app) as client:
        resp = await client.get(
            "/api/debug/analysis",
            cookies={"wls": "invalid-session"},
        )
    assert resp.status_code == 401


async def test_debug_analysis_with_valid_session():
    app, token_service, _, chat_state_repo, result_repo = _make_app()
    await _seed_chats(chat_state_repo, result_repo)
    _session_id, raw_token = await token_service.issue(user_id=USER_ID)

    async with _client(app) as client:
        # Exchange token for cookie.
        await client.get(f"/q/{raw_token}")
        # Use cookie for debug API.
        resp = await client.get("/api/debug/analysis")

    assert resp.status_code == 200
    data = resp.json()
    assert data["user_id"] == USER_ID
    assert data["total_results"] == 3
    assert len(data["chats"]) == 2

    chat_a = next(c for c in data["chats"] if c["chat_id"] == CHAT_ID_A)
    assert chat_a["chat_name"] == "שיוש"
    assert len(chat_a["results"]) == 2
    assert chat_a["results"][0]["decision"] == "not_waiting_for_me"
    assert chat_a["results"][1]["decision"] == "waiting_for_me"
    assert chat_a["results"][1]["model"] == "gpt-4.1"


async def test_debug_analysis_empty_data():
    app, token_service, _, _, _ = _make_app()
    _session_id, raw_token = await token_service.issue(user_id=USER_ID)

    async with _client(app) as client:
        await client.get(f"/q/{raw_token}")
        resp = await client.get("/api/debug/analysis")

    assert resp.status_code == 200
    data = resp.json()
    assert data["chats"] == []
    assert data["total_results"] == 0
