"""FastAPI routes for the waiting-list mini web app.

Two route groups:
* ``GET /q/{token}`` — one-time token exchange. Validates the raw
  token, sets a Secure HttpOnly SameSite cookie, serves the HTML page.
  If the token is invalid/expired, serves the expired-link page.
* ``GET /api/waiting`` — cookie-authenticated JSON API.
* ``POST /api/waiting/items/{active_id}/actions`` — execute an action.

Security:
* The raw token never appears in API paths — only in the initial
  ``GET /q/{token}`` which is redacted in access logs.
* The cookie is ``Secure; HttpOnly; SameSite=Lax; Path=/api/waiting``.
* Response headers: ``Cache-Control: no-store``,
  ``Referrer-Policy: no-referrer``,
  ``X-Content-Type-Options: nosniff``, restrictive CSP.
* Rate limiting per ``session_id`` (hashed, in-memory counter).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from echo_v2.app.waiting_list_page import EXPIRED_LINK_PAGE, WAITING_LIST_PAGE
from echo_v2.services.waiting_list_action_service import WaitingListActionService
from echo_v2.services.waiting_list_token_service import WaitingListTokenService

__all__ = ["build_waiting_list_router"]

_logger = logging.getLogger("echo_v2.app.waiting_list_routes")

# Cookie name for the session id.
_SESSION_COOKIE = "wls"
# Cookie max age (48h, matching session TTL).
_COOKIE_MAX_AGE = 172800

# Rate limit: max actions per minute per session.
_RATE_LIMIT_MAX = 60
_RATE_LIMIT_WINDOW = 60  # seconds


class ActionRequest(BaseModel):
    """Request body for POST /api/waiting/items/{active_id}/actions."""

    action_id: str = Field(..., description="Client-generated UUID for idempotency")
    expected_version: int = Field(..., description="The target_version the client saw")
    action: str = Field(..., description="Action: done, snooze, or dismiss")
    snooze_preset: str | None = Field(None, description="Snooze preset: morning/afternoon/evening/tomorrow")
    snooze_until: str | None = Field(None, description="ISO 8601 datetime for custom snooze")
    dismiss_reason: str | None = Field(None, description="Dismiss reason: already_handled/no_response_required/detected_incorrectly")


class SendRequest(BaseModel):
    """Request body for POST /api/waiting/items/{active_id}/send.

    Schedules a WhatsApp message to the contact of the waiting item.
    The recipient (``chat_id``) is resolved server-side from the
    ``active_id`` — never trusts a client-supplied phone number.

    Idempotency: the browser generates ``request_id`` once when the user
    first submits and reuses it for retries. The server derives a
    deterministic action id from ``user_id + request_id`` so a retry
    returns the original action instead of creating a duplicate.
    """

    request_id: UUID = Field(..., description="Client-generated UUID for idempotency")
    message: str = Field(..., min_length=1, max_length=1000, description="Message body")
    send_preset: Literal[
        "10m", "1h", "3h", "morning", "afternoon", "evening", "tomorrow"
    ] | None = Field(None, description="Time preset for scheduling")
    send_at: datetime | None = Field(
        None, description="ISO 8601 offset-aware datetime for custom scheduling"
    )

    @field_validator("message")
    @classmethod
    def _strip_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("message must not be empty")
        return v

    @model_validator(mode="after")
    def _exactly_one_timing(self) -> SendRequest:
        if (self.send_preset is None) == (self.send_at is None):
            raise ValueError("exactly one of send_preset or send_at must be set")
        return self


class StarRequest(BaseModel):
    """Request body for POST /api/waiting/items/{active_id}/star.

    Explicit (not toggle): the client sends the desired ``is_starred``
    value. This is idempotent — retrying the same value is a no-op.
    """

    is_starred: bool = Field(..., description="True to star, False to unstar")


class LabelRequest(BaseModel):
    """Request body for POST /api/waiting/items/{active_id}/label.

    Explicit: the client sends the desired ``color_label`` value.
    ``None`` clears the label. This is idempotent.
    """

    color_label: str | None = Field(
        None,
        description="Color label: red, yellow, green, blue, purple, or null to clear",
        pattern=r"^(red|yellow|green|blue|purple)$",
    )


class TagsRequest(BaseModel):
    """Request body for POST /api/waiting/items/{active_id}/tags.

    Explicit: the client sends the full desired tags list. This is
    idempotent — retrying the same list is a no-op.
    """

    tags: list[str] = Field(
        ...,
        max_length=10,
        description="Full tags list (max 10 tags, each max 40 chars)",
    )

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, v: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for tag in v:
            t = tag.strip()[:40]
            if not t:
                continue
            key = t.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(t)
            if len(result) >= 10:
                break
        return result


def build_waiting_list_router(
    *,
    token_service: WaitingListTokenService,
    waiting_list_service: WaitingListActionService,
    bot_phone: str,
) -> APIRouter:
    """Build the waiting-list mini web app router.

    Args:
        token_service: The :class:`WaitingListTokenService` for token
            validation and cookie exchange.
        waiting_list_service: The :class:`WaitingListActionService` for
            listing items and executing actions.
        bot_phone: The Echo bot's WhatsApp phone number (for the
            "back to WhatsApp" link).
    """
    router = APIRouter()

    # In-memory rate limiter: session_id -> [timestamp, ...].
    _rate_limiter: dict[str, list[float]] = {}

    def _check_rate_limit(session_id: str) -> bool:
        """Check if the session is within the rate limit. Returns True if OK."""
        now = time.time()
        window_start = now - _RATE_LIMIT_WINDOW
        hits = _rate_limiter.get(session_id, [])
        hits = [t for t in hits if t > window_start]
        if len(hits) >= _RATE_LIMIT_MAX:
            _rate_limiter[session_id] = hits
            return False
        hits.append(now)
        _rate_limiter[session_id] = hits
        return True

    def _security_headers() -> dict[str, str]:
        return {
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                "default-src 'self'; "
                "script-src 'unsafe-inline' 'self'; "
                "style-src 'unsafe-inline' 'self'"
            ),
        }

    # --- GET /q/{token} — one-time token exchange + HTML page ---

    @router.get("/q/{token}", response_class=HTMLResponse)
    async def waiting_list_page(token: str) -> HTMLResponse:
        # Validate the token (one-time).
        resolved = await token_service.resolve(token)
        if resolved is None:
            # Serve the expired-link page.
            html = EXPIRED_LINK_PAGE.replace(
                "{{BOT_PHONE_LINK}}",
                f"https://wa.me/{bot_phone}?text={_url_encode('סיכום חדש בבקשה')}",
            )
            return HTMLResponse(content=html, headers=_security_headers())

        # Set the cookie and serve the page.
        html = WAITING_LIST_PAGE.replace("{{BOT_PHONE}}", bot_phone)
        headers = _security_headers()
        response = HTMLResponse(content=html, headers=headers)
        response.set_cookie(
            key=_SESSION_COOKIE,
            value=resolved.session_id,
            max_age=_COOKIE_MAX_AGE,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/api/waiting",
        )
        return response

    # --- GET /q/expired — explicit expired page (for API 401 redirects) ---

    @router.get("/q/expired", response_class=HTMLResponse)
    async def expired_page() -> HTMLResponse:
        html = EXPIRED_LINK_PAGE.replace(
            "{{BOT_PHONE_LINK}}",
            f"https://wa.me/{bot_phone}?text={_url_encode('סיכום חדש בבקשה')}",
        )
        return HTMLResponse(content=html, headers=_security_headers())

    # --- GET /api/waiting — list items ---

    @router.get("/api/waiting")
    async def list_items(
        request: Request,
        wls: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
    ) -> JSONResponse:
        if wls is None:
            raise HTTPException(status_code=401, detail="no session")

        resolved = await token_service.resolve_session(wls)
        if resolved is None:
            raise HTTPException(status_code=401, detail="session expired")

        if not _check_rate_limit(resolved.session_id):
            raise HTTPException(status_code=429, detail="rate limited")

        result = await waiting_list_service.list_items(
            session_id=resolved.session_id,
            user_id=resolved.user_id,
        )
        if result is None:
            raise HTTPException(status_code=401, detail="session invalid")

        return JSONResponse(
            content={
                "items": [
                    {
                        "active_id": item.active_id,
                        "contact_name": item.contact_name,
                        "situation_summary": item.situation_summary,
                        "message_preview": item.message_preview,
                        "waiting_since": item.waiting_since.isoformat(),
                        "waiting_hours": item.waiting_hours,
                        "expected_version": item.expected_version,
                        "is_starred": item.is_starred,
                        "color_label": item.color_label,
                        "tags": item.tags,
                    }
                    for item in result.items
                ],
                "summary": {
                    "waiting": result.summary.waiting,
                    "snoozed": result.summary.snoozed,
                    "completed": result.summary.completed,
                },
            },
            headers=_security_headers(),
        )

    # --- GET /api/waiting/items/{active_id}/context — read-only context ---

    @router.get("/api/waiting/items/{active_id}/context")
    async def get_context(
        active_id: str,
        limit: int = 8,
        wls: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
    ) -> JSONResponse:
        if wls is None:
            raise HTTPException(status_code=401, detail="no session")

        resolved = await token_service.resolve_session(wls)
        if resolved is None:
            raise HTTPException(status_code=401, detail="session expired")

        if not _check_rate_limit(resolved.session_id):
            raise HTTPException(status_code=429, detail="rate limited")

        result = await waiting_list_service.get_context(
            session_id=resolved.session_id,
            user_id=resolved.user_id,
            active_id=active_id,
            limit=limit,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="item not found")

        return JSONResponse(
            content={
                "messages": [
                    {
                        "direction": message.direction,
                        "timestamp": message.timestamp.isoformat(),
                        "text": message.text,
                        "message_type": message.message_type,
                    }
                    for message in result
                ]
            },
            headers=_security_headers(),
        )

    # --- POST /api/waiting/items/{active_id}/actions — execute action ---

    @router.post("/api/waiting/items/{active_id}/actions")
    async def execute_action(
        active_id: str,
        body: ActionRequest,
        wls: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
    ) -> JSONResponse:
        if wls is None:
            raise HTTPException(status_code=401, detail="no session")

        resolved = await token_service.resolve_session(wls)
        if resolved is None:
            raise HTTPException(status_code=401, detail="session expired")

        if not _check_rate_limit(resolved.session_id):
            raise HTTPException(status_code=429, detail="rate limited")

        # Parse snooze_until if provided.
        snooze_until_dt: datetime | None = None
        if body.snooze_until:
            try:
                snooze_until_dt = datetime.fromisoformat(body.snooze_until)
                if snooze_until_dt.tzinfo is None:
                    snooze_until_dt = snooze_until_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                raise HTTPException(status_code=422, detail="invalid snooze_until format")

        result = await waiting_list_service.execute_action(
            session_id=resolved.session_id,
            user_id=resolved.user_id,
            active_id=active_id,
            action_id=body.action_id,
            expected_version=body.expected_version,
            action=body.action,
            snooze_preset=body.snooze_preset,
            snooze_until=snooze_until_dt,
            dismiss_reason=body.dismiss_reason,
        )
        if result is None:
            raise HTTPException(status_code=401, detail="session invalid")

        status_code = 200
        if result.outcome == "stale":
            status_code = 409
        elif result.outcome == "not_found":
            status_code = 404
        elif result.outcome == "invalid_snooze":
            status_code = 422

        item_data = None
        if result.item:
            item_data = {
                "active_id": result.item.active_id,
                "contact_name": result.item.contact_name,
                "message_preview": result.item.message_preview,
                "waiting_since": result.item.waiting_since.isoformat(),
                "waiting_hours": result.item.waiting_hours,
                "expected_version": result.item.expected_version,
            }

        return JSONResponse(
            status_code=status_code,
            content={
                "outcome": result.outcome,
                "action": result.action,
                "item": item_data,
                "summary": {
                    "waiting": result.summary.waiting,
                    "snoozed": result.summary.snoozed,
                    "completed": result.summary.completed,
                },
            },
            headers=_security_headers(),
        )

    # --- POST /api/waiting/items/{active_id}/send — schedule a message ---

    @router.post("/api/waiting/items/{active_id}/send")
    async def schedule_send(
        active_id: str,
        body: SendRequest,
        wls: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
    ) -> JSONResponse:
        if wls is None:
            raise HTTPException(status_code=401, detail="no session")

        resolved = await token_service.resolve_session(wls)
        if resolved is None:
            raise HTTPException(status_code=401, detail="session expired")

        if not _check_rate_limit(resolved.session_id):
            raise HTTPException(status_code=429, detail="rate limited")

        result = await waiting_list_service.schedule_send(
            session_id=resolved.session_id,
            user_id=resolved.user_id,
            active_id=active_id,
            request_id=str(body.request_id),
            message=body.message,
            send_preset=body.send_preset,
            send_at=body.send_at,
        )
        if result is None:
            raise HTTPException(status_code=401, detail="session invalid")

        status_code = 200
        if result.outcome == "not_found":
            status_code = 404
        elif result.outcome == "invalid":
            status_code = 422

        return JSONResponse(
            status_code=status_code,
            content={
                "outcome": result.outcome,
                "scheduled_for": result.scheduled_for,
                "action_id": result.action_id,
            },
            headers=_security_headers(),
        )

    # --- POST /api/waiting/items/{active_id}/star — star/unstar contact ---

    @router.post("/api/waiting/items/{active_id}/star")
    async def set_starred(
        active_id: str,
        body: StarRequest,
        wls: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
    ) -> JSONResponse:
        if wls is None:
            raise HTTPException(status_code=401, detail="no session")

        resolved = await token_service.resolve_session(wls)
        if resolved is None:
            raise HTTPException(status_code=401, detail="session expired")

        if not _check_rate_limit(resolved.session_id):
            raise HTTPException(status_code=429, detail="rate limited")

        result = await waiting_list_service.set_starred(
            session_id=resolved.session_id,
            user_id=resolved.user_id,
            active_id=active_id,
            is_starred=body.is_starred,
        )
        if result is None:
            raise HTTPException(status_code=401, detail="session invalid")

        status_code = 200
        if result.outcome == "not_found":
            status_code = 404

        return JSONResponse(
            status_code=status_code,
            content={
                "outcome": result.outcome,
                "is_starred": result.is_starred,
            },
            headers=_security_headers(),
        )

    # --- POST /api/waiting/items/{active_id}/label — set color label ---

    @router.post("/api/waiting/items/{active_id}/label")
    async def set_label(
        active_id: str,
        body: LabelRequest,
        wls: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
    ) -> JSONResponse:
        if wls is None:
            raise HTTPException(status_code=401, detail="no session")

        resolved = await token_service.resolve_session(wls)
        if resolved is None:
            raise HTTPException(status_code=401, detail="session expired")

        if not _check_rate_limit(resolved.session_id):
            raise HTTPException(status_code=429, detail="rate limited")

        result = await waiting_list_service.set_label(
            session_id=resolved.session_id,
            user_id=resolved.user_id,
            active_id=active_id,
            color_label=body.color_label,
        )
        if result is None:
            raise HTTPException(status_code=401, detail="session invalid")

        status_code = 200
        if result.outcome == "not_found":
            status_code = 404

        return JSONResponse(
            status_code=status_code,
            content={
                "outcome": result.outcome,
                "color_label": result.color_label,
            },
            headers=_security_headers(),
        )

    # --- POST /api/waiting/items/{active_id}/tags — set tags ---

    @router.post("/api/waiting/items/{active_id}/tags")
    async def set_tags(
        active_id: str,
        body: TagsRequest,
        wls: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
    ) -> JSONResponse:
        if wls is None:
            raise HTTPException(status_code=401, detail="no session")

        resolved = await token_service.resolve_session(wls)
        if resolved is None:
            raise HTTPException(status_code=401, detail="session expired")

        if not _check_rate_limit(resolved.session_id):
            raise HTTPException(status_code=429, detail="rate limited")

        result = await waiting_list_service.set_tags(
            session_id=resolved.session_id,
            user_id=resolved.user_id,
            active_id=active_id,
            tags=body.tags,
        )
        if result is None:
            raise HTTPException(status_code=401, detail="session invalid")

        status_code = 200
        if result.outcome == "not_found":
            status_code = 404

        return JSONResponse(
            status_code=status_code,
            content={
                "outcome": result.outcome,
                "tags": result.tags,
            },
            headers=_security_headers(),
        )

    # --- GET /api/waiting/tags — list all tags for autocomplete ---

    @router.get("/api/waiting/tags")
    async def list_tags(
        wls: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
    ) -> JSONResponse:
        if wls is None:
            raise HTTPException(status_code=401, detail="no session")

        resolved = await token_service.resolve_session(wls)
        if resolved is None:
            raise HTTPException(status_code=401, detail="session expired")

        if not _check_rate_limit(resolved.session_id):
            raise HTTPException(status_code=429, detail="rate limited")

        tags = await waiting_list_service.list_tags(
            session_id=resolved.session_id,
            user_id=resolved.user_id,
        )
        if tags is None:
            raise HTTPException(status_code=401, detail="session invalid")

        return JSONResponse(
            content={"tags": tags},
            headers=_security_headers(),
        )

    return router


def _url_encode(text: str) -> str:
    """URL-encode Hebrew text for wa.me links."""
    from urllib.parse import quote

    return quote(text)
