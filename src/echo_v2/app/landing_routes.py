"""FastAPI routes for the public landing page.

* ``GET /`` — serves the landing page HTML.
* ``POST /api/waitlist`` — waitlist signup (name + phone).

Security:
* Phone numbers are normalized to canonical E.164 before storage; invalid
  numbers are rejected with 422.
* Signups are deduplicated by phone. A duplicate submission returns the
  same success response as a new one, so the endpoint never leaks whether
  a number is already on the list.
* In-memory rate limiting per client IP (the endpoint is unauthenticated
  and public).
* Standard security headers (no-store, nosniff, restrictive CSP).
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from echo_v2.app.landing_page import LANDING_PAGE
from echo_v2.persistence.identity import PhoneParseError, normalize_phone_e164
from echo_v2.persistence.waitlist import WaitlistRepository

__all__ = ["build_landing_router"]

_logger = logging.getLogger("echo_v2.app.landing_routes")

# Rate limit: max signup attempts per IP per window.
_RATE_LIMIT_MAX = 10
_RATE_LIMIT_WINDOW = 60  # seconds


class WaitlistRequest(BaseModel):
    """Request body for POST /api/waitlist."""

    name: str = Field(..., min_length=1, max_length=80, description="Full name")
    phone: str = Field(..., min_length=6, max_length=20, description="Phone number")

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be empty")
        return v


def build_landing_router(*, waitlist_repo: WaitlistRepository) -> APIRouter:
    """Build the landing page router.

    Args:
        waitlist_repo: The :class:`WaitlistRepository` for signups.
    """
    router = APIRouter()

    # In-memory rate limiter: client_ip -> [timestamp, ...].
    _rate_limiter: dict[str, list[float]] = {}

    def _check_rate_limit(client_ip: str) -> bool:
        now = time.time()
        window_start = now - _RATE_LIMIT_WINDOW
        hits = [t for t in _rate_limiter.get(client_ip, []) if t > window_start]
        if len(hits) >= _RATE_LIMIT_MAX:
            _rate_limiter[client_ip] = hits
            return False
        hits.append(now)
        _rate_limiter[client_ip] = hits
        return True

    def _security_headers() -> dict[str, str]:
        return {
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'self'; "
                "script-src 'unsafe-inline' 'self'; "
                "style-src 'unsafe-inline' 'self'"
            ),
        }

    @router.get("/", response_class=HTMLResponse)
    async def landing() -> HTMLResponse:
        return HTMLResponse(content=LANDING_PAGE, headers=_security_headers())

    @router.post("/api/waitlist")
    async def waitlist_signup(body: WaitlistRequest, request: Request) -> JSONResponse:
        client_ip = request.client.host if request.client else "unknown"
        if not _check_rate_limit(client_ip):
            raise HTTPException(status_code=429, detail="rate limited")

        try:
            phone_e164 = normalize_phone_e164(body.phone)
        except PhoneParseError:
            raise HTTPException(status_code=422, detail="invalid phone number")

        inserted = await waitlist_repo.add(name=body.name, phone_number=phone_e164)
        # Duplicates get the same response as new signups — the endpoint
        # must not leak whether a number is already on the list.
        if inserted:
            _logger.info("waitlist: new signup")
        return JSONResponse(
            content={"status": "ok"},
            headers={**_security_headers(), "Cache-Control": "no-store"},
        )

    return router
