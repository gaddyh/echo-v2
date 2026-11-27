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

from echo_v2.domain.pipeline_event import PipelineEvent
from echo_v2.domain.waha import WahaInboxMessage, WahaWorkerRegistration
from echo_v2.persistence.pipeline_events import PipelineEventRepository
from echo_v2.persistence.waha_event_inbox import WahaEventInboxRepository
from echo_v2.persistence.waha_workers import WahaWorkerRegistry
from echo_v2.services.ops_metrics_recorder import OpsMetricsRecorder

_logger = logging.getLogger("echo_v2.app.waha_routes")

_MAX_REGISTER_BODY_BYTES = 64 * 1024
_SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")
_HEALTH_VALUES = ("starting", "ok", "degraded")

# Optional agent-reported resource telemetry (Operations Center contract).
# Numeric gauges are whitelisted; anything else is rejected so a buggy agent
# cannot stuff arbitrary JSON into the registry.
_RESOURCE_NUMBER_FIELDS = (
    "vm_cpu_percent",
    "vm_ram_total_bytes",
    "vm_ram_available_bytes",
    "waha_container_memory_bytes",
    "waha_container_cpu_percent",
    "agent_memory_bytes",
    "disk_total_bytes",
    "disk_used_bytes",
    "sqlite_db_bytes",
    "agent_state_db_bytes",
    "vm_uptime_seconds",
    "waha_container_uptime_seconds",
    "agent_uptime_seconds",
    "oldest_undelivered_local_message_at",
    "last_batch_delivery_at",
)
_RESOURCE_STRING_FIELDS = ("waha_version",)


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


def _parse_resources(payload: dict[str, Any]) -> tuple[dict[str, Any], datetime | None]:
    """Parse the optional ``resources`` telemetry object.

    Backward compatible: absence returns ``(None-ish empty, None)`` so old
    agents continue to register unchanged. Present but malformed values are
    rejected (400) — the agent should send valid gauges or none at all.
    ``observed_at`` (epoch seconds) is the agent's measurement time; when
    omitted, Echo stamps ``now()`` server-side so resource freshness never
    depends on the agent's clock.
    """
    raw = payload.get("resources")
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="invalid resources")
    resources: dict[str, Any] = {}
    for field in _RESOURCE_NUMBER_FIELDS:
        value = raw.get(field)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(status_code=400, detail=f"invalid resources.{field}")
        resources[field] = float(value)
    for field in _RESOURCE_STRING_FIELDS:
        value = raw.get(field)
        if value is None:
            continue
        if not isinstance(value, str) or not value or len(value) > 64:
            raise HTTPException(status_code=400, detail=f"invalid resources.{field}")
        resources[field] = value
    observed = raw.get("observed_at")
    if observed is None:
        return resources, datetime.now(timezone.utc)
    return resources, _parse_epoch(observed, "resources.observed_at")


def _parse_registration(payload: Any) -> WahaWorkerRegistration:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    health = payload.get("health")
    if not isinstance(health, str) or health not in _HEALTH_VALUES:
        raise HTTPException(status_code=400, detail="invalid health")
    management_protocol = payload.get("management_protocol", 0)
    if (
        isinstance(management_protocol, bool)
        or not isinstance(management_protocol, int)
        or management_protocol < 0
    ):
        raise HTTPException(status_code=400, detail="invalid management_protocol")
    resources, resources_observed_at = _parse_resources(payload)
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
        management_protocol=management_protocol,
        resources=resources or None,
        resources_observed_at=resources_observed_at if resources else None,
    )


def build_waha_router(
    *,
    inbox: WahaEventInboxRepository,
    workers: WahaWorkerRegistry,
    token: str = "",
    max_messages: int = 200,
    max_body_bytes: int = 8 * 1024 * 1024,
    pipeline_events: PipelineEventRepository | None = None,
    metrics: OpsMetricsRecorder | None = None,
) -> APIRouter:
    router = APIRouter()

    async def _record_event(event: PipelineEvent) -> None:
        """Best-effort telemetry: never let it fail or delay the response."""
        if pipeline_events is None:
            return
        try:
            await pipeline_events.record(event)
        except Exception:
            _logger.exception(
                "waha telemetry event failed event_name=%s", event.event_name
            )

    def _count(metric: str, **kwargs: Any) -> None:
        if metrics is None:
            return
        try:
            metrics.increment(metric, **kwargs)
        except Exception:
            _logger.exception("waha telemetry counter failed metric=%s", metric)

    @router.post("/internal/waha/batch")
    async def ingest_batch(request: Request) -> JSONResponse:
        started = time.perf_counter()
        try:
            _require_token(token, request.headers.get("authorization"))
        except HTTPException:
            await _record_event(
                PipelineEvent(
                    event_name="waha.auth.rejected",
                    pipeline="waha_ingestion",
                    status="rejected",
                    error_type="auth",
                )
            )
            raise
        body = await _read_body_limited(request, limit=max_body_bytes)
        actual_bytes = len(body)
        _count("waha.batch.requests", provider="waha", count=1)
        _count("waha.batch.bytes", provider="waha", count=1, sum_ms=actual_bytes)
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
            await _record_event(
                PipelineEvent(
                    event_name="waha.batch.rejected",
                    pipeline="waha_ingestion",
                    status="invalid_json",
                    error_type="invalid_json",
                    event_id=None,
                )
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
            category = "oversized" if exc.status_code == 413 else "validation"
            await _record_event(
                PipelineEvent(
                    event_name="waha.batch.rejected",
                    pipeline="waha_ingestion",
                    status=category,
                    error_type=category,
                )
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
            await _record_event(
                PipelineEvent(
                    event_name="waha.batch.failed",
                    pipeline="waha_ingestion",
                    status="failed",
                    error_type=type(exc).__name__,
                    event_id=batch_id,
                )
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
        if messages:
            # Exact per-batch historical record (bounded, privacy-safe
            # counters only). Empty batches are counted only in ops_metrics.
            await _record_event(
                PipelineEvent(
                    event_name="waha.batch.ingested",
                    pipeline="waha_ingestion",
                    status="success",
                    event_id=batch_id,
                    duration_ms=total_ms,
                    attempt=1,
                    metadata={
                        "received": len(messages),
                        "acked": len(acked),
                        "inserted": result.inserted,
                        "duplicated": result.duplicated + in_batch_dup,
                        "updated": result.updated,
                        "in_batch_dup": in_batch_dup,
                        "discarded_revisions": discarded_revisions,
                        "body_bytes": actual_bytes,
                        "persistence_ms": round(persistence_ms, 1),
                    },
                )
            )
        else:
            _count("waha.batch.empty", provider="waha", count=1)
        _count(
            "waha.batch.latency_ms",
            provider="waha",
            count=1,
            sum_ms=total_ms,
            max_ms=total_ms,
        )
        _count(
            "waha.batch.persistence_ms",
            provider="waha",
            count=1,
            sum_ms=persistence_ms,
            max_ms=persistence_ms,
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
        try:
            _require_token(token, request.headers.get("authorization"))
        except HTTPException:
            await _record_event(
                PipelineEvent(
                    event_name="waha.auth.rejected",
                    pipeline="waha_ingestion",
                    status="rejected",
                    error_type="auth",
                )
            )
            raise
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
            registration_result = await workers.register(registration)
        except SQLAlchemyError as exc:
            _logger.error(
                "waha worker register failed category=db error_type=%s worker_id=%s",
                type(exc).__name__,
                registration.worker_id,
            )
            await _record_event(
                PipelineEvent(
                    event_name="waha.register.failed",
                    pipeline="waha_ingestion",
                    status="failed",
                    error_type=type(exc).__name__,
                    event_id=registration.worker_id,
                )
            )
            raise HTTPException(
                status_code=503, detail="registration temporarily unavailable"
            ) from exc
        first_registration = registration_result.first
        if first_registration:
            _logger.info(
                "waha worker registered worker_id=%s region=%s version=%s "
                "health=%s active_sessions=%d max_sessions=%d pending=%d "
                "management_protocol=%d",
                registration.worker_id,
                registration.region,
                registration.version,
                registration.health,
                registration.active_sessions,
                registration.max_sessions,
                registration.pending,
                registration.management_protocol,
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
        content: dict[str, Any] = {"ok": True}
        if registration_result.issued_worker_token is not None:
            # Issued exactly once (first management-capable registration).
            # The agent must persist it; it is never shown again and never
            # logged.
            content["worker_token"] = registration_result.issued_worker_token
        return JSONResponse(content=content, headers=_headers())

    return router
