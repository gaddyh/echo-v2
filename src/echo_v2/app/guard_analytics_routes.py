"""Admin analytics routes for Guard daily reporting and AI cost tracking.

Auth reuses the same waiting-list session cookie and admin user id set
as ``guard_operations_routes``. Parent/child link authorization reuses
``GuardianChildLinkRepository`` for any non-admin access.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Cookie, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse

from echo_v2.app.guard_analytics_page import ANALYTICS_PAGE
from echo_v2.services.reporting.cost_analytics_service import CostAnalyticsService
from echo_v2.services.reporting.guard_reporting_service import GuardReportingService
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


def _parse_period(period: str) -> tuple[date, date]:
    """Parse ``24h``/``7d``/``30d`` to calendar-day boundaries (UTC today)."""
    today = datetime.now(timezone.utc).date()
    if period == "24h":
        return today, today
    days = 7 if period == "7d" else 30 if period == "30d" else 7
    return today - timedelta(days=days - 1), today


def build_guard_analytics_router(
    *,
    reporting_service: GuardReportingService,
    cost_service: CostAnalyticsService,
    token_service: WaitingListTokenService,
    admin_user_ids: set[str] | frozenset[str] = frozenset(),
    child_name_resolver: Any | None = None,
) -> APIRouter:
    router = APIRouter()

    async def _require_admin_async(wls: str | None) -> str:
        if wls is None:
            raise HTTPException(status_code=401, detail="admin access required")
        resolved = await token_service.resolve_session(wls)
        if resolved is None:
            raise HTTPException(status_code=401, detail="admin access required")
        if resolved.user_id not in admin_user_ids:
            raise HTTPException(status_code=401, detail="admin access required")
        return resolved.user_id

    @router.get("/api/admin/guard/analytics", response_class=HTMLResponse)
    async def analytics_page() -> HTMLResponse:
        return HTMLResponse(content=ANALYTICS_PAGE, headers=_headers())

    @router.get("/api/admin/guard/analytics/overview")
    async def overview(
        wls: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
        period: str = "7d",
        child_id: str | None = None,
    ) -> JSONResponse:
        await _require_admin_async(wls)
        date_from, date_to = _parse_period(period)
        result = await reporting_service.overview(
            date_from=date_from,
            date_to=date_to,
            child_user_id=child_id,
        )
        return JSONResponse(
            content=jsonable_encoder(result), headers=_headers()
        )

    @router.get("/api/admin/guard/analytics/children")
    async def children(
        wls: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
        period: str = "7d",
    ) -> JSONResponse:
        await _require_admin_async(wls)
        date_from, date_to = _parse_period(period)
        rows = await reporting_service.children_table(
            date_from=date_from,
            date_to=date_to,
        )
        # Enrich with child names if resolver is available.
        names: dict[str, str | None] = {}
        if child_name_resolver is not None:
            for row in rows:
                if row.child_user_id not in names:
                    names[row.child_user_id] = await child_name_resolver.resolve_name(
                        row.child_user_id
                    )
        payload = [
            {
                "child_user_id": row.child_user_id,
                "child_name": names.get(row.child_user_id),
                "local_date": row.local_date.isoformat(),
                "coverage_status": row.coverage_status,
                "coverage_health": row.coverage_health,
                "messages_received": row.messages_received,
                "guard_assessments": row.guard_assessments,
                "security_detections": row.security_detections,
                "exposure_detections": row.exposure_detections,
                "security_alerts_sent": row.security_alerts_sent,
                "exposure_alerts_sent": row.exposure_alerts_sent,
                "connection_seconds": row.connection_seconds,
            }
            for row in rows
        ]
        return JSONResponse(content=payload, headers=_headers())

    @router.get("/api/admin/guard/analytics/costs")
    async def costs(
        wls: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
        period: str = "7d",
        group_by: str = "model",
        child_id: str | None = None,
    ) -> JSONResponse:
        await _require_admin_async(wls)
        date_from, date_to = _parse_period(period)
        # Use UTC start/end of day for cost queries.
        start = datetime.combine(date_from, time.min, tzinfo=timezone.utc)
        end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=timezone.utc)
        breakdown = await cost_service.costs(
            date_from=start,
            date_to=end,
            child_user_id=child_id,
        )
        if group_by == "operation":
            groups = breakdown.by_operation
        elif group_by == "child":
            groups = breakdown.by_child
        else:
            groups = breakdown.by_model
        return JSONResponse(
            content=jsonable_encoder(
                {
                    "overview": breakdown.overview,
                    "groups": groups,
                }
            ),
            headers=_headers(),
        )

    @router.get("/api/admin/guard/analytics/daily")
    async def daily(
        wls: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
        period: str = "7d",
        child_id: str | None = None,
    ) -> JSONResponse:
        await _require_admin_async(wls)
        date_from, date_to = _parse_period(period)
        series = await reporting_service.daily_series(
            date_from=date_from,
            date_to=date_to,
            child_user_id=child_id,
        )
        return JSONResponse(
            content=jsonable_encoder(series), headers=_headers()
        )

    @router.post("/api/admin/guard/analytics/rebuild")
    async def rebuild(
        wls: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
        date_from: date | None = None,
        date_to: date | None = None,
        child_id: str | None = None,
    ) -> JSONResponse:
        await _require_admin_async(wls)
        if date_from is None or date_to is None:
            raise HTTPException(status_code=400, detail="date_from and date_to required")
        if child_id is None:
            raise HTTPException(status_code=400, detail="child_id required (rebuild is per-child)")
        results = await reporting_service.rebuild_range(
            child_user_id=child_id,
            date_from=date_from,
            date_to=date_to,
        )
        return JSONResponse(
            content={"rebuilt": len(results)}, headers=_headers()
        )

    return router
