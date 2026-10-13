"""Public deterministic Guard demo replay routes."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from echo_v2.app.guard_demo_page import GUARD_DEMO_PAGE
from echo_v2.app.guard_demo_page_he import GUARD_DEMO_PAGE_HE
from echo_v2.domain.guard_eval import GuardEvalJudgmentLabel
from echo_v2.services.guard_live_compare_service import GuardLiveCompareService

__all__ = ["build_guard_demo_router"]

_REVIEWER_COOKIE = "guard_eval_reviewer"
_DEMO_FIXTURE_PATH = Path(__file__).parent / "static" / "guard_demo_scenarios.json"
_DEMO_FIXTURE_PATH_HE = Path(__file__).parent / "static" / "guard_demo_scenarios_he.json"


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


def _load_fixture(path: Path = _DEMO_FIXTURE_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Guard demo fixture is unavailable") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("scenarios"), list):
        raise TypeError("Guard demo fixture has an invalid shape")
    return payload


class LiveRunRequest(BaseModel):
    scenario_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)


class JudgmentRequest(BaseModel):
    snapshot_id: str = Field(min_length=1, max_length=128)
    label: GuardEvalJudgmentLabel
    note: str | None = Field(default=None, max_length=2000)
    reason: str | None = Field(default=None, max_length=2000)


def _requester_key(request: Request) -> str:
    host = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        host = forwarded.split(",", 1)[0].strip()[:128] or host
    salt = os.environ.get("GUARD_EVAL_RATE_LIMIT_KEY", "echo-guard-eval-rate-limit")
    return hmac.new(salt.encode(), host.encode(), hashlib.sha256).hexdigest()


def _reviewer_key(value: str | None) -> tuple[str, bool]:
    if value:
        return value, False
    return secrets.token_urlsafe(24), True


def build_guard_demo_router(
    *, live_compare: GuardLiveCompareService | None = None
) -> APIRouter:
    """Build the public Guard demo and optional isolated Live Compare API."""
    router = APIRouter()

    @router.get("/demo/guard", response_class=HTMLResponse)
    async def guard_demo_page() -> HTMLResponse:
        return HTMLResponse(content=GUARD_DEMO_PAGE, headers=_headers())

    @router.get("/demo/guard/he", response_class=HTMLResponse)
    async def guard_demo_page_hebrew() -> HTMLResponse:
        return HTMLResponse(content=GUARD_DEMO_PAGE_HE, headers=_headers())

    @router.get("/api/demo/guard")
    async def guard_demo_data(lang: str = "en") -> JSONResponse:
        fixture_path = _DEMO_FIXTURE_PATH_HE if lang == "he" else _DEMO_FIXTURE_PATH
        return JSONResponse(content=_load_fixture(fixture_path), headers=_headers())

    @router.post("/api/demo/guard/live/runs", status_code=202)
    async def start_live_run(body: LiveRunRequest, request: Request) -> JSONResponse:
        if live_compare is None:
            raise HTTPException(status_code=503, detail="live compare unavailable")
        try:
            admission = await live_compare.start(
                scenario_id=body.scenario_id,
                requester_key=_requester_key(request),
                request_id=body.request_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown scenario") from exc
        if not admission.allowed or admission.run is None:
            response = JSONResponse(
                status_code=429,
                content={"detail": admission.reason or "run quota exceeded"},
                headers={**_headers(), "Retry-After": "60"},
            )
            return response
        response = JSONResponse(
            status_code=202,
            content={
                "run_id": admission.run.id,
                "status": admission.run.status.value,
                "duplicate": admission.duplicate,
            },
            headers={**_headers(), "Location": f"/api/demo/guard/live/runs/{admission.run.id}"},
        )
        return response

    @router.get("/api/demo/guard/live/runs")
    async def list_live_runs(scenario_id: str | None = None) -> JSONResponse:
        if live_compare is None:
            raise HTTPException(status_code=503, detail="live compare unavailable")
        runs = await live_compare.list(scenario_id=scenario_id, limit=20)
        return JSONResponse(
            content={"runs": [_serialize_run(run) for run in runs]}, headers=_headers()
        )

    @router.get("/api/demo/guard/live/runs/{run_id}")
    async def get_live_run(run_id: str) -> JSONResponse:
        if live_compare is None:
            raise HTTPException(status_code=503, detail="live compare unavailable")
        bundle = await live_compare.get(run_id)
        if bundle is None:
            raise HTTPException(status_code=404, detail="run not found")
        return JSONResponse(content=_serialize_bundle(bundle), headers=_headers())

    @router.post("/api/demo/guard/live/judgments")
    async def save_live_judgment(
        body: JudgmentRequest,
        reviewer: str | None = Cookie(default=None, alias=_REVIEWER_COOKIE),
    ) -> JSONResponse:
        if live_compare is None:
            raise HTTPException(status_code=503, detail="live compare unavailable")
        reviewer_key, is_new = _reviewer_key(reviewer)
        judgment = await live_compare.set_judgment(
            snapshot_id=body.snapshot_id,
            reviewer_key=reviewer_key,
            label=body.label,
            note=body.note,
            reason=body.reason,
        )
        if judgment is None:
            raise HTTPException(status_code=404, detail="snapshot not found")
        output = JSONResponse(
            content={
                "snapshot_id": judgment.snapshot_id,
                "label": judgment.label.value,
                "note": judgment.note,
                "reason": judgment.reason,
                "updated_at": judgment.updated_at.isoformat(),
            },
            headers=_headers(),
        )
        if is_new:
            output.set_cookie(
                key=_REVIEWER_COOKIE,
                value=reviewer_key,
                max_age=60 * 60 * 24 * 365,
                httponly=True,
                secure=True,
                samesite="lax",
                path="/api/demo/guard",
            )
        return output

    return router


def _serialize_run(run: Any) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "scenario_id": run.scenario_id,
        "status": run.status.value,
        "gold_version": run.gold_version,
        "gold_label": run.gold_label,
        "model": run.model,
        "prompt_version": run.prompt_version,
        "analyzer_version": run.analyzer_version,
        "taxonomy_version": run.taxonomy_version,
        "created_at": run.created_at.isoformat(),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "error": run.error,
        "metadata": run.metadata,
    }


def _serialize_bundle(bundle: Any) -> dict[str, Any]:
    judgments_by_snapshot = {
        judgment.snapshot_id: {
            "label": judgment.label.value,
            "note": judgment.note,
            "reason": judgment.reason,
            "updated_at": judgment.updated_at.isoformat(),
        }
        for judgment in bundle.judgments
    }
    return {
        "run": _serialize_run(bundle.run),
        "snapshots": [
            {
                "snapshot_id": snapshot.id,
                "scenario_id": snapshot.scenario_id,
                "ordinal": snapshot.ordinal,
                "after_message_id": snapshot.after_message_id,
                "expected": snapshot.expected,
                "actual": snapshot.actual,
                "diff": snapshot.diff,
                "has_difference": snapshot.has_difference,
                "failure": snapshot.failure,
                "judgment": judgments_by_snapshot.get(snapshot.id),
            }
            for snapshot in bundle.snapshots
        ],
    }
