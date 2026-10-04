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
from echo_v2.app.landing_page_echo_guard_clean import LANDING_PAGE as GUARD_LANDING_PAGE
from echo_v2.persistence.identity import PhoneParseError, normalize_phone_e164
from echo_v2.persistence.waitlist import WaitlistRepository
from echo_v2.services.waitlist_notifier import WaitlistNotifier

__all__ = ["build_landing_router"]

_logger = logging.getLogger("echo_v2.app.landing_routes")

# Public waitlist abuse controls. These are process-local limits; production
# deployments with multiple instances should move them to shared storage.
_IP_SHORT_LIMIT = 5
_IP_SHORT_WINDOW = 10 * 60  # 10 minutes
_IP_DAILY_LIMIT = 20
_IP_DAILY_WINDOW = 24 * 60 * 60
_PHONE_LIMIT = 2
_PHONE_WINDOW = 60 * 60  # 1 hour

# The live counter is shown only once the list is at least this long —
# an empty counter hurts conversion more than no counter.
_COUNTER_DISPLAY_THRESHOLD = 25

_OG_IMAGE_PATH = Path(__file__).parent / "static" / "og.png"
_HEBREW_LANDING_PATH = Path(__file__).resolve().parents[3] / "root" / "he" / "index.html"
# The source asset has no extension but is a valid PNG; keep the public URL
# versioned so future artwork can use og-guard-v3.png without cache ambiguity.
_OG_GUARD_IMAGE_PATH = Path(__file__).parent / "static" / "og.guard.png"


class WaitlistRequest(BaseModel):
    """Request body for POST /api/waitlist."""

    name: str = Field(..., min_length=1, max_length=80, description="Full name")
    phone: str = Field(..., min_length=6, max_length=20, description="Phone number")
    wtp: Literal["free", "under_30", "30_70", "70_120", "120_plus"] | None = Field(
        None, description="Optional willingness-to-pay signal"
    )
    # Echo Guard pilot fields (optional — only sent from the /guard landing page).
    # Not yet persisted; used only to format the owner's WhatsApp notification.
    children_count: str | None = Field(
        None, max_length=16, description="Echo Guard: number of children to enroll"
    )
    children_ages: str | None = Field(
        None, max_length=120, description="Echo Guard: free-text ages of children"
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

    # In-memory limiter state. Each list contains timestamps for accepted
    # attempts and is pruned when that key is checked.
    _ip_attempts: dict[str, list[float]] = {}
    _phone_attempts: dict[str, list[float]] = {}

    def _check_rate_limits(client_ip: str, phone_e164: str) -> str | None:
        now = time.time()
        ip_hits = [
            timestamp
            for timestamp in _ip_attempts.get(client_ip, [])
            if timestamp > now - _IP_DAILY_WINDOW
        ]
        phone_hits = [
            timestamp
            for timestamp in _phone_attempts.get(phone_e164, [])
            if timestamp > now - _PHONE_WINDOW
        ]
        short_ip_hits = [
            timestamp for timestamp in ip_hits if timestamp > now - _IP_SHORT_WINDOW
        ]

        if len(short_ip_hits) >= _IP_SHORT_LIMIT:
            return "ip short window limit exceeded"
        if len(ip_hits) >= _IP_DAILY_LIMIT:
            return "ip daily limit exceeded"
        if len(phone_hits) >= _PHONE_LIMIT:
            return "phone limit exceeded"

        _ip_attempts[client_ip] = [*ip_hits, now]
        _phone_attempts[phone_e164] = [*phone_hits, now]
        return None

    def _client_ip(request: Request) -> str:
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            # Render/Cloudflare put the original client first. Ignore any
            # extra values rather than allowing unbounded header input in logs.
            return forwarded_for.split(",", 1)[0].strip()[:128] or "unknown"
        return request.client.host if request.client else "unknown"

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
    async def landing(request: Request) -> HTMLResponse:
        raw_country = request.headers.get("cf-ipcountry", "").strip().upper()
        country = raw_country[:16] if raw_country.isalnum() else "invalid"
        language = "he" if country == "IL" else "en"
        _logger.info(
            "landing request: country=%s cf_ray_present=%s language=%s",
            country or "missing",
            bool(request.headers.get("cf-ray")),
            language,
        )

        count = await waitlist_repo.count()
        # Keep the early-access form useful before the list has real volume;
        # once the live list reaches the threshold, show its actual count.
        display_count = max(count, 50) if count < _COUNTER_DISPLAY_THRESHOLD else count
        if language == "he":
            counter_html = f'<div class="counter">🔥 כבר {display_count} אנשים ברשימה</div>'
            page = _HEBREW_LANDING_PATH.read_text(encoding="utf-8")
        else:
            counter_html = f'<div class="counter">🔥 {display_count} early-access spots</div>'
            page = LANDING_PAGE
        html = (
            page.replace("{{COUNTER}}", counter_html)
            .replace("{{BASE_URL}}", base_url.rstrip("/"))
        )
        return HTMLResponse(content=html, headers=_security_headers())

    @router.get("/guard", response_class=HTMLResponse)
    async def guard_landing() -> HTMLResponse:
        count = await waitlist_repo.count()
        counter_html = ""
        if count >= _COUNTER_DISPLAY_THRESHOLD:
            counter_html = f'<div class="counter">🔥 {count} כבר ברשימה</div>'
        html = (
            GUARD_LANDING_PAGE
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

    @router.get("/og-guard-v2.png")
    async def og_guard_image() -> FileResponse:
        if not _OG_GUARD_IMAGE_PATH.exists():
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(
            _OG_GUARD_IMAGE_PATH,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @router.post("/api/waitlist")
    async def waitlist_signup(body: WaitlistRequest, request: Request) -> JSONResponse:
        client_ip = _client_ip(request)
        cf_ray = request.headers.get("cf-ray", "-")[:128]
        try:
            phone_e164 = normalize_phone_e164(body.phone)
        except PhoneParseError:
            _logger.warning(
                "waitlist: invalid phone from ip=%s cf_ray=%s", client_ip, cf_ray
            )
            raise HTTPException(status_code=422, detail="invalid phone number")

        _logger.info(
            "waitlist: signup request ip=%s cf_ray=%s name=%r phone=%r wtp=%r "
            "children_count=%r children_ages=%r",
            client_ip, cf_ray, body.name, phone_e164, body.wtp,
            body.children_count, body.children_ages,
        )
        rate_limit_reason = _check_rate_limits(client_ip, phone_e164)
        if rate_limit_reason is not None:
            _logger.warning(
                "waitlist: rate limited reason=%s ip=%s phone=%s cf_ray=%s",
                rate_limit_reason, client_ip, phone_e164, cf_ray,
            )
            raise HTTPException(status_code=429, detail="rate limited")

        inserted = await waitlist_repo.add(
            name=body.name,
            phone_number=phone_e164,
            willingness_to_pay=body.wtp,
        )
        # Duplicates get the same response as new signups — the endpoint
        # must not leak whether a number is already on the list.
        if inserted:
            _logger.info("waitlist: new signup stored for %r (%s)", body.name, phone_e164)
            if notifier is None:
                _logger.warning("waitlist: notifier is None (ECHO_OWNER_PHONE not set?)")
            else:
                _logger.info("waitlist: calling notifier for %r (%s)", body.name, phone_e164)
                try:
                    await notifier.notify(
                        name=body.name,
                        phone=phone_e164,
                        willingness_to_pay=body.wtp,
                        children_count=body.children_count,
                        children_ages=body.children_ages,
                    )
                    _logger.info("waitlist: notifier returned without error for %r", body.name)
                except Exception:
                    _logger.exception("waitlist: notifier failed for %r (%s)", body.name, phone_e164)
        else:
            _logger.info("waitlist: duplicate signup for %s (notifier skipped)", phone_e164)
        return JSONResponse(
            content={"status": "ok"},
            headers={**_security_headers(), "Cache-Control": "no-store"},
        )

    return router
