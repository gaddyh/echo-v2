"""Unified recent-errors view for the Echo Operations Center.

Merges failure records from every subsystem into one paginated view:

* WAHA inbox consumer failures (``waha_event_inbox``)
* Baileys connector processing failures (ledger)
* WAHA management command failures (``waha_commands``)
* Media enrichment failures (``message_enrichments``)
* Pipeline failure events (``pipeline_events``)
* AI provider call errors (``ai_usage_events``)
* Scheduled action failures (``scheduled_actions``)
* Parent notification delivery failures (both outcome tables)

Privacy/safety: descriptions are truncated, newlines stripped and
control characters removed. No raw message content, SQL bind parameters,
tokens, media keys or credentials are included — sources store exception
summaries only, and this view never fetches message bodies.

Each source query is capped and indexed; the merged list is sorted newest
first and paginated in memory over the most recent ``per_source_limit``
rows per source (documented limitation — sufficient for an operational
"what is failing now" view, not a complete audit log).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["OpsErrorView", "OpsErrorViewSettings"]

_logger = logging.getLogger("echo_v2.services.ops_error_view")

_MAX_DESCRIPTION = 200
_SANITIZE_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class OpsErrorViewSettings:
    waha_message_max_attempts: int = 10
    baileys_message_max_attempts: int = 10
    default_limit: int = 50
    max_limit: int = 200
    per_source_limit: int = 300


def _sanitize(value: Any) -> str | None:
    if not value:
        return None
    cleaned = _SANITIZE_RE.sub(" ", str(value))
    return cleaned[:_MAX_DESCRIPTION].strip() or None


class OpsErrorView:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: OpsErrorViewSettings | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings or OpsErrorViewSettings()

    async def list_errors(
        self,
        *,
        since: datetime | None = None,
        subsystem: str | None = None,
        severity: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Merge and paginate recent errors; aggregate by category."""
        window = since or datetime.now(timezone.utc) - timedelta(hours=24)
        limit = min(
            max(limit or self._settings.default_limit, 1), self._settings.max_limit
        )
        cap = self._settings.per_source_limit
        async with self._session_factory() as session:
            sources: list[dict[str, Any]] = []
            for loader in (
                self._waha_inbox_errors,
                self._baileys_errors,
                self._waha_command_errors,
                self._enrichment_errors,
                self._pipeline_event_errors,
                self._ai_usage_errors,
                self._scheduled_action_errors,
                self._delivery_errors,
            ):
                try:
                    sources.extend(await loader(session, window, cap))
                except Exception as exc:
                    _logger.exception(
                        "ops error view source failed loader=%s", loader.__name__
                    )
                    try:
                        await session.rollback()
                    except Exception:  # noqa: BLE001, S110 — best-effort
                        pass
                    sources.append({
                        "timestamp": datetime.now(timezone.utc),
                        "subsystem": "ops_error_view",
                        "provider": None,
                        "user_id": None,
                        "worker_id": None,
                        "category": "source_unavailable",
                        "description": f"Error source unavailable: {type(exc).__name__}",
                        "state": "terminal",
                        "attempt_count": None,
                        "recovered": None,
                    })
            sources.sort(key=lambda item: item["timestamp"], reverse=True)
        filtered = [
            item for item in sources
            if (subsystem is None or item["subsystem"] == subsystem)
            and (severity is None or item["state"] == severity)
        ]
        aggregates: dict[tuple[str, str], int] = {}
        for item in filtered:
            key = (item["subsystem"], item["category"])
            aggregates[key] = aggregates.get(key, 0) + 1
        page = filtered[offset : offset + limit]
        for item in page:
            item["timestamp"] = item["timestamp"].isoformat()
        return {
            "available": True,
            "errors": page,
            "total_in_window": len(filtered),
            "offset": offset,
            "limit": limit,
            "aggregates": [
                {"subsystem": key[0], "category": key[1], "count": count}
                for key, count in sorted(
                    aggregates.items(), key=lambda kv: -kv[1]
                )
            ],
            "notes": (
                "Each source is capped at "
                f"{self._settings.per_source_limit} most recent rows; the "
                "merged view is an operational tool, not a complete audit "
                "log. 'recovered' is unknown unless the source row itself "
                "shows a later successful state."
            ),
        }

    async def _rows(
        self, session: AsyncSession, sql: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        result = await session.execute(text(sql), params)
        return [dict(row) for row in result.mappings().all()]

    async def _waha_inbox_errors(
        self, session: AsyncSession, since: datetime, cap: int
    ) -> list[dict[str, Any]]:
        rows = await self._rows(
            session,
            """
            SELECT last_updated_at AS timestamp, session, attempt_count,
                   last_error, status,
                   (attempt_count >= :max_attempts) AS exhausted
              FROM waha_event_inbox
             WHERE status = 'failed' AND last_updated_at >= :since
             ORDER BY last_updated_at DESC
             LIMIT :cap
            """,
            {
                "since": since,
                "cap": cap,
                "max_attempts": self._settings.waha_message_max_attempts,
            },
        )
        return [
            {
                "timestamp": row["timestamp"],
                "subsystem": "waha_message_consumer",
                "provider": "waha",
                "user_id": None,
                "worker_id": row["session"],
                "category": (
                    "inbox_retry_exhausted" if row["exhausted"] else "inbox_retryable"
                ),
                "description": _sanitize(row["last_error"]),
                "state": "terminal" if row["exhausted"] else "retryable",
                "attempt_count": row["attempt_count"],
                "recovered": None,
            }
            for row in rows
        ]

    async def _baileys_errors(
        self, session: AsyncSession, since: datetime, cap: int
    ) -> list[dict[str, Any]]:
        rows = await self._rows(
            session,
            """
            SELECT updated_at AS timestamp, event_id, attempt_count,
                   last_error, status
              FROM baileys_connector_event_processing
             WHERE last_error IS NOT NULL AND updated_at >= :since
             ORDER BY updated_at DESC
             LIMIT :cap
            """,
            {"since": since, "cap": cap},
        )
        return [
            {
                "timestamp": row["timestamp"],
                "subsystem": "baileys_consumer",
                "provider": "baileys",
                "user_id": None,
                "worker_id": None,
                "category": (
                    "processing_retry_exhausted"
                    if row["attempt_count"]
                    >= self._settings.baileys_message_max_attempts
                    and row["status"] == "failed"
                    else "processing_retryable"
                ),
                "description": _sanitize(row["last_error"]),
                "state": (
                    "terminal"
                    if row["status"] == "failed"
                    and row["attempt_count"]
                    >= self._settings.baileys_message_max_attempts
                    else "retryable"
                ),
                "attempt_count": row["attempt_count"],
                "recovered": row["status"] == "processed",
            }
            for row in rows
        ]

    async def _waha_command_errors(
        self, session: AsyncSession, since: datetime, cap: int
    ) -> list[dict[str, Any]]:
        rows = await self._rows(
            session,
            """
            SELECT completed_at AS timestamp, worker_id, session_name,
                   action, attempts, last_error
              FROM waha_commands
             WHERE status = 'failed' AND completed_at >= :since
             ORDER BY completed_at DESC
             LIMIT :cap
            """,
            {"since": since, "cap": cap},
        )
        return [
            {
                "timestamp": row["timestamp"],
                "subsystem": "waha_management",
                "provider": "waha",
                "user_id": None,
                "worker_id": row["worker_id"],
                "category": f"command_failed:{row['action']}",
                "description": _sanitize(row["last_error"]),
                "state": "terminal",
                "attempt_count": row["attempts"],
                "recovered": None,
            }
            for row in rows
        ]

    async def _enrichment_errors(
        self, session: AsyncSession, since: datetime, cap: int
    ) -> list[dict[str, Any]]:
        rows = await self._rows(
            session,
            """
            SELECT e.updated_at AS timestamp, e.status, e.attempts, e.error,
                   m.user_id::text AS user_id,
                   c.provider AS provider
              FROM message_enrichments e
              JOIN messages m ON m.id = e.message_id
              JOIN whatsapp_connections c ON c.id = m.connection_id
             WHERE e.status IN ('failed', 'terminal')
               AND e.updated_at >= :since
             ORDER BY e.updated_at DESC
             LIMIT :cap
            """,
            {"since": since, "cap": cap},
        )
        return [
            {
                "timestamp": row["timestamp"],
                "subsystem": "media_enrichment",
                "provider": row["provider"],
                "user_id": row["user_id"],
                "worker_id": None,
                "category": f"enrichment_{row['status']}",
                "description": _sanitize(row["error"]),
                "state": "terminal" if row["status"] == "terminal" else "retryable",
                "attempt_count": row["attempts"],
                "recovered": None,
            }
            for row in rows
        ]

    async def _pipeline_event_errors(
        self, session: AsyncSession, since: datetime, cap: int
    ) -> list[dict[str, Any]]:
        rows = await self._rows(
            session,
            """
            SELECT created_at AS timestamp, event_name, status, error_type,
                   error_message, pipeline
              FROM pipeline_events
             WHERE status IN ('failed', 'terminal', 'rejected', 'invalid_json',
                              'validation', 'oversized', 'auth', 'db')
               AND created_at >= :since
             ORDER BY created_at DESC
             LIMIT :cap
            """,
            {"since": since, "cap": cap},
        )
        subsystem_by_pipeline = {
            "waha_ingestion": "waha_ingestion",
            "media_enrichment": "media_enrichment",
            "guard_analysis": "guard_analysis",
            "guard_reconnect": "guard_reconnect",
        }
        return [
            {
                "timestamp": row["timestamp"],
                "subsystem": subsystem_by_pipeline.get(
                    row["pipeline"], row["pipeline"]
                ),
                "provider": "waha" if row["pipeline"] == "waha_ingestion" else None,
                "user_id": None,
                "worker_id": None,
                "category": row["error_type"] or row["event_name"],
                "description": _sanitize(
                    row["error_message"] or row["event_name"]
                ),
                "state": (
                    "retryable" if row["status"] == "failed" else "terminal"
                ),
                "attempt_count": None,
                "recovered": None,
            }
            for row in rows
        ]

    async def _ai_usage_errors(
        self, session: AsyncSession, since: datetime, cap: int
    ) -> list[dict[str, Any]]:
        rows = await self._rows(
            session,
            """
            SELECT created_at AS timestamp, provider, operation, error_type,
                   child_user_id::text AS user_id
              FROM ai_usage_events
             WHERE status = 'error' AND created_at >= :since
             ORDER BY created_at DESC
             LIMIT :cap
            """,
            {"since": since, "cap": cap},
        )
        return [
            {
                "timestamp": row["timestamp"],
                "subsystem": "ai_provider",
                "provider": row["provider"],
                "user_id": row["user_id"],
                "worker_id": None,
                "category": row["error_type"] or "unknown",
                "description": (
                    f"{row['operation']} call failed ({row['error_type']})"
                ),
                "state": "retryable",
                "attempt_count": None,
                "recovered": None,
            }
            for row in rows
        ]

    async def _scheduled_action_errors(
        self, session: AsyncSession, since: datetime, cap: int
    ) -> list[dict[str, Any]]:
        rows = await self._rows(
            session,
            """
            SELECT updated_at AS timestamp, type, error, user_id::text AS user_id
              FROM scheduled_actions
             WHERE status = 'failed' AND error IS NOT NULL
               AND updated_at >= :since
             ORDER BY updated_at DESC
             LIMIT :cap
            """,
            {"since": since, "cap": cap},
        )
        return [
            {
                "timestamp": row["timestamp"],
                "subsystem": "scheduler",
                "provider": None,
                "user_id": row["user_id"],
                "worker_id": None,
                "category": f"action_failed:{row['type']}",
                "description": _sanitize(row["error"]),
                "state": "terminal",
                "attempt_count": None,
                "recovered": None,
            }
            for row in rows
        ]

    async def _delivery_errors(
        self, session: AsyncSession, since: datetime, cap: int
    ) -> list[dict[str, Any]]:
        rows = await self._rows(
            session,
            """
            SELECT failed_at AS timestamp, status, reason,
                   child_user_id::text AS user_id
              FROM guard_parent_attention_delivery_outcomes
             WHERE status = 'failed' AND failed_at >= :since
             ORDER BY failed_at DESC
             LIMIT :cap
            """,
            {"since": since, "cap": cap},
        )
        rows += await self._rows(
            session,
            """
            SELECT failed_at AS timestamp, status, reason,
                   child_user_id::text AS user_id
              FROM guard_exposure_delivery_outcomes
             WHERE status = 'failed' AND failed_at >= :since
             ORDER BY failed_at DESC
             LIMIT :cap
            """,
            {"since": since, "cap": cap},
        )
        return [
            {
                "timestamp": row["timestamp"],
                "subsystem": "notifications",
                "provider": None,
                "user_id": row["user_id"],
                "worker_id": None,
                "category": "delivery_failed",
                "description": _sanitize(row["reason"]),
                "state": "terminal",
                "attempt_count": None,
                "recovered": None,
            }
            for row in rows
        ]
