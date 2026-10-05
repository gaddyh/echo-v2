"""Internal Guard shadow-review routes."""

from __future__ import annotations

from datetime import date, datetime, time, timezone

from fastapi import APIRouter, Cookie, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from echo_v2.app.guard_debug_page import GUARD_DEBUG_PAGE
from echo_v2.domain.guard_feedback import GuardFeedbackLabel
from echo_v2.services.guard_review_service import (
    GuardReviewResponse,
    GuardReviewService,
)
from echo_v2.services.waiting_list_token_service import WaitingListTokenService

_SESSION_COOKIE = "wls"


class GuardFeedbackRequest(BaseModel):
    result_id: str = Field(min_length=1, max_length=128)
    label: GuardFeedbackLabel
    note: str | None = Field(default=None, max_length=2000)


def _headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'",
    }


def build_guard_debug_router(
    *, service: GuardReviewService, token_service: WaitingListTokenService
) -> APIRouter:
    router = APIRouter()

    @router.get("/q/guard/{token}")
    async def guard_token_exchange(token: str) -> Response:
        resolved = await token_service.resolve(token)
        if resolved is None:
            return HTMLResponse(content="Guard review link expired", headers=_headers())
        response = RedirectResponse(url="/debug/guard", status_code=303, headers=_headers())
        response.set_cookie(
            key=_SESSION_COOKIE, value=resolved.session_id, max_age=172800,
            httponly=True, secure=True, samesite="lax", path="/api",
        )
        return response

    @router.get("/debug/guard", response_class=HTMLResponse)
    async def guard_debug_page() -> HTMLResponse:
        return HTMLResponse(content=GUARD_DEBUG_PAGE, headers=_headers())

    @router.get("/api/debug/guard")
    async def guard_debug(
        wls: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
        decision: str | None = None,
        category: str | None = None,
        chat_id: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> JSONResponse:
        if wls is None:
            raise HTTPException(status_code=401, detail="no session")
        result = await service.list_reviews(
            session_id=wls, decision=decision, category=category, chat_id=chat_id,
            date_from=(datetime.combine(date_from, time.min, tzinfo=timezone.utc) if date_from else None),
            date_to=(datetime.combine(date_to, time.max, tzinfo=timezone.utc) if date_to else None),
        )
        if result is None:
            raise HTTPException(status_code=401, detail="session expired")
        return JSONResponse(content=_response(result), headers=_headers())

    @router.post("/api/debug/guard/feedback")
    async def guard_feedback(
        body: GuardFeedbackRequest,
        wls: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
    ) -> JSONResponse:
        if wls is None:
            raise HTTPException(status_code=401, detail="no session")
        feedback = await service.set_feedback(
            session_id=wls, result_id=body.result_id, label=body.label, note=body.note
        )
        if feedback is None:
            raise HTTPException(status_code=404, detail="result not found")
        return JSONResponse(
            content={
                "result_id": feedback.result_id,
                "reviewer_user_id": feedback.reviewer_user_id,
                "label": feedback.label.value,
                "note": feedback.note,
                "updated_at": feedback.updated_at.isoformat() if feedback.updated_at else None,
            },
            headers=_headers(),
        )

    return router


def _response(response: GuardReviewResponse) -> dict[str, object]:
    # Keep serialization explicit so the API never exposes domain dataclasses wholesale.
    return {
        "guardian_user_id": response.guardian_user_id,
        "total_results": response.total_results,
        "results": [
            {
                "id": item.id, "child_user_id": item.child_user_id, "chat_id": item.chat_id,
                "chat_name": item.chat_name, "created_at": item.created_at, "target_version": item.target_version,
                "previous_target_version": item.previous_target_version,
                "previous_decision": item.previous_decision, "decision": item.decision,
                "categories": list(item.categories), "signals": list(item.signals),
                "confidence": item.confidence, "reason": item.reason,
                "evidence_message_ids": list(item.evidence_message_ids),
                "evidence_messages": [
                    {
                        "id": message.id, "direction": message.direction,
                        "sender": message.sender, "text": message.text,
                        "timestamp": message.timestamp,
                    }
                    for message in item.evidence_messages
                ],
                "is_group": item.is_group,
                "schedule_reason": item.schedule_reason, "pending_since": item.pending_since,
                "scheduled_for": item.scheduled_for,
                "scheduled_delay_seconds": item.scheduled_delay_seconds,
                "actual_delay_seconds": item.actual_delay_seconds,
                "scheduler_lag_seconds": item.scheduler_lag_seconds,
                "activity_delta": item.activity_delta,
                "feedback": (
                    {"label": item.feedback.label.value, "note": item.feedback.note}
                    if item.feedback else None
                ),
            }
            for item in response.results
        ],
    }
