"""Public deterministic Guard demo replay routes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

from echo_v2.app.guard_demo_page import GUARD_DEMO_PAGE
from echo_v2.app.guard_demo_page_he import GUARD_DEMO_PAGE_HE

__all__ = ["build_guard_demo_router"]

_DEMO_FIXTURE_PATH = Path(__file__).parent / "static" / "guard_demo_scenarios.json"


def _headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'unsafe-inline' 'self'; "
            "style-src 'unsafe-inline' 'self'; connect-src 'self'"
        ),
    }


def _load_fixture() -> dict[str, Any]:
    try:
        payload = json.loads(_DEMO_FIXTURE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Guard demo fixture is unavailable") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("scenarios"), list):
        raise TypeError("Guard demo fixture has an invalid shape")
    return payload


def build_guard_demo_router() -> APIRouter:
    """Build the unauthenticated, static Guard demo router."""
    router = APIRouter()

    @router.get("/demo/guard", response_class=HTMLResponse)
    async def guard_demo_page() -> HTMLResponse:
        return HTMLResponse(content=GUARD_DEMO_PAGE, headers=_headers())

    @router.get("/demo/guard/he", response_class=HTMLResponse)
    async def guard_demo_page_hebrew() -> HTMLResponse:
        return HTMLResponse(content=GUARD_DEMO_PAGE_HE, headers=_headers())

    @router.get("/api/demo/guard")
    async def guard_demo_data() -> JSONResponse:
        return JSONResponse(content=_load_fixture(), headers=_headers())

    return router
