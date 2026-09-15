"""FastAPI routes for the public landing page.

* ``GET /`` — serves the landing page HTML (with a live signup counter
  injected server-side, shown only above a display threshold).
* ``GET /og.png`` — Open Graph share image for WhatsApp/social previews.
* ``POST /api/waitlist`` — waitlist signup (name + phone + optional
  willingness-to-pay signal).

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
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from echo_v2.app.landing_page import LANDING_PAGE
from echo_v2.persistence.identity import PhoneParseError, normalize_phone_e164
from echo_v2.persistence.waitlist import WaitlistRepository
from echo_v2.services.waitlist_notifier import WaitlistNotifier

__all__ = ["build_landing_router"]

_logger = logging.getLogger("echo_v2.app.landing_routes")

# Rate limit: max signup attempts per IP per window.
_RATE_LIMIT_MAX = 10
_RATE_LIMIT_WINDOW = 60  # seconds

# The live counter is shown only once the list is at least this long —
# an empty counter hurts conversion more than no counter.
_COUNTER_DISPLAY_THRESHOLD = 25

_OG_IMAGE_PATH = Path(__file__).parent / "static" / "og.png"


class WaitlistRequest(BaseModel):
    """Request body for POST /api/waitlist."""

    name: str = Field(..., min_length=1, max_length=80, description="Full name")
    phone: str = Field(..., min_length=6, max_length=20, description="Phone number")
    wtp: Literal["free", "under_30", "30_70", "70_120", "120_plus"] | None = Field(
        None, description="Optional willingness-to-pay signal"
    )

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be empty")
        return v


def build_landing_router(
    *,
    waitlist_repo: WaitlistRepository,
    base_url: str = "",
    notifier: WaitlistNotifier | None = None,
) -> APIRouter:
    """Build the landing page router.

    Args:
        waitlist_repo: The :class:`WaitlistRepository` for signups.
        base_url: Public base URL (for absolute Open Graph URLs).
        notifier: Optional :class:`WaitlistNotifier` invoked on each *new*
            signup. Failures are logged and never break the signup.
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
                "style-src 'unsafe-inline' 'self'; "
                "img-src 'self' data:"
            ),
        }

    @router.get("/", response_class=HTMLResponse)
    async def landing() -> HTMLResponse:
        count = await waitlist_repo.count()
        counter_html = ""
        if count >= _COUNTER_DISPLAY_THRESHOLD:
            counter_html = (
                f'<div class="counter">🔥 {count} כבר ברשימה</div>'
            )
        html = (
            LANDING_PAGE
            .replace("{{COUNTER}}", counter_html)
            .replace("{{BASE_URL}}", base_url.rstrip("/"))
        )
        return HTMLResponse(content=html, headers=_security_headers())

    @router.get("/og.png")
    async def og_image() -> FileResponse:
        if not _OG_IMAGE_PATH.exists():
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(
            _OG_IMAGE_PATH,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @router.post("/api/waitlist")
    async def waitlist_signup(body: WaitlistRequest, request: Request) -> JSONResponse:
        client_ip = request.client.host if request.client else "unknown"
        if not _check_rate_limit(client_ip):
            raise HTTPException(status_code=429, detail="rate limited")

        try:
            phone_e164 = normalize_phone_e164(body.phone)
        except PhoneParseError:
            raise HTTPException(status_code=422, detail="invalid phone number")

        inserted = await waitlist_repo.add(
            name=body.name,
            phone_number=phone_e164,
            willingness_to_pay=body.wtp,
        )
        # Duplicates get the same response as new signups — the endpoint
        # must not leak whether a number is already on the list.
        if inserted:
            _logger.info("waitlist: new signup")
            if notifier is not None:
                try:
                    await notifier.notify(
                        name=body.name,
                        phone=phone_e164,
                        willingness_to_pay=body.wtp,
                    )
                except Exception:
                    _logger.exception("waitlist: notifier failed")
        return JSONResponse(
            content={"status": "ok"},
            headers={**_security_headers(), "Cache-Control": "no-store"},
        )

    return router
