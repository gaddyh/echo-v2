"""WAHA batch-agent routes: durable message ingestion + worker registry.

Two endpoints, both authenticated with a shared bearer token
(``WAHA_AGENT_TOKEN``, constant-time compare, fail closed when unset):

* ``POST /internal/waha/batch`` — durable batch ingestion. The body is
  consumed with a streaming size limit (never buffered whole before the
  limit is checked), every message's SHA-256 is verified against the exact
  UTF-8 bytes of its ``data`` string, and the whole batch is persisted in a
  single PostgreSQL transaction. The ACK is returned only after that
  transaction has committed; a failed transaction rolls back and returns a
  non-200 response so the agent retries.
* ``POST /internal/waha/register`` — idempotent worker registration /
  heartbeat. Availability is derived later from Echo-side
  ``last_seen_at`` + reported health, never from the worker's clock.

ACK semantics (the agent deletes local rows only on an exact
``(session, id, sha256)`` triple match): if a batch carries the same
``(session, id)`` with different hashes, only the hash that was actually
persisted (the last occurrence) is acknowledged — discarded revisions are
never ACKed and never counted as duplicates or updates.

Logging: structured counters only — batch IDs, counts, byte sizes and
durations. No raw payloads, message bodies, phone numbers/JIDs, media
content, secrets or SQL exception details (SQL errors can embed the
statement and bind parameters, so only the exception type is logged).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import ClientDisconnect

from echo_v2.domain.waha import WahaInboxMessage, WahaWorkerRegistration
from echo_v2.persistence.waha_event_inbox import WahaEventInboxRepository
from echo_v2.persistence.waha_workers import WahaWorkerRegistry

_logger = logging.getLogger("echo_v2.app.waha_routes")

_MAX_REGISTER_BODY_BYTES = 64 * 1024
_SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")
_HEALTH_VALUES = ("starting", "ok", "degraded")


def _headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }


def _require_token(token: str, authorization: str | None) -> None:
    if not token:
        # Fail closed: unauthenticated ingestion must never be exposed.
        raise HTTPException(status_code=503, detail="waha agent token not configured")
    if authorization is None or not authorization.startswith("Bearer "):
        _logger.warning("waha auth rejected reason=missing_bearer_token")
        raise HTTPException(status_code=401, detail="missing bearer token")
    candidate = authorization[len("Bearer "):]
    if not hmac.compare_digest(candidate, token):
        _logger.warning("waha auth rejected reason=invalid_bearer_token")
        raise HTTPException(status_code=401, detail="invalid bearer token")


async def _read_body_limited(request: Request, *, limit: int) -> bytes:
    """Read the request body, enforcing ``limit`` during streaming.

    The ``Content-Length`` header is checked first (cheap early reject);
    chunked bodies are enforced chunk-by-chunk so an oversized request is
    aborted as soon as the limit is crossed, before the whole body is
    buffered.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_length = int(declared)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid content-length") from exc
        if declared_length > limit:
            raise HTTPException(status_code=413, detail="payload too large")
    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            total += len(chunk)
            if total > limit:
                raise HTTPException(status_code=413, detail="payload too large")
            chunks.append(chunk)
    except ClientDisconnect as exc:
        raise HTTPException(status_code=400, detail="client disconnected") from exc
    return b"".join(chunks)


def _require_str(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise HTTPException(status_code=400, detail=f"invalid {field}")
    return value


def _require_int(payload: dict[str, Any], field: str, *, minimum: int | None = None) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(status_code=400, detail=f"invalid {field}")
    if minimum is not None and value < minimum:
        raise HTTPException(status_code=400, detail=f"invalid {field}")
    return value


def _require_number(payload: dict[str, Any], field: str) -> float:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HTTPException(status_code=400, detail=f"invalid {field}")
    return float(value)


def _require_flag(payload: dict[str, Any], field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1):
        raise HTTPException(status_code=400, detail=f"invalid {field}")
    return value


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise HTTPException(status_code=400, detail="invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise HTTPException(status_code=400, detail="timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _parse_epoch(value: Any, field: str) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HTTPException(status_code=400, detail=f"invalid {field}")
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (OSError, OverflowError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid {field}") from exc


def _parse_message(raw: Any) -> WahaInboxMessage:
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="message must be an object")
    session = _require_str(raw, "session")
    message_id = _require_str(raw, "id")
    chat_jid = _require_str(raw, "jid")
    whatsapp_timestamp = _parse_timestamp(raw.get("timestamp"))
    from_me = _require_flag(raw, "from_me")
    is_real = _require_flag(raw, "is_real")
    data = raw.get("data")
    if not isinstance(data, str) or not data:
        raise HTTPException(status_code=400, detail="invalid data")
    sha256 = raw.get("sha256")
    if not isinstance(sha256, str) or not _SHA256_HEX.fullmatch(sha256):
        raise HTTPException(status_code=400, detail="invalid sha256")
    seq = _require_int(raw, "seq")
    agent_first_seen_at = _parse_epoch(raw.get("first_seen_at"), "first_seen_at")

    computed = hashlib.sha256(data.encode("utf-8")).hexdigest()
    if computed != sha256.lower():
        raise HTTPException(status_code=400, detail="sha256 mismatch")

    return WahaInboxMessage(
        session=session,
        message_id=message_id,
        chat_jid=chat_jid,
        whatsapp_timestamp=whatsapp_timestamp,
        from_me=from_me,
        is_real=is_real,
        data=data,
        sha256=sha256,
        seq=seq,
        agent_first_seen_at=agent_first_seen_at,
        batch_id="",
    )


def _parse_batch(payload: Any) -> tuple[str, list[WahaInboxMessage]]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    batch_id = _require_str(payload, "batch_id")
    _require_number(payload, "sent_at")
    informational_bytes = payload.get("bytes")
    if isinstance(informational_bytes, bool) or (
        informational_bytes is not None
        and not isinstance(informational_bytes, int)
    ):
        # Informational only — never trusted for size enforcement.
        raise HTTPException(status_code=400, detail="invalid bytes")
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list):
        raise HTTPException(status_code=400, detail="messages must be a list")
    messages = [_parse_message(raw) for raw in raw_messages]
    return batch_id, messages


def _parse_registration(payload: Any) -> WahaWorkerRegistration:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    health = payload.get("health")
    if not isinstance(health, str) or health not in _HEALTH_VALUES:
        raise HTTPException(status_code=400, detail="invalid health")
    return WahaWorkerRegistration(
        worker_id=_require_str(payload, "worker_id"),
        region=_require_str(payload, "region"),
        endpoint=_require_str(payload, "endpoint"),
        max_sessions=_require_int(payload, "max_sessions", minimum=0),
        active_sessions=_require_int(payload, "active_sessions", minimum=0),
        version=_require_str(payload, "version"),
        health=health,
        reported_heartbeat=_parse_epoch(payload.get("last_heartbeat"), "last_heartbeat"),
        pending=_require_int(payload, "pending", minimum=0),
    )


def build_waha_router(
    *,
    inbox: WahaEventInboxRepository,
    workers: WahaWorkerRegistry,
    token: str = "",
    max_messages: int = 200,
    max_body_bytes: int = 8 * 1024 * 1024,
) -> APIRouter:
    router = APIRouter()

    @router.post("/internal/waha/batch")
    async def ingest_batch(request: Request) -> JSONResponse:
        started = time.perf_counter()
        _require_token(token, request.headers.get("authorization"))
        body = await _read_body_limited(request, limit=max_body_bytes)
        actual_bytes = len(body)
        try:
            payload = json.loads(body)
            batch_id, messages = _parse_batch(payload)
            if len(messages) > max_messages:
                raise HTTPException(status_code=413, detail="too many messages")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            _logger.info(
                "waha batch rejected category=invalid_json body_bytes=%d",
                actual_bytes,
            )
            raise HTTPException(status_code=400, detail="invalid JSON") from exc
        except HTTPException as exc:
            _logger.info(
                "waha batch rejected status=%s category=validation detail=%s "
                "body_bytes=%d",
                exc.status_code,
                exc.detail,
                actual_bytes,
            )
            raise

        # Collapse within-batch repeats of the same (session, message_id):
        # the last occurrence wins; an earlier different hash is a discarded
        # revision — never acknowledged, never counted. Same-hash repeats
        # are duplicates.
        persisted: dict[tuple[str, str], WahaInboxMessage] = {}
        in_batch_dup = 0
        discarded_revisions = 0
        for message in messages:
            key = (message.session, message.message_id)
            current = persisted.get(key)
            if current is None:
                persisted[key] = message
            elif current.sha256 == message.sha256:
                in_batch_dup += 1
            else:
                persisted[key] = message
                discarded_revisions += 1

        persistence_started = time.perf_counter()
        try:
            # Single transaction, commit before returning — the ACK below is
            # only reachable after the commit has durably succeeded.
            result = await inbox.ingest_batch(batch_id, list(persisted.values()))
        except SQLAlchemyError as exc:
            # Only the exception type is logged: SQLAlchemy errors can embed
            # the SQL statement and bind parameters (raw message payloads).
            _logger.error(
                "waha batch ingest failed category=db error_type=%s batch_id=%s "
                "received=%d",
                type(exc).__name__,
                batch_id,
                len(messages),
            )
            raise HTTPException(
                status_code=503, detail="ingestion temporarily unavailable"
            ) from exc
        persistence_ms = (time.perf_counter() - persistence_started) * 1000

        acked = [
            {"session": m.session, "id": m.message_id, "sha256": m.sha256}
            for m in persisted.values()
        ]
        total_ms = (time.perf_counter() - started) * 1000
        _logger.info(
            "waha batch ingested batch_id=%s received=%d acked=%d inserted=%d "
            "dup=%d updated=%d in_batch_dup=%d discarded_revisions=%d "
            "body_bytes=%d persistence_ms=%.1f total_ms=%.1f",
            batch_id,
            len(messages),
            len(acked),
            result.inserted,
            result.duplicated + in_batch_dup,
            result.updated,
            in_batch_dup,
            discarded_revisions,
            actual_bytes,
            persistence_ms,
            total_ms,
        )
        return JSONResponse(
            content={
                "batch_id": batch_id,
                "acked": acked,
                "dup": result.duplicated + in_batch_dup,
                "updated": result.updated,
            },
            headers=_headers(),
        )

    @router.post("/internal/waha/register")
    async def register_worker(request: Request) -> JSONResponse:
        _require_token(token, request.headers.get("authorization"))
        body = await _read_body_limited(request, limit=_MAX_REGISTER_BODY_BYTES)
        try:
            registration = _parse_registration(json.loads(body))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="invalid JSON") from exc
        except HTTPException as exc:
            _logger.info(
                "waha register rejected status=%s category=validation detail=%s",
                exc.status_code,
                exc.detail,
            )
            raise
        try:
            first_registration = await workers.register(registration)
        except SQLAlchemyError as exc:
            _logger.error(
                "waha worker register failed category=db error_type=%s worker_id=%s",
                type(exc).__name__,
                registration.worker_id,
            )
            raise HTTPException(
                status_code=503, detail="registration temporarily unavailable"
            ) from exc
        if first_registration:
            _logger.info(
                "waha worker registered worker_id=%s region=%s version=%s "
                "health=%s active_sessions=%d max_sessions=%d pending=%d",
                registration.worker_id,
                registration.region,
                registration.version,
                registration.health,
                registration.active_sessions,
                registration.max_sessions,
                registration.pending,
            )
        else:
            _logger.debug(
                "waha worker heartbeat worker_id=%s region=%s health=%s "
                "active_sessions=%d max_sessions=%d pending=%d",
                registration.worker_id,
                registration.region,
                registration.health,
                registration.active_sessions,
                registration.max_sessions,
                registration.pending,
            )
        return JSONResponse(content={"ok": True}, headers=_headers())

    return router
