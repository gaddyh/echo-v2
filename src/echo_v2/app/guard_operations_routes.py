"""Authenticated Guard Activity telemetry routes + Echo Operations Center."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Cookie, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from echo_v2.app.guard_admin_dashboard_page import ADMIN_DASHBOARD_PAGE
from echo_v2.app.guard_operations_page import GUARD_ACTIVITY_PAGE
from echo_v2.services.guard_simple_telemetry_service import GuardSimpleTelemetryService
from echo_v2.services.ops_center_service import (
    DEFAULT_PERIOD,
    PERIOD_SECONDS,
    OpsCenterService,
)
from echo_v2.services.ops_error_view import OpsErrorView
from echo_v2.services.ops_health_rules import OpsHealthRules
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
    *,
    service: GuardSimpleTelemetryService,
    token_service: WaitingListTokenService,
    admin_user_ids: set[str] | frozenset[str] = frozenset(),
    ops_center_service: OpsCenterService | None = None,
    ops_health_rules: OpsHealthRules | None = None,
    ops_error_view: OpsErrorView | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/q/guard/operations/{token}")
    async def operations_token_exchange(token: str) -> Response:
        resolved = await token_service.resolve(token)
        if resolved is None:
            return HTMLResponse(content="Guard Activity link expired", headers=_headers())
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
        return HTMLResponse(content=GUARD_ACTIVITY_PAGE, headers=_headers())

    @router.get("/debug/guard/admin", response_class=HTMLResponse)
    async def admin_page() -> HTMLResponse:
        return HTMLResponse(content=ADMIN_DASHBOARD_PAGE, headers=_headers())

    async def _require_admin(
        wls: str | None,
    ) -> None:
        if wls is None or ops_center_service is None:
            raise HTTPException(status_code=401, detail="admin access required")
        resolved = await token_service.resolve_session(wls)
        if resolved is None or resolved.user_id not in admin_user_ids:
            raise HTTPException(status_code=401, detail="admin access required")

    @router.get("/api/debug/guard/admin")
    async def admin_data(
        wls: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
        period: str = Query(default=DEFAULT_PERIOD),
        provider: str = Query(default="all"),
        worker_id: str | None = Query(default=None),
        user_id: str | None = Query(default=None),
    ) -> JSONResponse:
        await _require_admin(wls)
        if ops_center_service is None:
            raise HTTPException(status_code=503, detail="ops center unavailable")
        snapshot = await ops_center_service.snapshot(
            period=period if period in PERIOD_SECONDS else DEFAULT_PERIOD,
            provider=provider,
            worker_id=worker_id,
            user_id=user_id,
        )
        if ops_health_rules is not None:
            snapshot["health"] = ops_health_rules.evaluate(snapshot)
        return JSONResponse(
            content=jsonable_encoder(snapshot),
            headers=_headers(),
        )

    @router.get("/api/debug/guard/admin/users/{user_id}")
    async def admin_user_detail(
        user_id: str,
        wls: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
    ) -> JSONResponse:
        await _require_admin(wls)
        if ops_center_service is None:
            raise HTTPException(status_code=503, detail="ops center unavailable")
        return JSONResponse(
            content=jsonable_encoder(await ops_center_service.user_detail(user_id)),
            headers=_headers(),
        )

    @router.get("/api/debug/guard/admin/errors")
    async def admin_errors(
        wls: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
        subsystem: str | None = Query(default=None),
        severity: str | None = Query(default=None),
        hours: int = Query(default=24, ge=1, le=720),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        await _require_admin(wls)
        if ops_error_view is None:
            raise HTTPException(status_code=503, detail="error view unavailable")
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        result = await ops_error_view.list_errors(
            since=since,
            subsystem=subsystem,
            severity=severity,
            limit=limit,
            offset=offset,
        )
        return JSONResponse(
            content=jsonable_encoder(result),
            headers=_headers(),
        )

    @router.get("/api/debug/guard/operations")
    async def operations_data(
        wls: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
        child_id: str | None = None,
        chat_id: str | None = None,
        chat_name: str | None = None,
        decision: str | None = None,
        analyzer: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> JSONResponse:
        if wls is None:
            raise HTTPException(status_code=401, detail="no session")
        resolved = await token_service.resolve_session(wls)
        if resolved is None:
            raise HTTPException(status_code=401, detail="session expired")
        activity_kwargs: dict[str, Any] = {
            "session_id": wls,
            "child_user_id": child_id,
            "chat_id": chat_id,
            "decision": decision,
            "date_from": _date_start(date_from),
            "date_to": _date_end(date_to),
            "limit": limit,
            "cursor": cursor,
            "admin": resolved.user_id in admin_user_ids,
        }
        if analyzer is not None:
            activity_kwargs["analyzer"] = analyzer
        if chat_name is not None:
            activity_kwargs["chat_name"] = chat_name
        result = await service.list_activity(**activity_kwargs)
        if result is None:
            raise HTTPException(status_code=401, detail="session expired")
        return JSONResponse(
            content=jsonable_encoder(asdict(result)),
            headers=_headers(),
        )

    @router.get("/api/debug/guard/operations/{assessment_id}/context")
    async def analysis_context(
        assessment_id: str,
        wls: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
    ) -> JSONResponse:
        if wls is None:
            raise HTTPException(status_code=401, detail="no session")
        resolved = await token_service.resolve_session(wls)
        if resolved is None:
            raise HTTPException(status_code=401, detail="session expired")
        result = await service.get_context(
            session_id=wls,
            assessment_id=assessment_id,
            admin=resolved.user_id in admin_user_ids,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="analysis not found")
        return JSONResponse(content=jsonable_encoder(result), headers=_headers())

    @router.get("/api/debug/guard/operations/{assessment_id}")
    async def analysis_detail(
        assessment_id: str,
        wls: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
    ) -> JSONResponse:
        if wls is None:
            raise HTTPException(status_code=401, detail="no session")
        resolved = await token_service.resolve_session(wls)
        if resolved is None:
            raise HTTPException(status_code=401, detail="session expired")
        result = await service.get_analysis(
            session_id=wls,
            assessment_id=assessment_id,
            admin=resolved.user_id in admin_user_ids,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="analysis not found")
        return JSONResponse(content=jsonable_encoder(result), headers=_headers())

    return router


def _date_start(value: date | None) -> datetime | None:
    return datetime.combine(value, time.min, tzinfo=timezone.utc) if value else None


def _date_end(value: date | None) -> datetime | None:
    return (
        datetime.combine(value, time.min, tzinfo=timezone.utc) + timedelta(days=1)
        if value
        else None
    )


__all__ = ["build_guard_operations_router"]
