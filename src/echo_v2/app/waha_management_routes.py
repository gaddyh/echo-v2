"""WAHA agent management endpoints: command polling + event reporting.

Two endpoints, both authenticated with the **per-worker management token**
(``Authorization: Bearer wt_<worker_id>_<secret>`` — issued once at the
worker's first management-capable registration, verified against the stored
SHA-256 hash in constant time). This is deliberately not the fleet token:
a worker can only see its own commands, report its own command results, and
publish events for sessions assigned to it. The authenticated worker id is
authoritative — a ``worker_id`` in a request body is never trusted.

* ``POST /internal/waha/commands`` — the agent's management poll
  (independent of the batch loop, ~2 s interval). Claims lease-bounded
  commands for the authenticated worker; claiming never removes anything.
* ``POST /internal/waha/events`` — the agent reports command results,
  session lifecycle events and QR images. Returns 200 only after the
  required event state has been persisted / processing completed;
  temporary effect failures return 503 so the bounded agent-side retries
  re-drive them (idempotently).

Response codes: 200 processed; 202 safely ignored (unknown session /
unsubscribed event); 400 malformed; 401 bad token; 403 foreign session or
command; 503 temporary failure or storage unavailability.
"""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import ClientDisconnect

from echo_v2.persistence.waha_workers import WahaWorkerRegistry
from echo_v2.services.waha_session_service import (
    WahaCallbackAuthError,
    WahaCallbackBadRequestError,
    WahaCallbackForbiddenError,
    WahaCallbackRetryError,
    WahaSessionService,
)

__all__ = ["build_waha_management_router"]

_logger = logging.getLogger("echo_v2.app.waha_management_routes")

_MAX_BODY_BYTES = 512 * 1024


def _headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }


async def _read_body_limited(request: Request, *, limit: int) -> bytes:
    """Read the request body, enforcing ``limit`` during streaming."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_length = int(declared)
        except ValueError as exc:
            raise WahaCallbackBadRequestError("invalid content-length") from exc
        if declared_length > limit:
            raise WahaCallbackBadRequestError("payload too large")
    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            total += len(chunk)
            if total > limit:
                raise WahaCallbackBadRequestError("payload too large")
            chunks.append(chunk)
    except ClientDisconnect as exc:
        raise WahaCallbackBadRequestError("client disconnected") from exc
    return b"".join(chunks)


def build_waha_management_router(
    *,
    workers: WahaWorkerRegistry,
    service: WahaSessionService,
    max_body_bytes: int = _MAX_BODY_BYTES,
) -> APIRouter:
    router = APIRouter()

    async def _authenticated_worker(request: Request) -> str:
        """Resolve the worker identity from its management token.

        The bearer token embeds the worker id and a secret compared in
        constant time against the stored hash. 401 on anything malformed,
        unknown or mismatched — never a request-body ``worker_id``.
        """
        worker_id = await workers.verify_worker_token(
            request.headers.get("authorization")
        )
        if worker_id is None:
            _logger.warning(
                "waha management auth rejected reason=invalid_worker_token"
            )
            raise WahaCallbackAuthError("invalid worker token")
        return worker_id

    @router.post("/internal/waha/commands")
    async def poll_commands(request: Request) -> JSONResponse:
        started = time.perf_counter()
        try:
            worker_id = await _authenticated_worker(request)
            body = await _read_body_limited(request, limit=4096)
            if body.strip():
                try:
                    payload = json.loads(body)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    raise WahaCallbackBadRequestError("invalid JSON")
                if not isinstance(payload, dict):
                    raise WahaCallbackBadRequestError("payload must be an object")
                claimed = payload.get("worker_id")
                if claimed is not None and claimed != worker_id:
                    raise WahaCallbackForbiddenError(
                        "worker_id does not match the authenticated worker"
                    )
            commands = await service.claim_commands(worker_id)
        except WahaCallbackAuthError:
            return JSONResponse(
                status_code=401,
                content={"detail": "invalid worker token"},
                headers=_headers(),
            )
        except WahaCallbackForbiddenError:
            return JSONResponse(
                status_code=403,
                content={"detail": "forbidden"},
                headers=_headers(),
            )
        except WahaCallbackBadRequestError as exc:
            _logger.info(
                "waha commands rejected reason=malformed detail=%s", exc
            )
            return JSONResponse(
                status_code=400,
                content={"detail": "invalid payload"},
                headers=_headers(),
            )
        except SQLAlchemyError as exc:
            # Only the exception type: SQLAlchemy errors can embed the SQL
            # statement and bind parameters.
            _logger.error(
                "waha commands failed category=db error_type=%s worker=%s",
                type(exc).__name__,
                worker_id,
            )
            return JSONResponse(
                status_code=503,
                content={"detail": "commands temporarily unavailable"},
                headers=_headers(),
            )
        _logger.info(
            "waha commands polled worker=%s count=%d total_ms=%.1f",
            worker_id,
            len(commands),
            (time.perf_counter() - started) * 1000,
        )
        return JSONResponse(
            content={"commands": commands}, headers=_headers()
        )

    @router.post("/internal/waha/events")
    async def report_event(request: Request) -> JSONResponse:
        started = time.perf_counter()
        try:
            worker_id = await _authenticated_worker(request)
            raw_body = await _read_body_limited(request, limit=max_body_bytes)
            try:
                payload = json.loads(raw_body)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise WahaCallbackBadRequestError("invalid JSON") from exc
            if not isinstance(payload, dict):
                raise WahaCallbackBadRequestError("event must be an object")
            outcome = await service.handle_agent_event(payload, worker_id=worker_id)
        except WahaCallbackAuthError:
            return JSONResponse(
                status_code=401,
                content={"detail": "invalid worker token"},
                headers=_headers(),
            )
        except WahaCallbackForbiddenError:
            # Never confirm or deny foreign sessions' existence in detail.
            return JSONResponse(
                status_code=403,
                content={"detail": "forbidden"},
                headers=_headers(),
            )
        except WahaCallbackBadRequestError as exc:
            _logger.info(
                "waha event rejected reason=malformed detail=%s", exc
            )
            return JSONResponse(
                status_code=400,
                content={"detail": "invalid payload"},
                headers=_headers(),
            )
        except WahaCallbackRetryError as exc:
            # The lifecycle state is committed; only the side effect is
            # unfinished. The agent's bounded event retries re-drive it.
            _logger.warning("waha event retryable detail=%s", exc)
            return JSONResponse(
                status_code=503,
                content={"detail": "event processing temporarily unavailable"},
                headers=_headers(),
            )
        except SQLAlchemyError as exc:
            _logger.error(
                "waha event failed category=db error_type=%s worker=%s",
                type(exc).__name__,
                worker_id,
            )
            return JSONResponse(
                status_code=503,
                content={"detail": "event processing temporarily unavailable"},
                headers=_headers(),
            )
        status_code = 202 if outcome in ("unknown_session", "ignored_event") else 200
        _logger.info(
            "waha event outcome=%s worker=%s status_code=%d total_ms=%.1f",
            outcome,
            worker_id,
            status_code,
            (time.perf_counter() - started) * 1000,
        )
        return JSONResponse(
            content={"ok": True, "outcome": outcome},
            status_code=status_code,
            headers=_headers(),
        )

    return router
