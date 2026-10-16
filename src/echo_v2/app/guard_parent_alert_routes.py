"""Routes for the read-only parent-facing Guard alert page."""

from __future__ import annotations

from fastapi import APIRouter, Cookie, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from echo_v2.app.guard_parent_alert_page import (
    GUARD_PARENT_ALERT_EXPIRED_PAGE,
    GUARD_PARENT_ALERT_PAGE,
)
from echo_v2.services.guard_parent_alert_service import GuardParentAlertService
from echo_v2.services.waiting_list_token_service import WaitingListTokenService

__all__ = ["build_guard_parent_alert_router"]

_SESSION_COOKIE = "wls"
_COOKIE_MAX_AGE = 172800


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


def build_guard_parent_alert_router(
    *, token_service: WaitingListTokenService, service: GuardParentAlertService
) -> APIRouter:
    router = APIRouter()

    @router.get("/q/guard/{token}")
    async def guard_alert_token_exchange(token: str) -> Response:
        resolved = await token_service.resolve(token)
        if resolved is None:
            return HTMLResponse(
                content=GUARD_PARENT_ALERT_EXPIRED_PAGE,
                headers=_headers(),
            )
        response = RedirectResponse(url="/guard/alert", status_code=303, headers=_headers())
        response.set_cookie(
            key=_SESSION_COOKIE,
            value=resolved.session_id,
            max_age=_COOKIE_MAX_AGE,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/api",
        )
        return response

    @router.get("/guard/alert", response_class=HTMLResponse)
    async def guard_alert_page() -> HTMLResponse:
        return HTMLResponse(content=GUARD_PARENT_ALERT_PAGE, headers=_headers())

    @router.get("/api/guard/alert")
    async def guard_alert_data(
        wls: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
    ) -> JSONResponse:
        if wls is None:
            raise HTTPException(status_code=401, detail="no session")
        alert = await service.get_alert(session_id=wls)
        if alert is None:
            raise HTTPException(status_code=404, detail="alert unavailable")
        return JSONResponse(
            content={
                "alert_id": alert.alert_id,
                "child_name": alert.child_name,
                "detected_at": alert.detected_at.isoformat(),
                "severity": alert.severity,
                "categories": list(alert.categories),
                "summary": alert.summary,
                "concern": alert.concern,
                "expired": alert.expired,
            },
            headers=_headers(),
        )

    return router
