"""Ops-alert routes: connector digest ingestion + admin browsing.

Two auth mechanisms, one router:

* ``POST /internal/ops-alerts`` — the echo-whatsapp-connector's ops log
  monitor POSTs its hourly digest here, authenticated with a shared
  bearer token (constant-time compare against ``OPS_ALERT_TOKEN``).
  Persisted only; nothing is pushed to users (Option B: table + history).
* ``GET /api/admin/guard/ops/digests`` — the admin health dashboard reads
  the digest history, authed with the same waiting-list session cookie
  and admin user id set as the other admin routes.

The connector's payload contract (see its ``monitor/log-monitor.ts``):
``{severity, title, body, source, worker_id, timestamp, metrics?,
window?}`` where ``body`` is the JSON-encoded digest (happened /
resolved / still_open / one_liner).
"""

from __future__ import annotations

import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response

from echo_v2.domain.ops_digest import OpsDigest
from echo_v2.persistence.ops_digests import OpsDigestRepository
from echo_v2.services.waiting_list_token_service import WaitingListTokenService

_SESSION_COOKIE = "wls"
_PERIODS = {"24h": timedelta(hours=24), "7d": timedelta(days=7)}
_MAX_BODY_BYTES = 256 * 1024


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


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    raise HTTPException(status_code=400, detail="invalid timestamp")


def build_ops_alert_router(
    *,
    repository: OpsDigestRepository,
    token_service: WaitingListTokenService,
    admin_user_ids: set[str] | frozenset[str] = frozenset(),
    alert_token: str = "",
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

    def _require_alert_token(authorization: str | None) -> None:
        if not alert_token:
            raise HTTPException(status_code=503, detail="ops alerts not configured")
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        candidate = authorization[len("Bearer "):]
        if not hmac.compare_digest(candidate, alert_token):
            raise HTTPException(status_code=401, detail="invalid bearer token")

    @router.post("/internal/ops-alerts", status_code=204)
    async def ingest_ops_alert(request: Request) -> Response:
        _require_alert_token(request.headers.get("authorization"))
        raw = await request.body()
        if len(raw) > _MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="payload too large")
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="invalid JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="payload must be an object")

        severity = payload.get("severity")
        title = payload.get("title")
        body = payload.get("body")
        worker_id = payload.get("worker_id")
        source = payload.get("source", "connector")
        if (
            not isinstance(severity, str)
            or not severity
            or not isinstance(title, str)
            or not title
            or not isinstance(worker_id, str)
            or not worker_id
            or not isinstance(body, str)
        ):
            raise HTTPException(status_code=400, detail="missing required fields")

        try:
            digest = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="digest body is not JSON") from exc
        if not isinstance(digest, dict) or not isinstance(digest.get("one_liner"), str):
            raise HTTPException(status_code=400, detail="digest body missing one_liner")

        received_at = _parse_timestamp(payload.get("timestamp"))
        window = payload.get("window")
        window_from = (
            _parse_timestamp(window.get("from"))
            if isinstance(window, dict) and window.get("from")
            else None
        )
        window_to = (
            _parse_timestamp(window.get("to"))
            if isinstance(window, dict) and window.get("to")
            else None
        )
        metrics = payload.get("metrics")
        if metrics is not None and not isinstance(metrics, dict):
            raise HTTPException(status_code=400, detail="metrics must be an object")

        await repository.record(
            OpsDigest(
                digest_key=f"{worker_id}:{window_to.isoformat() if window_to else received_at.isoformat()}",
                worker_id=worker_id,
                source=source if isinstance(source, str) and source else "connector",
                severity=severity,
                attention=digest.get("attention") is True or severity == "high",
                one_liner=digest["one_liner"],
                digest=digest,
                metrics=metrics,
                window_from=window_from,
                window_to=window_to,
                received_at=received_at,
            )
        )
        # Plain Response: JSONResponse(content=None) serializes the body to
        # b"null", which violates the bodiless 204 contract and aborts the
        # connection mid-send ("Response content longer than Content-Length").
        return Response(status_code=204, headers=_headers())

    @router.get("/api/admin/guard/ops/digests")
    async def list_ops_digests(
        period: str = "24h",
        wls: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
    ) -> JSONResponse:
        await _require_admin(wls)
        window = _PERIODS.get(period)
        if window is None:
            raise HTTPException(status_code=400, detail="period must be 24h or 7d")
        since = datetime.now(timezone.utc) - window
        digests = await repository.list_since(since=since, limit=100)
        return JSONResponse(
            content=jsonable_encoder(
                {
                    "period": period,
                    "digests": [
                        {
                            "received_at": d.received_at,
                            "worker_id": d.worker_id,
                            "severity": d.severity,
                            "attention": d.attention,
                            "one_liner": d.one_liner,
                            "still_open": d.digest.get("still_open", []),
                            "resolved": d.digest.get("resolved", []),
                            "happened": d.digest.get("happened", []),
                            "window_from": d.window_from,
                            "window_to": d.window_to,
                            "metrics": d.metrics,
                        }
                        for d in digests
                    ],
                }
            ),
            headers=_headers(),
        )

    return router
