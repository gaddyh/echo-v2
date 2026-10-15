"""Authenticated Guard operations dashboard routes."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Cookie, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from echo_v2.app.guard_operations_page import GUARD_OPERATIONS_PAGE
from echo_v2.services.guard_operations_service import GuardOperationsService
from echo_v2.services.waiting_list_token_service import WaitingListTokenService

_SESSION_COOKIE = "wls"


def _headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; connect-src 'self'"
        ),
    }


def build_guard_operations_router(
    *, service: GuardOperationsService, token_service: WaitingListTokenService
) -> APIRouter:
    router = APIRouter()

    @router.get("/q/guard/operations/{token}")
    async def operations_token_exchange(token: str) -> Response:
        resolved = await token_service.resolve(token)
        if resolved is None:
            return HTMLResponse(content="Guard operations link expired", headers=_headers())
        response = RedirectResponse(
            url="/debug/guard/operations", status_code=303, headers=_headers()
        )
        response.set_cookie(
            key=_SESSION_COOKIE,
            value=resolved.session_id,
            max_age=172800,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/api",
        )
        return response

    @router.get("/debug/guard/operations", response_class=HTMLResponse)
    async def operations_page() -> HTMLResponse:
        return HTMLResponse(content=GUARD_OPERATIONS_PAGE, headers=_headers())

    @router.get("/api/debug/guard/operations")
    async def operations_data(
        wls: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
        child_id: str | None = None,
        chat_id: str | None = None,
        decision: str | None = None,
        category: str | None = None,
        mode: str | None = None,
        overdue: bool | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> JSONResponse:
        if wls is None:
            raise HTTPException(status_code=401, detail="no session")
        result = await service.list_dashboard(
            session_id=wls,
            child_user_id=child_id,
            chat_id=chat_id,
            decision=decision,
            category=category,
            mode=mode,
            overdue=overdue,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )
        if result is None:
            raise HTTPException(status_code=401, detail="session expired")
        return JSONResponse(
            content=jsonable_encoder(
                {
                    "guardian_user_id": result.guardian_user_id,
                    "generated_at": result.generated_at,
                    "summary": result.summary,
                    "chats": result.chats,
                }
            ),
            headers=_headers(),
        )

    return router


__all__ = ["build_guard_operations_router"]
