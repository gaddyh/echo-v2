"""Admin health routes for Guard system monitoring.

Auth reuses the same waiting-list session cookie and admin user id set
as ``guard_operations_routes``. All endpoints are read-only.
"""

from __future__ import annotations

from fastapi import APIRouter, Cookie, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse

from echo_v2.app.guard_health_page import HEALTH_PAGE
from echo_v2.services.guard_health_service import GuardHealthService
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


def build_guard_health_router(
    *,
    health_service: GuardHealthService,
    token_service: WaitingListTokenService,
    admin_user_ids: set[str] | frozenset[str] = frozenset(),
) -> APIRouter:
    router = APIRouter()

    async def _require_admin(wls: str | None) -> str:
        if wls is None:
            raise HTTPException(status_code=401, detail="admin access required")
        resolved = await token_service.resolve_session(wls)
        if resolved is None:
            raise HTTPException(status_code=401, detail="admin access required")
        if resolved.user_id not in admin_user_ids:
            raise HTTPException(status_code=401, detail="admin access required")
        return resolved.user_id

    @router.get("/api/admin/guard/health", response_class=HTMLResponse)
    async def health_page() -> HTMLResponse:
        return HTMLResponse(content=HEALTH_PAGE, headers=_headers())

    @router.get("/api/admin/guard/health/snapshot")
    async def health_snapshot(
        wls: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
    ) -> JSONResponse:
        await _require_admin(wls)
        snapshot = await health_service.snapshot()
        return JSONResponse(
            content=jsonable_encoder(snapshot), headers=_headers()
        )

    return router
