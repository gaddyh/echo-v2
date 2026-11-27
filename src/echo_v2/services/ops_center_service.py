"""Echo Operations Center — the single read model for operational health.

One snapshot answers: are workers healthy, are users connected, are
messages reaching Echo, are they processed, is media analyzed, are Guard
analyzers keeping up, are parent notifications delivered, where are
failures, and what does it cost.

Design rules (see the Operations Center spec):

* **Read-only grouped SQL.** No N+1 loops — every section is a handful of
  ``GROUP BY`` queries over indexed windows. Raw JSON payloads
  (``waha_event_inbox.data``) are never scanned.
* **Sections fail independently.** Each section is built inside
  ``_section``; a failing query marks only that section ``unavailable``.
* **Current state vs. windowed telemetry.** Worker health, inbox backlog
  and command state always reflect *now*, regardless of the selected
  historical period; windowed counters cover the selected period only.
* **Never fabricate.** Missing values are ``None`` (rendered N/A), never
  zero. Notifications are labeled "sent (provider accepted)" — no device
  delivery receipts exist anywhere in Echo.
* **No side effects.** No WAHA HTTP calls, no management commands, no
  writes. Safe to refresh every 15-30 seconds; a short-lived snapshot
  cache keeps repeated refreshes cheap.

Health rules live in :mod:`echo_v2.services.ops_health_rules` and are
evaluated against the assembled snapshot — an explicit, configurable
worst-of status rather than "the API responded".
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "PERIOD_SECONDS",
    "OpsCenterService",
    "OpsCenterSettings",
]

_logger = logging.getLogger("echo_v2.services.ops_center_service")

PERIOD_SECONDS: dict[str, int] = {
    "15m": 15 * 60,
    "1h": 60 * 60,
    "24h": 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
    "30d": 30 * 24 * 60 * 60,
}
DEFAULT_PERIOD = "24h"

# Sync/catch-up label threshold: a WhatsApp message received more than this
# long after its device timestamp is historical sync, not live delivery.
_SYNC_LAG_SECONDS = 600


@dataclass(frozen=True)
class OpsCenterSettings:
    """Thresholds for derived state and health rules (all configurable)."""

    waha_heartbeat_timeout_seconds: float = 90.0
    waha_message_max_attempts: int = 10
    waha_message_lease_seconds: int = 60
    waha_command_max_attempts: int = 5
    waha_command_lease_seconds: int = 120
    baileys_message_max_attempts: int = 10
    baileys_lease_seconds: int = 60
    inbox_backlog_warning_seconds: float = 3600.0
    analysis_overdue_seconds: float = 600.0
    analyzer_failure_threshold: int = 5
    analysis_backlog_threshold: int = 20
    notification_overdue_seconds: float = 900.0
    media_url_expiry_seconds: float = 86400.0
    expired_lease_grace_seconds: float = 300.0
    snapshot_cache_seconds: float = 15.0


class OpsCenterService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: OpsCenterSettings | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings or OpsCenterSettings()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._cache: dict[str, dict[str, Any]] = {}
        self._cache_at: dict[str, datetime] = {}
        self._cache_lock = asyncio.Lock()

    # -- query helpers ------------------------------------------------------

    async def _rows(
        self, session: AsyncSession, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        result = await session.execute(text(sql), params or {})
        return [dict(row) for row in result.mappings().all()]

    async def _one(
        self, session: AsyncSession, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        result = await session.execute(text(sql), params or {})
        return dict(result.mappings().one())

    # -- public API -----------------------------------------------------------

    async def snapshot(
        self,
        *,
        period: str = DEFAULT_PERIOD,
        provider: str = "all",
        worker_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        key = f"{period}|{provider}|{worker_id or ''}|{user_id or ''}"
        async with self._cache_lock:
            cached = self._cache.get(key)
            cached_at = self._cache_at.get(key)
            if (
                cached is not None
                and cached_at is not None
                and (self._clock() - cached_at).total_seconds()
                < self._settings.snapshot_cache_seconds
            ):
                return cached
        result = await self._build_snapshot(
            period=period, provider=provider, worker_id=worker_id, user_id=user_id
        )
        async with self._cache_lock:
            self._cache[key] = result
            self._cache_at[key] = self._clock()
        return result

    async def user_detail(self, user_id: str) -> dict[str, Any]:
        """Per-user drill-down (operational detail, no raw message content)."""
        now = self._clock()
        since = now - timedelta(days=7)
        async with self._session_factory() as session:
            user = await self._user_row(session, user_id)
            if user is None:
                return {"available": False, "reason": "user not found"}
            return {
                "available": True,
                "user": user,
                "connections": await self._connections_for_user(session, user_id),
                "messages_7d": await self._user_messages(session, user_id, since),
                "inbox": await self._user_inbox(session, user_id),
                "analyses_7d": await self._user_analyses(session, user_id, since),
                "notifications_7d": await self._user_notifications(
                    session, user_id, since
                ),
            }

    # -- snapshot assembly ------------------------------------------------------

    async def _build_snapshot(
        self,
        *,
        period: str,
        provider: str,
        worker_id: str | None,
        user_id: str | None,
    ) -> dict[str, Any]:
        seconds = PERIOD_SECONDS.get(period, PERIOD_SECONDS[DEFAULT_PERIOD])
        now = self._clock()
        since = now - timedelta(seconds=seconds)
        sections: dict[str, Any] = {}

        async with self._session_factory() as session:
            for name, builder, kwargs in (
                ("providers", self._providers, {}),
                ("waha_infrastructure", self._waha_infrastructure,
                 {"worker_id": worker_id}),
                ("users", self._users_section, {"user_id": user_id}),
                ("ingestion", self._ingestion, {}),
                ("media", self._media, {}),
                ("guard_analysis", self._guard_analysis, {}),
                ("notifications", self._notifications, {}),
                ("management", self._management, {}),
                ("ai_costs", self._ai_costs, {}),
            ):
                try:
                    sections[name] = await builder(session, since, now, **kwargs)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Roll back so one failing query cannot poison the
                    # session for the remaining sections (a Postgres error
                    # aborts the transaction until rollback).
                    try:
                        await session.rollback()
                    except Exception:  # noqa: BLE001, S110 — best-effort
                        pass
                    _logger.exception("ops center section failed section=%s", name)
                    sections[name] = {"available": False, "error": type(exc).__name__}

        sections["overview"] = self._overview(sections)
        return {
            "generated_at": now.isoformat(),
            "period": period if period in PERIOD_SECONDS else DEFAULT_PERIOD,
            "period_seconds": seconds,
            "filters": {
                "provider": provider,
                "worker_id": worker_id,
                "user_id": user_id,
            },
            "settings": {
                "waha_heartbeat_timeout_seconds": (
                    self._settings.waha_heartbeat_timeout_seconds
                ),
                "waha_message_max_attempts": self._settings.waha_message_max_attempts,
                "waha_command_max_attempts": self._settings.waha_command_max_attempts,
                "baileys_message_max_attempts": (
                    self._settings.baileys_message_max_attempts
                ),
            },
            "sections": sections,
        }

    # -- overview ----------------------------------------------------------------

    def _overview(self, sections: dict[str, Any]) -> dict[str, Any]:
        providers = sections.get("providers") or {}
        waha = sections.get("waha_infrastructure") or {}
        ingestion = sections.get("ingestion") or {}
        guard = sections.get("guard_analysis") or {}
        notifications = sections.get("notifications") or {}
        costs = sections.get("ai_costs") or {}
        connection_summary = providers.get("connection_summary", {})
        inbox = ingestion.get("waha_inbox", {}).get("current", {})
        baileys = ingestion.get("baileys_inbox", {}).get("current", {})
        worker_summary = waha.get("worker_summary", {})

        def _opt(value: Any) -> int | None:
            return value if value is not None else None

        return {
            "available": providers.get("available", False),
            # Current-state counters
            "monitored_users": connection_summary.get("total_users"),
            "connected_users": connection_summary.get("connected"),
            "disconnected_users": connection_summary.get("disconnected"),
            "pairing_required_users": connection_summary.get("pairing_required"),
            "stale_status_users": connection_summary.get("stale"),
            "healthy_workers": worker_summary.get("healthy"),
            "unavailable_workers": (
                (worker_summary.get("stale") or 0)
                + (worker_summary.get("degraded") or 0)
                if waha.get("available")
                else None
            ),
            # Windowed counters (selected period)
            "messages_received_period": providers.get("messages_period", {}).get(
                "total"
            ),
            "guard_analyses_committed_period": (
                (guard.get("security", {}).get("committed") or 0)
                + (guard.get("exposure", {}).get("committed") or 0)
                if guard.get("available")
                else None
            ),
            "notifications_sent_period": notifications.get("summary", {}).get("sent"),
            "notifications_failed_period": notifications.get("summary", {}).get(
                "failed"
            ),
            "estimated_ai_cost_period": costs.get("overview", {}).get(
                "total_cost_usd"
            ),
            # Current backlog (always live)
            "messages_pending_processing": (
                _opt(
                    (inbox.get("pending") or 0)
                    + (inbox.get("failed_retryable") or 0)
                    + (inbox.get("processing") or 0)
                    + (baileys.get("unprocessed") or 0)
                )
                if ingestion.get("available")
                else None
            ),
            "messages_failed_exhausted": (
                _opt(
                    (inbox.get("failed_exhausted") or 0)
                    + (baileys.get("failed_exhausted") or 0)
                )
                if ingestion.get("available")
                else None
            ),
            "overdue_guard_chats": guard.get("scheduled", {}).get("overdue"),
        }

    # -- provider health -----------------------------------------------------------

    async def _providers(
        self, session: AsyncSession, since: datetime, now: datetime
    ) -> dict[str, Any]:
        rows = await self._rows(
            session,
            """
            SELECT c.provider,
                   count(*) AS assigned,
                   count(DISTINCT c.user_id) AS users,
                   count(*) FILTER (WHERE c.connection_status = 'connected')
                       AS connected,
                   count(*) FILTER (WHERE c.connection_status = 'pairing_required')
                       AS pairing_required,
                   count(*) FILTER (WHERE c.connection_status IN
                       ('provisioning', 'connecting')) AS provisioning,
                   count(*) FILTER (WHERE c.connection_status IN
                       ('degraded', 'blocked', 'suspended', 'unknown'))
                       AS degraded_or_blocked,
                   count(*) FILTER (WHERE c.updated_at < :stale_cutoff
                       AND c.connection_status <> 'connected') AS stale,
                   max(c.updated_at) AS last_status_change
              FROM whatsapp_connections c
             GROUP BY c.provider
             ORDER BY c.provider
            """,
            {"stale_cutoff": now - timedelta(hours=1)},
        )
        msg_rows = await self._rows(
            session,
            """
            SELECT conn.provider,
                   count(*) AS messages,
                   count(*) FILTER (WHERE m.direction = 'inbound') AS incoming,
                   count(*) FILTER (WHERE m.direction = 'outbound') AS phone_sent,
                   count(*) FILTER (WHERE m.chat_id LIKE '%@g.us') AS group_messages,
                   count(*) FILTER (WHERE m.chat_id NOT LIKE '%@g.us')
                       AS private_messages,
                   max(m.timestamp) AS last_whatsapp_at,
                   max(m.created_at) AS last_ingested_at,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY
                       EXTRACT(EPOCH FROM (m.created_at - m.timestamp)))
                       AS device_to_ingest_p50_s,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY
                       EXTRACT(EPOCH FROM (m.created_at - m.timestamp)))
                       AS device_to_ingest_p95_s
              FROM messages m
              JOIN whatsapp_connections conn ON conn.id = m.connection_id
             WHERE m.created_at >= :since
             GROUP BY conn.provider
            """,
            {"since": since},
        )
        by_provider = {row["provider"]: row for row in msg_rows}
        waha_backlog = await self._waha_inbox_current(session)
        baileys_backlog = await self._baileys_backlog(session)

        providers = []
        for row in rows:
            provider = row["provider"]
            messages = by_provider.get(provider, {})
            backlog = (
                waha_backlog
                if provider == "waha"
                else baileys_backlog if provider == "baileys" else None
            )
            providers.append({
                "provider": provider,
                "assigned_connections": row["assigned"],
                "users": row["users"],
                "connected": row["connected"],
                "pairing_required": row["pairing_required"],
                "provisioning": row["provisioning"],
                "degraded_or_blocked": row["degraded_or_blocked"],
                "stale": row["stale"],
                "last_status_change": row["last_status_change"],
                "messages_period": messages.get("messages"),
                "incoming_period": messages.get("incoming"),
                "phone_sent_period": messages.get("phone_sent"),
                "group_messages_period": messages.get("group_messages"),
                "private_messages_period": messages.get("private_messages"),
                "last_whatsapp_at": messages.get("last_whatsapp_at"),
                "last_ingested_at": messages.get("last_ingested_at"),
                "device_to_ingest_p50_s": messages.get("device_to_ingest_p50_s"),
                "device_to_ingest_p95_s": messages.get("device_to_ingest_p95_s"),
                "pending_backlog": backlog.get("pending") if backlog else None,
                "failed_backlog": (
                    (backlog.get("failed_retryable") or 0)
                    + (backlog.get("failed_exhausted") or 0)
                    if backlog
                    else None
                ),
                "oldest_pending_at": (
                    backlog.get("oldest_pending_at") if backlog else None
                ),
            })
        summary = {
            key: sum(row[key] or 0 for row in rows)
            for key in ("assigned", "connected", "pairing_required", "stale")
        }
        summary["disconnected"] = sum(
            (row["degraded_or_blocked"] or 0) + (row["provisioning"] or 0)
            for row in rows
        )
        summary["total_users"] = sum(row["users"] or 0 for row in rows)
        return {
            "available": True,
            "providers": providers,
            "connection_summary": summary,
            "messages_period": {
                "total": sum(row["messages"] or 0 for row in msg_rows),
                "incoming": sum(row["incoming"] or 0 for row in msg_rows),
                "phone_sent": sum(row["phone_sent"] or 0 for row in msg_rows),
            },
            "provider_neutrality_note": (
                "Backlog semantics differ per provider: WAHA counts its own "
                "inbox rows, Baileys counts the connector inbox. Missing "
                "values are N/A, not zero. Message volume alone says "
                "nothing about reliability."
            ),
        }

    async def _waha_inbox_current(self, session: AsyncSession) -> dict[str, Any]:
        return await self._one(
            session,
            """
            SELECT count(*) FILTER (WHERE status = 'pending') AS pending,
                   count(*) FILTER (WHERE status = 'failed'
                       AND attempt_count < :max_attempts) AS failed_retryable,
                   count(*) FILTER (WHERE status = 'failed'
                       AND attempt_count >= :max_attempts) AS failed_exhausted,
                   count(*) FILTER (WHERE status = 'processing') AS processing,
                   min(first_received_at) FILTER (
                       WHERE status IN ('pending', 'failed')) AS oldest_pending_at
              FROM waha_event_inbox
            """,
            {"max_attempts": self._settings.waha_message_max_attempts},
        )

    async def _baileys_backlog(self, session: AsyncSession) -> dict[str, Any]:
        try:
            return await self._one(
                session,
                """
                SELECT count(*) FILTER (WHERE processed_at IS NULL
                       AND event_type = 'message') AS pending,
                       0 AS failed_retryable,
                       count(*) FILTER (EXISTS (
                           SELECT 1 FROM baileys_connector_event_processing p
                            WHERE p.inbox_id = whatsapp_connector.event_inbox.id
                              AND p.status = 'failed'
                              AND p.attempt_count >= :max_attempts))
                           AS failed_exhausted,
                       min(received_at) FILTER (WHERE processed_at IS NULL)
                           AS oldest_pending_at
                  FROM whatsapp_connector.event_inbox
                 WHERE received_at >= now() - interval '30 days'
                """,
                {"max_attempts": self._settings.baileys_message_max_attempts},
            )
        except Exception as exc:  # noqa: BLE001 — optional connector schema
            _logger.warning(
                "baileys connector inbox unavailable error=%s", type(exc).__name__
            )
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001, S110 — best-effort
                pass
            return {
                "pending": None,
                "failed_retryable": None,
                "failed_exhausted": None,
                "oldest_pending_at": None,
            }

    # -- WAHA infrastructure ----------------------------------------------------------

    async def _waha_infrastructure(
        self,
        session: AsyncSession,
        since: datetime,
        now: datetime,
        *,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        worker_rows = await self._rows(
            session,
            """
            SELECT w.worker_id, w.region, w.max_sessions, w.active_sessions,
                   w.agent_version, w.health, w.last_seen_at,
                   w.first_registered_at, w.pending, w.management_protocol,
                   w.resources, w.resources_observed_at,
                   EXTRACT(EPOCH FROM (now() - w.last_seen_at))
                       AS heartbeat_age_seconds
              FROM waha_workers w
             WHERE (CAST(:worker_id AS text) IS NULL OR w.worker_id = :worker_id)
             ORDER BY w.worker_id
            """,
            {"worker_id": worker_id},
        )
        assigned_rows = await self._rows(
            session,
            """
            SELECT worker_id, count(*) AS assigned,
                   count(*) FILTER (WHERE pending_effect IS NOT NULL)
                       AS pending_effects
              FROM waha_sessions
             GROUP BY worker_id
            """,
        )
        assigned_by_worker = {row["worker_id"]: row for row in assigned_rows}
        command_rows = await self._rows(
            session,
            """
            SELECT worker_id,
                   count(*) FILTER (WHERE status = 'pending') AS pending,
                   count(*) FILTER (WHERE status = 'claimed') AS claimed,
                   count(*) FILTER (WHERE status = 'claimed'
                       AND lease_expires_at < now()) AS expired_leases,
                   count(*) FILTER (WHERE status = 'failed') AS failed,
                   count(*) FILTER (WHERE status = 'completed') AS completed,
                   min(created_at) FILTER (WHERE status IN ('pending', 'claimed'))
                       AS oldest_outstanding_at
              FROM waha_commands
             GROUP BY worker_id
            """,
        )
        commands_by_worker = {row["worker_id"]: row for row in command_rows}

        workers = []
        for row in worker_rows:
            wid = row["worker_id"]
            assigned_sessions = int(assigned_by_worker.get(wid, {}).get("assigned", 0))
            cmd = commands_by_worker.get(wid, {})
            heartbeat_age = row["heartbeat_age_seconds"]
            available = (
                heartbeat_age is not None
                and heartbeat_age <= self._settings.waha_heartbeat_timeout_seconds
            )
            derived = (
                "healthy"
                if available and row["health"] == "ok"
                else "stale"
                if not available
                else "degraded"
                if row["health"] == "degraded"
                else "starting"
            )
            workers.append({
                "worker_id": wid,
                "region": row["region"],
                "agent_version": row["agent_version"],
                "management_protocol": row["management_protocol"],
                "management_capable": row["management_protocol"] >= 1,
                "reported_health": row["health"],
                "derived_availability": derived,
                "heartbeat_age_seconds": heartbeat_age,
                "first_registered_at": row["first_registered_at"],
                "max_sessions": row["max_sessions"],
                "assigned_sessions": assigned_sessions,
                "reported_active_sessions": row["active_sessions"],
                "capacity_utilization": (
                    round(assigned_sessions / row["max_sessions"], 3)
                    if row["max_sessions"]
                    else None
                ),
                "session_count_mismatch": (
                    abs(assigned_sessions - row["active_sessions"])
                    if row["active_sessions"] is not None
                    else None
                ),
                "reported_pending_backlog": row["pending"],
                "pending_commands": cmd.get("pending", 0),
                "claimed_commands": cmd.get("claimed", 0),
                "expired_command_leases": cmd.get("expired_leases", 0),
                "failed_commands": cmd.get("failed", 0),
                "completed_commands": cmd.get("completed", 0),
                "oldest_outstanding_command_at": cmd.get("oldest_outstanding_at"),
                "sessions_with_pending_effects": assigned_by_worker.get(wid, {}).get(
                    "pending_effects", 0
                ),
                "resources": row["resources"],
                "resources_observed_at": row["resources_observed_at"],
                "resources_available": row["resources"] is not None,
            })

        eligible = [
            w
            for w in workers
            if w["derived_availability"] == "healthy" and w["management_capable"]
        ]
        summary = {
            "registered": len(workers),
            "healthy": sum(1 for w in workers if w["derived_availability"] == "healthy"),
            "starting": sum(1 for w in workers if w["derived_availability"] == "starting"),
            "degraded": sum(1 for w in workers if w["derived_availability"] == "degraded"),
            "stale": sum(1 for w in workers if w["derived_availability"] == "stale"),
            "management_capable": sum(1 for w in workers if w["management_capable"]),
            "eligible_capacity": sum(w["max_sessions"] for w in eligible),
            "assigned_sessions": sum(w["assigned_sessions"] for w in eligible),
            "eligible_workers": len(eligible),
        }
        summary["remaining_capacity"] = max(
            0, summary["eligible_capacity"] - summary["assigned_sessions"]
        )
        summary["capacity_exhausted"] = (
            summary["eligible_workers"] > 0 and summary["remaining_capacity"] == 0
        )
        return {
            "available": True,
            "worker_summary": summary,
            "workers": workers,
            "notes": {
                "assigned_vs_reported": (
                    "Echo-assigned session count (waha_sessions) is "
                    "authoritative for allocation; agent-reported active "
                    "sessions is observational. Significant mismatches are "
                    "highlighted."
                ),
                "resources": (
                    "Resource metrics come from the optional agent "
                    "registration contract; missing telemetry shows N/A "
                    "and is not itself a health issue (unknown, not "
                    "critical)."
                ),
            },
        }

    # -- per-user monitoring health ------------------------------------------------------

    async def _users_section(
        self,
        session: AsyncSession,
        since: datetime,
        now: datetime,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        connections = await self._rows(
            session,
            """
            SELECT c.id, c.user_id::text AS user_id, c.provider,
                   c.provider_connection_id, c.connection_status,
                   c.provider_raw_status, c.updated_at, c.created_at,
                   c.last_authenticated_at, u.phone_number, u.first_name
              FROM whatsapp_connections c
              JOIN users u ON u.id = c.user_id
             WHERE (CAST(:user_id AS text) IS NULL OR c.user_id::text = :user_id)
             ORDER BY c.updated_at DESC
            """,
            {"user_id": user_id},
        )
        msg_stats = {
            row["user_id"]: row
            for row in await self._rows(
                session,
                """
                SELECT m.user_id::text AS user_id, count(*) AS messages,
                       count(*) FILTER (WHERE m.direction = 'inbound')
                           AS incoming,
                       count(*) FILTER (WHERE m.direction = 'outbound')
                           AS phone_sent,
                       count(*) FILTER (WHERE m.chat_id LIKE '%@g.us')
                           AS group_messages,
                       max(m.timestamp) AS last_whatsapp_at,
                       max(m.created_at) AS last_ingested_at
                  FROM messages m
                 WHERE m.created_at >= :since
                 GROUP BY m.user_id
                """,
                {"since": since},
            )
        }
        media_stats = {
            row["user_id"]: row
            for row in await self._rows(
                session,
                """
                SELECT m.user_id::text AS user_id,
                       count(*) FILTER (WHERE m.message_type IN
                           ('image', 'video', 'audio', 'document'))
                           AS media_discovered,
                       count(*) FILTER (WHERE EXISTS (
                           SELECT 1 FROM message_enrichments e
                            WHERE e.message_id = m.id
                              AND e.status = 'completed')) AS media_enriched,
                       count(*) FILTER (WHERE EXISTS (
                           SELECT 1 FROM message_enrichments e
                            WHERE e.message_id = m.id
                              AND e.status IN ('failed', 'terminal')))
                           AS media_failed
                  FROM messages m
                 WHERE m.created_at >= :since
                 GROUP BY m.user_id
                """,
                {"since": since},
            )
        }
        inbox_stats = await self._waha_inbox_by_session(session)
        session_rows = await self._rows(
            session,
            """
            SELECT s.user_id::text AS user_id, s.worker_id, s.status
                   AS session_status, s.raw_status, s.last_status_at,
                   s.created_at AS allocated_at
              FROM waha_sessions s
            """,
        )
        session_by_user = {row["user_id"]: row for row in session_rows}
        worker_rows = await self._rows(
            session,
            """
            SELECT worker_id, health, EXTRACT(EPOCH FROM (now() - last_seen_at))
                       AS heartbeat_age
              FROM waha_workers
            """,
        )
        workers = {row["worker_id"]: row for row in worker_rows}
        security_stats = {
            row["child_user_id"]: row
            for row in await self._rows(
                session,
                """
                SELECT child_user_id::text AS child_user_id, count(*) AS committed,
                       max(created_at) AS last_at,
                       count(*) FILTER (WHERE parent_should_know IS TRUE)
                           AS psk_true
                  FROM guard_simple_assessments
                 WHERE created_at >= :since
                 GROUP BY child_user_id
                """,
                {"since": since},
            )
        }
        exposure_stats = {
            row["child_user_id"]: row
            for row in await self._rows(
                session,
                """
                SELECT child_user_id::text AS child_user_id, count(*) AS committed,
                       max(created_at) AS last_at,
                       count(*) FILTER (WHERE significant_exposure IS TRUE)
                           AS significant
                  FROM guard_exposure_assessments
                 WHERE created_at >= :since
                 GROUP BY child_user_id
                """,
                {"since": since},
            )
        }
        chat_stats = {
            row["child_user_id"]: row
            for row in await self._rows(
                session,
                """
                SELECT child_user_id::text AS child_user_id,
                       count(*) FILTER (WHERE next_analysis_at IS NOT NULL
                           AND next_analysis_at <= now()
                           AND activity_version > last_analyzed_version)
                           AS overdue,
                       count(*) FILTER (WHERE next_analysis_at IS NOT NULL)
                           AS scheduled,
                       max(last_analysis_at) AS last_analysis_at
                  FROM guard_chat_state
                 GROUP BY child_user_id
                """,
            )
        }
        incident_stats = {
            row["child_user_id"]: row["incidents"]
            for row in await self._rows(
                session,
                """
                SELECT child_user_id, count(*) AS incidents
                  FROM alerted_incidents
                 WHERE created_at >= :since
                 GROUP BY child_user_id
                """,
                {"since": since},
            )
        }
        notification_stats = {
            row["child_user_id"]: row["sent"]
            for row in await self._rows(
                session,
                """
                SELECT child_user_id::text AS child_user_id, count(*) AS sent
                  FROM (
                    SELECT child_user_id, status
                      FROM guard_parent_attention_delivery_outcomes
                     WHERE created_at >= :since
                       AND status = 'sent'
                    UNION ALL
                    SELECT child_user_id, status
                      FROM guard_exposure_delivery_outcomes
                     WHERE created_at >= :since
                       AND status = 'sent'
                  ) sent_outcomes
                 GROUP BY child_user_id
                """,
                {"since": since},
            )
        }

        rows = []
        for conn in connections:
            uid = conn["user_id"]
            msg = msg_stats.get(uid, {})
            media = media_stats.get(uid, {})
            session_row = session_by_user.get(uid)
            inbox = (
                inbox_stats.get(conn["provider_connection_id"], {})
                if conn["provider"] == "waha"
                else {}
            )
            sec = security_stats.get(uid, {})
            exp = exposure_stats.get(uid, {})
            chat = chat_stats.get(uid, {})
            worker = workers.get(session_row["worker_id"]) if session_row else None
            worker_available = bool(
                worker is not None
                and worker["health"] == "ok"
                and worker["heartbeat_age"] is not None
                and worker["heartbeat_age"]
                <= self._settings.waha_heartbeat_timeout_seconds
            )
            pending = (inbox.get("pending") or 0) + (inbox.get("failed_retryable") or 0)
            exhausted = inbox.get("failed_exhausted") or 0
            expired = inbox.get("expired_leases") or 0
            status_age = (
                (now - conn["updated_at"]).total_seconds()
                if conn["updated_at"]
                else None
            )
            last_ingest = msg.get("last_ingested_at")
            monitoring_health, health_reason = self._user_health(
                provider=conn["provider"],
                connection_status=conn["connection_status"],
                status_age=status_age,
                worker_available=worker_available
                if conn["provider"] == "waha"
                else True,
                pending=pending,
                exhausted=exhausted,
                expired_leases=expired,
            )
            inactive_hours = (
                (now - last_ingest).total_seconds() / 3600 if last_ingest else None
            )
            rows.append({
                "user_id": uid,
                "display_name": conn["first_name"] or _mask_phone(conn["phone_number"]),
                "provider": conn["provider"],
                "connection_id": str(conn["id"]),
                "worker_id": session_row["worker_id"] if session_row else None,
                "worker_available": (
                    worker_available if conn["provider"] == "waha" else None
                ),
                "persisted_status": conn["connection_status"],
                "provider_raw_status": conn["provider_raw_status"],
                "status_age_seconds": status_age,
                "last_authenticated_at": conn["last_authenticated_at"],
                "connection_created_at": (
                    session_row["allocated_at"] if session_row else conn["created_at"]
                ),
                "messages_period": msg.get("messages", 0),
                "incoming_period": msg.get("incoming", 0),
                "phone_sent_period": msg.get("phone_sent", 0),
                "group_messages_period": msg.get("group_messages", 0),
                "last_whatsapp_at": msg.get("last_whatsapp_at"),
                "last_ingested_at": last_ingest,
                "inbox_pending": inbox.get("pending", 0) if inbox else 0,
                "inbox_failed_retryable": (
                    inbox.get("failed_retryable", 0) if inbox else 0
                ),
                "inbox_failed_exhausted": (
                    inbox.get("failed_exhausted", 0) if inbox else 0
                ),
                "inbox_expired_leases": (
                    inbox.get("expired_leases", 0) if inbox else 0
                ),
                "oldest_pending_at": inbox.get("oldest_pending_at"),
                "media_discovered_period": media.get("media_discovered", 0),
                "media_failed_period": media.get("media_failed", 0),
                "last_security_analysis_at": sec.get("last_at"),
                "last_exposure_analysis_at": exp.get("last_at"),
                "scheduled_analyses": chat.get("scheduled", 0),
                "overdue_analyses": chat.get("overdue", 0),
                "alerts_period": incident_stats.get(uid, 0),
                "notifications_sent_period": notification_stats.get(uid, 0),
                "monitoring_health": monitoring_health,
                "monitoring_health_reason": health_reason,
                "inactive_hours": inactive_hours,
                "message_activity": (
                    "active"
                    if inactive_hours is not None and inactive_hours < 24
                    else "inactive"
                    if inactive_hours is not None
                    else "no_messages"
                ),
            })
        return {
            "available": True,
            "users": rows,
            "notes": (
                "Message inactivity is reported separately from monitoring "
                "health — a child not using WhatsApp is not an ingestion "
                "failure. Drill into a user row for detail; no raw message "
                "content is exposed here."
            ),
        }

    def _user_health(
        self,
        *,
        provider: str,
        connection_status: str,
        status_age: float | None,
        worker_available: bool,
        pending: int,
        exhausted: int,
        expired_leases: int,
    ) -> tuple[str, str]:
        if provider == "waha" and not worker_available:
            return "worker_unavailable", "Assigned WAHA worker is stale or unhealthy"
        if connection_status in ("blocked", "suspended", "degraded"):
            return "disconnected", f"Connection status: {connection_status}"
        if connection_status == "pairing_required":
            return "awaiting_pairing", "Device pairing required"
        if connection_status in ("provisioning", "connecting"):
            return "provisioning", "Onboarding in progress"
        if exhausted > 0:
            return (
                "processing_failures",
                f"{exhausted} inbox message(s) exhausted retries",
            )
        if pending > 0 or expired_leases > 0:
            return (
                "ingestion_backlog",
                f"{pending} pending, {expired_leases} expired leases",
            )
        if (
            connection_status == "connected"
            and status_age is not None
            and status_age > 86400
        ):
            return "status_stale", "Connection status not updated in over 24h"
        if connection_status == "connected":
            return "connected_processing", "Connected"
        return "unknown", f"Connection status: {connection_status}"

    async def _waha_inbox_by_session(self, session: AsyncSession) -> dict[str, Any]:
        rows = await self._rows(
            session,
            """
            SELECT session,
                   count(*) FILTER (WHERE status = 'pending') AS pending,
                   count(*) FILTER (WHERE status = 'processing') AS processing,
                   count(*) FILTER (WHERE status = 'failed'
                       AND attempt_count < :max_attempts) AS failed_retryable,
                   count(*) FILTER (WHERE status = 'failed'
                       AND attempt_count >= :max_attempts) AS failed_exhausted,
                   count(*) FILTER (WHERE status = 'processing'
                       AND lease_expires_at < now()) AS expired_leases,
                   min(first_received_at) FILTER (
                       WHERE status IN ('pending', 'failed'))
                       AS oldest_pending_at
              FROM waha_event_inbox
             GROUP BY session
            """,
            {"max_attempts": self._settings.waha_message_max_attempts},
        )
        return {row["session"]: row for row in rows}

    # -- message ingestion ----------------------------------------------------------------

    async def _ingestion(
        self, session: AsyncSession, since: datetime, now: datetime
    ) -> dict[str, Any]:
        current = await self._one(
            session,
            """
            SELECT count(*) AS unique_rows,
                   count(*) FILTER (WHERE is_real = 1) AS real_messages,
                   count(*) FILTER (WHERE is_real = 0) AS non_real_events,
                   count(*) FILTER (WHERE from_me = 0) AS incoming,
                   count(*) FILTER (WHERE from_me = 1) AS phone_sent,
                   count(*) FILTER (WHERE status = 'pending') AS pending,
                   count(*) FILTER (WHERE status = 'processing') AS processing,
                   count(*) FILTER (WHERE status = 'processed') AS processed,
                   count(*) FILTER (WHERE status = 'failed'
                       AND attempt_count < :max_attempts) AS failed_retryable,
                   count(*) FILTER (WHERE status = 'failed'
                       AND attempt_count >= :max_attempts) AS failed_exhausted,
                   count(*) FILTER (WHERE status = 'processing'
                       AND lease_expires_at < now()) AS expired_leases,
                   count(*) FILTER (WHERE revision > 1) AS revised_rows,
                   min(first_received_at) FILTER (
                       WHERE status IN ('pending', 'failed'))
                       AS oldest_retryable_at,
                   min(first_received_at) FILTER (
                       WHERE status = 'failed'
                       AND attempt_count >= :max_attempts) AS oldest_exhausted_at
              FROM waha_event_inbox
             WHERE first_received_at >= :since
            """,
            {
                "max_attempts": self._settings.waha_message_max_attempts,
                "since": since,
            },
        )
        current_all = await self._one(
            session,
            """
            SELECT count(*) FILTER (WHERE status = 'pending') AS pending,
                   count(*) FILTER (WHERE status = 'processing') AS processing,
                   count(*) FILTER (WHERE status = 'failed'
                       AND attempt_count < :max_attempts) AS failed_retryable,
                   count(*) FILTER (WHERE status = 'failed'
                       AND attempt_count >= :max_attempts) AS failed_exhausted,
                   count(*) FILTER (WHERE status = 'processing'
                       AND lease_expires_at < now()) AS expired_leases,
                   min(first_received_at) FILTER (
                       WHERE status IN ('pending', 'failed')) AS oldest_pending_at,
                   count(*) FILTER (WHERE NOT EXISTS (
                       SELECT 1 FROM whatsapp_connections c
                        WHERE c.provider = 'waha'
                          AND c.provider_connection_id = waha_event_inbox.session))
                       AS unknown_session_rows
              FROM waha_event_inbox
            """,
            {"max_attempts": self._settings.waha_message_max_attempts},
        )
        batch_stats = await self._one(
            session,
            """
            SELECT count(*) AS batches,
                   coalesce(sum((metadata->>'received')::bigint), 0)
                       AS message_rows_submitted,
                   coalesce(sum((metadata->>'inserted')::bigint), 0)
                       AS new_unique_rows,
                   coalesce(sum((metadata->>'duplicated')::bigint), 0)
                       AS duplicate_deliveries,
                   coalesce(sum((metadata->>'updated')::bigint), 0)
                       AS updated_revisions,
                   coalesce(sum((metadata->>'in_batch_dup')::bigint), 0)
                       AS in_batch_duplicates,
                   coalesce(sum((metadata->>'discarded_revisions')::bigint), 0)
                       AS discarded_revisions,
                   coalesce(sum((metadata->>'body_bytes')::bigint), 0)
                       AS bytes_received,
                   coalesce(sum((metadata->>'persistence_ms')
                       ::double precision), 0) AS persistence_ms_total,
                   percentile_cont(0.5) WITHIN GROUP (
                       ORDER BY duration_ms) AS request_p50_ms,
                   percentile_cont(0.95) WITHIN GROUP (
                       ORDER BY duration_ms) AS request_p95_ms
              FROM pipeline_events
             WHERE event_name = 'waha.batch.ingested' AND created_at >= :since
            """,
            {"since": since},
        )
        rejected_rows = await self._rows(
            session,
            """
            SELECT event_name, status, count(*) AS count
              FROM pipeline_events
             WHERE pipeline = 'waha_ingestion'
               AND event_name IN ('waha.batch.rejected', 'waha.batch.failed',
                                  'waha.register.failed', 'waha.auth.rejected')
               AND created_at >= :since
             GROUP BY event_name, status
            """,
            {"since": since},
        )
        rejected = {
            f"{row['event_name']}:{row['status']}": row["count"]
            for row in rejected_rows
        }
        latency_queries = (
            (
                "whatsapp_to_receipt",
                """
                SELECT count(*) AS samples,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY lag_s) AS p50,
                       percentile_cont(0.95) WITHIN GROUP (ORDER BY lag_s) AS p95,
                       count(*) FILTER (WHERE lag_s > :sync_lag) AS sync_rows
                  FROM (SELECT EXTRACT(EPOCH FROM
                              (first_received_at - whatsapp_timestamp)) AS lag_s
                          FROM waha_event_inbox
                         WHERE first_received_at >= :since
                           AND status = 'processed') t
                """,
            ),
            (
                "agent_discovery_to_receipt",
                """
                SELECT count(*) AS samples,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY lag_s) AS p50,
                       percentile_cont(0.95) WITHIN GROUP (ORDER BY lag_s) AS p95,
                       NULL::bigint AS sync_rows
                  FROM (SELECT EXTRACT(EPOCH FROM
                              (first_received_at - agent_first_seen_at)) AS lag_s
                          FROM waha_event_inbox
                         WHERE first_received_at >= :since) t
                """,
            ),
            (
                "receipt_to_claim",
                """
                SELECT count(*) AS samples,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY lag_s) AS p50,
                       percentile_cont(0.95) WITHIN GROUP (ORDER BY lag_s) AS p95,
                       NULL::bigint AS sync_rows
                  FROM (SELECT EXTRACT(EPOCH FROM
                              (claimed_at - first_received_at)) AS lag_s
                          FROM waha_event_inbox
                         WHERE claimed_at >= :since) t
                """,
            ),
            (
                "claim_to_completion",
                """
                SELECT count(*) AS samples,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY lag_s) AS p50,
                       percentile_cont(0.95) WITHIN GROUP (ORDER BY lag_s) AS p95,
                       NULL::bigint AS sync_rows
                  FROM (SELECT EXTRACT(EPOCH FROM
                              (processed_at - claimed_at)) AS lag_s
                          FROM waha_event_inbox
                         WHERE processed_at >= :since) t
                """,
            ),
            (
                "receipt_to_canonical_ingestion",
                """
                SELECT count(*) AS samples,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY lag_s) AS p50,
                       percentile_cont(0.95) WITHIN GROUP (ORDER BY lag_s) AS p95,
                       NULL::bigint AS sync_rows
                  FROM (SELECT EXTRACT(EPOCH FROM
                              (m.created_at - i.first_received_at)) AS lag_s
                          FROM waha_event_inbox i
                          JOIN whatsapp_connections c
                            ON c.provider = 'waha'
                           AND c.provider_connection_id = i.session
                          JOIN messages m
                            ON m.connection_id = c.id
                           AND m.provider_message_id = i.message_id
                         WHERE i.status = 'processed'
                           AND i.first_received_at >= :since) t
                """,
            ),
        )
        latency: dict[str, Any] = {}
        for name, sql in latency_queries:
            row = await self._one(
                session,
                sql,
                {"since": since, "sync_lag": _SYNC_LAG_SECONDS},
            )
            latency[name] = {
                "samples": row["samples"],
                "p50_seconds": row["p50"],
                "p95_seconds": row["p95"],
                "sync_rows": row["sync_rows"],
            }
        baileys = await self._baileys_ingestion(session, since)
        trends = await self._ingestion_trends(session, since, now)
        return {
            "available": True,
            "waha_inbox": {
                "period": current,
                "current": current_all,
                "batch_delivery": batch_stats,
                "rejected": rejected,
                "latency": latency,
                "notes": (
                    "Windowed counters cover the selected period; 'current' "
                    "keys are live state. whatsapp_to_receipt and "
                    "agent_discovery_to_receipt are affected by device/agent "
                    "clock skew; sync_rows counts historical catch-up (over "
                    "10 min device lag) — do not read it as live-delivery "
                    "latency. claim_to_completion needs rows processed "
                    "after the claimed_at/processed_at columns were added; "
                    "older rows have no sample."
                ),
            },
            "baileys_inbox": baileys,
            "trends": trends,
        }

    async def _baileys_ingestion(
        self, session: AsyncSession, since: datetime
    ) -> dict[str, Any]:
        try:
            inbox = await self._rows(
                session,
                """
                SELECT event_type, count(*) AS received,
                       count(*) FILTER (WHERE processed_at IS NULL) AS unprocessed,
                       min(received_at) FILTER (WHERE processed_at IS NULL)
                           AS oldest_unprocessed_at,
                       max(received_at) AS last_received_at
                  FROM whatsapp_connector.event_inbox
                 WHERE received_at >= :since
                 GROUP BY event_type
                """,
                {"since": since},
            )
        except Exception as exc:  # noqa: BLE001 — optional connector schema
            _logger.warning("baileys inbox query failed error=%s", type(exc).__name__)
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001, S110 — best-effort
                pass
            inbox = []
        ledger = await self._rows(
            session,
            """
            SELECT status,
                   count(*) FILTER (WHERE attempt_count >= :max_attempts)
                       AS exhausted,
                   count(*) AS total,
                   max(updated_at) AS last_updated_at,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY
                       EXTRACT(EPOCH FROM (processed_at - claimed_at)))
                       AS claim_to_processed_p50_s,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY
                       EXTRACT(EPOCH FROM (processed_at - claimed_at)))
                       AS claim_to_processed_p95_s
              FROM baileys_connector_event_processing
             WHERE updated_at >= :since
             GROUP BY status
            """,
            {
                "max_attempts": self._settings.baileys_message_max_attempts,
                "since": since,
            },
        )
        current = await self._one(
            session,
            """
            SELECT count(*) FILTER (WHERE status = 'claimed') AS claimed,
                   count(*) FILTER (WHERE status = 'claimed'
                       AND claimed_at < now() - :lease_seconds * interval '1 second')
                       AS expired_claims,
                   count(*) FILTER (WHERE status = 'failed'
                       AND attempt_count < :max_attempts) AS failed_retryable,
                   count(*) FILTER (WHERE status = 'failed'
                       AND attempt_count >= :max_attempts) AS failed_exhausted,
                   count(*) FILTER (WHERE notification_state = 'pending')
                       AS notifications_pending
              FROM baileys_connector_event_processing
            """,
            {
                "max_attempts": self._settings.baileys_message_max_attempts,
                "lease_seconds": self._settings.baileys_lease_seconds,
            },
        )
        canonical = await self._one(
            session,
            """
            SELECT count(*) AS samples,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY lag_s) AS p50,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY lag_s) AS p95
              FROM (SELECT EXTRACT(EPOCH FROM (m.created_at - m.timestamp)) AS lag_s
                      FROM messages m
                      JOIN whatsapp_connections c ON c.id = m.connection_id
                     WHERE c.provider = 'baileys' AND m.created_at >= :since) t
            """,
            {"since": since},
        )
        return {
            "available": True,
            "period": inbox,
            "ledger": ledger,
            "current": current,
            "receipt_to_canonical_p50_s": canonical["p50"],
            "receipt_to_canonical_p95_s": canonical["p95"],
            "receipt_to_canonical_samples": canonical["samples"],
            "equivalence_notes": (
                "Baileys receipts come from the connector-owned inbox "
                "(processed_at marked by Echo); WAHA owns its inbox rows. "
                "Revision/retry semantics differ; treat counters as "
                "per-provider, not cross-provider comparable."
            ),
        }

    async def _ingestion_trends(
        self, session: AsyncSession, since: datetime, now: datetime
    ) -> dict[str, Any]:
        """Historical throughput / outcome / latency / backlog trends.

        Buckets adapt to the window: hourly up to 48h, daily beyond.
        Backlog history comes from the ``ops_metrics`` sampled gauges —
        empty until the metrics recorder has been running (shown as an
        explicit empty state, never fabricated).
        """
        span = (now - since).total_seconds()
        bucket = "hour" if span <= 48 * 3600 else "day"
        throughput = await self._rows(
            session,
            f"""
            SELECT date_trunc('{bucket}', m.created_at) AS bucket,
                   c.provider AS provider,
                   count(*) AS messages,
                   count(*) FILTER (WHERE m.direction = 'inbound') AS incoming,
                   count(*) FILTER (WHERE m.direction = 'outbound') AS phone_sent
              FROM messages m
              JOIN whatsapp_connections c ON c.id = m.connection_id
             WHERE m.created_at >= :since
             GROUP BY 1, 2
             ORDER BY 1, 2
            """,
            {"since": since},
        )
        outcomes = await self._rows(
            session,
            f"""
            SELECT date_trunc('{bucket}', first_received_at) AS bucket,
                   status, count(*) AS count
              FROM waha_event_inbox
             WHERE first_received_at >= :since
             GROUP BY 1, 2
             ORDER BY 1
            """,
            {"since": since},
        )
        latency_trend = await self._rows(
            session,
            f"""
            SELECT date_trunc('{bucket}', created_at) AS bucket,
                   count(*) AS batches,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)
                       AS p50_ms,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)
                       AS p95_ms
              FROM pipeline_events
             WHERE event_name = 'waha.batch.ingested'
               AND created_at >= :since
             GROUP BY 1
             ORDER BY 1
            """,
            {"since": since},
        )
        error_trend = await self._rows(
            session,
            f"""
            SELECT date_trunc('{bucket}', created_at) AS bucket,
                   count(*) AS failures
              FROM pipeline_events
             WHERE status IN ('failed', 'terminal', 'rejected')
               AND created_at >= :since
             GROUP BY 1
             ORDER BY 1
            """,
            {"since": since},
        )
        backlog_rows = await self._rows(
            session,
            """
            SELECT bucket_start AS bucket, metric, value
              FROM ops_metrics
             WHERE metric IN ('waha.inbox.pending', 'waha.inbox.failed',
                              'waha.worker.pending')
               AND bucket_start >= :since
             ORDER BY bucket_start
            """,
            {"since": since},
        )
        return {
            "bucket": bucket,
            "throughput": throughput,
            "inbox_outcomes": outcomes,
            "batch_latency": latency_trend,
            "pipeline_failures": error_trend,
            "backlog_gauges": backlog_rows,
            "notes": (
                "backlog_gauges come from ops_metrics samples collected "
                "by the metrics recorder — empty until it has been "
                "running; no historical backlog is fabricated from "
                "current state."
            ),
        }

    # -- media processing -------------------------------------------------------------------

    async def _media(
        self, session: AsyncSession, since: datetime, now: datetime
    ) -> dict[str, Any]:
        by_type = await self._rows(
            session,
            """
            SELECT m.message_type AS media_type,
                   count(*) AS discovered,
                   count(*) FILTER (WHERE EXISTS (
                       SELECT 1 FROM message_enrichments e
                        WHERE e.message_id = m.id
                          AND e.status = 'completed')) AS enriched,
                   count(*) FILTER (WHERE EXISTS (
                       SELECT 1 FROM message_enrichments e
                        WHERE e.message_id = m.id
                          AND e.status = 'pending')) AS pending,
                   count(*) FILTER (WHERE EXISTS (
                       SELECT 1 FROM message_enrichments e
                        WHERE e.message_id = m.id
                          AND e.status = 'processing')) AS processing,
                   count(*) FILTER (WHERE EXISTS (
                       SELECT 1 FROM message_enrichments e
                        WHERE e.message_id = m.id
                          AND e.status = 'failed')) AS failed,
                   count(*) FILTER (WHERE EXISTS (
                       SELECT 1 FROM message_enrichments e
                        WHERE e.message_id = m.id
                          AND e.status = 'terminal')) AS terminal,
                   count(*) FILTER (WHERE NOT EXISTS (
                       SELECT 1 FROM message_enrichments e
                        WHERE e.message_id = m.id)) AS no_enrichment_row
              FROM messages m
             WHERE m.created_at >= :since
               AND m.message_type IN ('image', 'video', 'audio', 'document')
             GROUP BY m.message_type
            """,
            {"since": since},
        )
        by_kind = await self._rows(
            session,
            """
            SELECT e.kind, e.status, count(*) AS count
              FROM message_enrichments e
              JOIN messages m ON m.id = e.message_id
             WHERE m.created_at >= :since
             GROUP BY e.kind, e.status
            """,
            {"since": since},
        )
        current = await self._rows(
            session,
            """
            SELECT status, count(*) AS count, min(created_at) AS oldest_at,
                   max(attempts) AS max_attempts_seen,
                   min(next_retry_at) AS next_retry_at
              FROM message_enrichments
             WHERE status IN ('pending', 'processing', 'failed')
             GROUP BY status
            """,
        )
        failure_categories = await self._rows(
            session,
            """
            SELECT error_type, count(*) AS count
              FROM pipeline_events
             WHERE event_name = 'media.enrichment.failed'
               AND created_at >= :since
             GROUP BY error_type
             ORDER BY count DESC
            """,
            {"since": since},
        )
        latency = await self._one(
            session,
            """
            SELECT count(*) AS samples,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)
                       AS p50,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)
                       AS p95
              FROM pipeline_events
             WHERE event_name = 'media.enrichment.completed'
               AND created_at >= :since
            """,
            {"since": since},
        )
        oldest_pending = next(
            (row["oldest_at"] for row in current if row["status"] == "pending"), None
        )
        return {
            "available": True,
            "by_media_type": by_type,
            "by_enrichment_kind": by_kind,
            "current_backlog": current,
            "oldest_pending_at": oldest_pending,
            "failure_categories": failure_categories,
            "enrichment_latency_p50_ms": latency["p50"],
            "enrichment_latency_p95_ms": latency["p95"],
            "enrichment_latency_samples": latency["samples"],
            "notes": (
                "Success-rate denominator: media messages with an "
                "enrichment row (completed + failed + terminal). "
                "no_enrichment_row counts media with no enrichment "
                "version — intentionally skipped (no target) or not yet "
                "discovered; best-effort. Download latency is inside "
                "enrichment latency (no separate stage timing — N/A). "
                "WahaMediaDownloadError / WahaMediaDecryptError / "
                "WahaMediaUnavailableError classify retrieval vs "
                "decryption; older events only show WahaMediaError."
            ),
        }

    # -- guard analysis ------------------------------------------------------------------------

    async def _guard_analysis(
        self, session: AsyncSession, since: datetime, now: datetime
    ) -> dict[str, Any]:
        security = await self._one(
            session,
            """
            SELECT count(*) AS committed,
                   count(*) FILTER (WHERE parent_should_know IS TRUE)
                       AS parent_should_know_true,
                   count(*) FILTER (WHERE parent_should_know IS FALSE)
                       AS parent_should_know_false,
                   count(*) FILTER (WHERE parent_should_know IS NULL)
                       AS parent_should_know_null,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY
                       EXTRACT(EPOCH FROM (created_at - window_end)))
                       AS analysis_delay_p50_s,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY
                       EXTRACT(EPOCH FROM (created_at - window_end)))
                       AS analysis_delay_p95_s
              FROM guard_simple_assessments
             WHERE created_at >= :since
            """,
            {"since": since},
        )
        security_versions = await self._rows(
            session,
            """
            SELECT model, analyzer_version, count(*) AS count
              FROM guard_simple_assessments
             WHERE created_at >= :since
             GROUP BY model, analyzer_version
            """,
            {"since": since},
        )
        exposure = await self._one(
            session,
            """
            SELECT count(*) AS committed,
                   count(*) FILTER (WHERE significant_exposure IS TRUE)
                       AS significant_true,
                   count(*) FILTER (WHERE significant_exposure IS FALSE)
                       AS significant_false,
                   count(*) FILTER (WHERE significant_exposure IS NULL)
                       AS significant_null
              FROM guard_exposure_assessments
             WHERE created_at >= :since
            """,
            {"since": since},
        )
        exposure_categories = await self._rows(
            session,
            """
            SELECT category, count(*) AS count
              FROM guard_exposure_assessments
             WHERE created_at >= :since
             GROUP BY category
            """,
            {"since": since},
        )
        ai_attempts = await self._rows(
            session,
            """
            SELECT operation, status, count(*) AS count,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)
                       AS latency_p50_ms,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)
                       AS latency_p95_ms
              FROM ai_usage_events
             WHERE operation IN ('guard_security', 'guard_exposure',
                                 'guard_incident_match')
               AND created_at >= :since
             GROUP BY operation, status
            """,
            {"since": since},
        )
        analysis_events = await self._rows(
            session,
            """
            SELECT event_name, metadata->>'analyzer' AS analyzer, status,
                   count(*) AS count,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)
                       AS latency_p50_ms,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)
                       AS latency_p95_ms
              FROM pipeline_events
             WHERE pipeline = 'guard_analysis' AND created_at >= :since
             GROUP BY event_name, metadata->>'analyzer', status
            """,
            {"since": since},
        )
        scheduled = await self._one(
            session,
            """
            SELECT count(*) FILTER (WHERE next_analysis_at IS NOT NULL)
                       AS scheduled,
                   count(*) FILTER (WHERE next_analysis_at IS NOT NULL
                       AND next_analysis_at <= now()
                       AND activity_version > last_analyzed_version) AS overdue,
                   count(*) FILTER (WHERE next_analysis_at IS NOT NULL
                       AND next_analysis_at <= now()
                       AND activity_version > last_analyzed_version
                       AND next_analysis_at
                           < now() - :overdue_seconds * interval '1 second')
                       AS significantly_overdue,
                   min(next_analysis_at) AS oldest_due_at,
                   min(pending_since) AS oldest_pending_since
              FROM guard_chat_state
            """,
            {"overdue_seconds": self._settings.analysis_overdue_seconds},
        )
        legacy_attempts = await self._rows(
            session,
            """
            SELECT status, count(*) AS count,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY
                       EXTRACT(EPOCH FROM (finished_at - started_at)))
                       AS latency_p50_s,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY
                       EXTRACT(EPOCH FROM (finished_at - started_at)))
                       AS latency_p95_s
              FROM guard_analysis_attempts
             WHERE started_at >= :since
             GROUP BY status
            """,
            {"since": since},
        )
        return {
            "available": True,
            "security": security,
            "security_versions": security_versions,
            "exposure": exposure,
            "exposure_categories": exposure_categories,
            "ai_call_attempts": ai_attempts,
            "analysis_events": analysis_events,
            "scheduled": scheduled,
            "legacy_attempts": legacy_attempts,
            "notes": (
                "Committed = an assessment row was written. AI call "
                "attempts (ai_usage_events) count billable provider calls "
                "including retries — an attempt is not a committed "
                "assessment. analysis_delay = created_at - window_end "
                "(last message in window to committed assessment). "
                "legacy_attempts cover the legacy worker's "
                "guard_analysis_attempts table only (empty under Guard "
                "Simple). Stale/superseded commits leave no assessment "
                "row — only legacy attempts record 'stale'."
            ),
        }

    # -- parent notifications ---------------------------------------------------------------------

    async def _notifications(
        self, session: AsyncSession, since: datetime, now: datetime
    ) -> dict[str, Any]:
        async def outcome_stats(table: str) -> list[dict[str, Any]]:
            return await self._rows(
                session,
                f"""
                SELECT status, count(*) AS count,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY
                           EXTRACT(EPOCH FROM (sent_at - scheduled_at)))
                           AS delivery_p50_s,
                       percentile_cont(0.95) WITHIN GROUP (ORDER BY
                           EXTRACT(EPOCH FROM (sent_at - scheduled_at)))
                           AS delivery_p95_s,
                       min(scheduled_at) AS oldest_scheduled_at,
                       max(failed_at) AS last_failed_at
                  FROM {table}
                 WHERE created_at >= :since
                 GROUP BY status
                """,
                {"since": since},
            )

        security_outcomes = await outcome_stats(
            "guard_parent_attention_delivery_outcomes"
        )
        exposure_outcomes = await outcome_stats("guard_exposure_delivery_outcomes")
        scheduled_actions = await self._rows(
            session,
            """
            SELECT status, count(*) AS count,
                   min(execute_at_utc) FILTER (WHERE status = 'pending')
                       AS oldest_pending_at,
                   count(*) FILTER (WHERE status = 'pending'
                       AND execute_at_utc
                           < now() - :overdue * interval '1 second')
                       AS overdue_pending,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY
                       EXTRACT(EPOCH FROM (executed_at - created_at)))
                       AS total_latency_p50_s,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY
                       EXTRACT(EPOCH FROM (executed_at - created_at)))
                       AS total_latency_p95_s
              FROM scheduled_actions
             WHERE created_at >= :since
               AND type IN ('send_bot_message', 'send_whatsapp_message')
             GROUP BY status
            """,
            {
                "since": since,
                "overdue": self._settings.notification_overdue_seconds,
            },
        )
        alert_evaluations = await self._rows(
            session,
            """
            SELECT outcome, count(*) AS count
              FROM guard_alert_evaluations
             WHERE evaluated_at >= :since
             GROUP BY outcome
            """,
            {"since": since},
        )
        alerts = await self._rows(
            session,
            """
            SELECT kind, status, count(*) AS count
              FROM guard_alerts
             WHERE created_at >= :since
             GROUP BY kind, status
            """,
            {"since": since},
        )
        incidents = await self._one(
            session,
            """
            SELECT count(*) AS created,
                   count(*) FILTER (WHERE closed_at IS NULL) AS open,
                   count(*) FILTER (WHERE related_incident_id IS NOT NULL)
                       AS recurrences,
                   count(*) FILTER (WHERE notification_version > 1)
                       AS re_notified
              FROM alerted_incidents
             WHERE created_at >= :since
            """,
            {"since": since},
        )

        def _status_count(rows: list[dict[str, Any]], status: str) -> int:
            return sum(row["count"] for row in rows if row["status"] == status)

        return {
            "available": True,
            "security_outcomes": security_outcomes,
            "exposure_outcomes": exposure_outcomes,
            "scheduled_actions": scheduled_actions,
            "legacy_alert_evaluations": alert_evaluations,
            "legacy_alerts": alerts,
            "incidents": incidents,
            "summary": {
                "sent": _status_count(security_outcomes, "sent")
                + _status_count(exposure_outcomes, "sent"),
                "failed": _status_count(security_outcomes, "failed")
                + _status_count(exposure_outcomes, "failed"),
                "scheduled": _status_count(security_outcomes, "scheduled")
                + _status_count(exposure_outcomes, "scheduled"),
            },
            "notes": (
                "'sent' means the WhatsApp Business API accepted the send "
                "(provider submission) — no device delivery or read "
                "receipts are captured anywhere in Echo. Duplicate-"
                "incident suppression counts are not persisted by the "
                "incident matcher; recurrences (linked incidents) and "
                "re-notifications (notification_version > 1) are shown "
                "instead. All sends go through the independent bot "
                "infrastructure, never through monitored WAHA accounts."
            ),
        }

    # -- WAHA management & onboarding ----------------------------------------------------------------

    async def _management(
        self, session: AsyncSession, since: datetime, now: datetime
    ) -> dict[str, Any]:
        session_statuses = await self._rows(
            session,
            """
            SELECT status, count(*) AS count,
                   count(*) FILTER (WHERE pending_effect IS NOT NULL)
                       AS pending_effects,
                   max(last_status_at) AS last_status_at
              FROM waha_sessions
             GROUP BY status
            """,
        )
        raw_statuses = await self._rows(
            session,
            """
            SELECT raw_status, count(*) AS count
              FROM waha_sessions
             WHERE raw_status IS NOT NULL
             GROUP BY raw_status
            """,
        )
        commands_by_action = await self._rows(
            session,
            """
            SELECT action, status, count(*) AS count, max(attempts)
                       AS max_attempts,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY
                       EXTRACT(EPOCH FROM (claimed_at - created_at)))
                       AS pickup_p50_s,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY
                       EXTRACT(EPOCH FROM (claimed_at - created_at)))
                       AS pickup_p95_s,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY
                       EXTRACT(EPOCH FROM (completed_at - claimed_at)))
                       AS execution_p50_s,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY
                       EXTRACT(EPOCH FROM (completed_at - claimed_at)))
                       AS execution_p95_s,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY
                       EXTRACT(EPOCH FROM (completed_at - created_at)))
                       AS total_p50_s,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY
                       EXTRACT(EPOCH FROM (completed_at - created_at)))
                       AS total_p95_s
              FROM waha_commands
             WHERE created_at >= :since
             GROUP BY action, status
            """,
            {"since": since},
        )
        current = await self._one(
            session,
            """
            SELECT count(*) FILTER (WHERE status = 'pending') AS pending,
                   count(*) FILTER (WHERE status = 'claimed') AS claimed,
                   count(*) FILTER (WHERE status = 'claimed'
                       AND lease_expires_at < now()) AS expired_leases,
                   count(*) FILTER (WHERE status = 'failed'
                       AND last_error = 'attempt limit reached') AS exhausted,
                   min(created_at) FILTER (WHERE status IN ('pending', 'claimed'))
                       AS oldest_outstanding_at
              FROM waha_commands
            """,
        )
        onboarding = await self._one(
            session,
            """
            SELECT count(*) AS attempts,
                   count(*) FILTER (WHERE qr_updated_at IS NOT NULL) AS qr_ready,
                   count(*) FILTER (WHERE status = 'connected') AS working,
                   count(*) FILTER (WHERE status = 'connected'
                       AND qr_updated_at IS NOT NULL) AS paired,
                   count(*) FILTER (WHERE status IN
                       ('provisioning', 'connecting', 'pairing_required')
                       AND created_at < now() - :stale_hours * interval '1 hour')
                       AS stuck,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY
                       EXTRACT(EPOCH FROM (qr_updated_at - created_at)))
                       AS allocation_to_qr_p50_s,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY
                       EXTRACT(EPOCH FROM (qr_updated_at - created_at)))
                       AS allocation_to_qr_p95_s,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY
                       EXTRACT(EPOCH FROM (last_status_at - qr_updated_at)))
                       AS qr_to_working_p50_s,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY
                       EXTRACT(EPOCH FROM (last_status_at - qr_updated_at)))
                       AS qr_to_working_p95_s
              FROM waha_sessions
             WHERE created_at >= :since
            """,
            {"since": since, "stale_hours": 24},
        )
        failed_commands = await self._rows(
            session,
            """
            SELECT worker_id, action, count(*) AS count
              FROM waha_commands
             WHERE status = 'failed' AND created_at >= :since
             GROUP BY worker_id, action
             ORDER BY count DESC
             LIMIT 20
            """,
            {"since": since},
        )
        return {
            "available": True,
            "session_statuses": session_statuses,
            "raw_statuses": raw_statuses,
            "commands_by_action": commands_by_action,
            "commands_current": current,
            "failed_commands_by_worker": failed_commands,
            "onboarding": onboarding,
            "notes": (
                "Onboarding durations derive from surviving timestamps "
                "(created_at -> qr_updated_at -> last_status_at); "
                "last_status_at reflects the latest lifecycle event, so "
                "qr_to_working is approximate when later events overwrote "
                "the WORKING transition. Command pickup latency uses the "
                "latest claim (claimed_at is overwritten per attempt). "
                "Read-only dashboard — no management actions, no QR "
                "images or token hashes are ever exposed."
            ),
        }

    # -- AI usage and costs -----------------------------------------------------------------------

    async def _ai_costs(
        self, session: AsyncSession, since: datetime, now: datetime
    ) -> dict[str, Any]:
        overview = await self._one(
            session,
            """
            SELECT coalesce(sum(estimated_cost_usd), 0) AS total_cost_usd,
                   count(*) AS calls,
                   count(*) FILTER (WHERE status = 'success') AS successful_calls,
                   count(*) FILTER (WHERE status = 'error') AS failed_calls,
                   coalesce(sum(prompt_tokens), 0) AS prompt_tokens,
                   coalesce(sum(completion_tokens), 0) AS completion_tokens,
                   coalesce(sum(cached_tokens), 0) AS cached_tokens,
                   count(DISTINCT logical_ref) AS logical_operations,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)
                       AS latency_p50_ms,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)
                       AS latency_p95_ms
              FROM ai_usage_events
             WHERE created_at >= :since
            """,
            {"since": since},
        )
        by_provider = await self._rows(
            session,
            """
            SELECT provider, count(*) AS calls,
                   coalesce(sum(estimated_cost_usd), 0) AS cost_usd,
                   coalesce(sum(total_tokens), 0) AS total_tokens
              FROM ai_usage_events
             WHERE created_at >= :since
             GROUP BY provider
            """,
            {"since": since},
        )
        by_operation = await self._rows(
            session,
            """
            SELECT operation, count(*) AS calls,
                   coalesce(sum(estimated_cost_usd), 0) AS cost_usd
              FROM ai_usage_events
             WHERE created_at >= :since
             GROUP BY operation
             ORDER BY cost_usd DESC
            """,
            {"since": since},
        )
        by_model = await self._rows(
            session,
            """
            SELECT model, count(*) AS calls,
                   coalesce(sum(estimated_cost_usd), 0) AS cost_usd,
                   coalesce(sum(prompt_tokens), 0) AS prompt_tokens,
                   coalesce(sum(completion_tokens), 0) AS completion_tokens,
                   coalesce(sum(cached_tokens), 0) AS cached_tokens
              FROM ai_usage_events
             WHERE created_at >= :since
             GROUP BY model
             ORDER BY cost_usd DESC
            """,
            {"since": since},
        )
        by_child = await self._rows(
            session,
            """
            SELECT child_user_id::text AS child_user_id, count(*) AS calls,
                   coalesce(sum(estimated_cost_usd), 0) AS cost_usd
              FROM ai_usage_events
             WHERE created_at >= :since AND child_user_id IS NOT NULL
             GROUP BY child_user_id
             ORDER BY cost_usd DESC
             LIMIT 50
            """,
            {"since": since},
        )
        daily = await self._rows(
            session,
            """
            SELECT date_trunc('day', created_at) AS day, count(*) AS calls,
                   coalesce(sum(estimated_cost_usd), 0) AS cost_usd,
                   coalesce(sum(total_tokens), 0) AS total_tokens
              FROM ai_usage_events
             WHERE created_at >= :since
             GROUP BY 1
             ORDER BY 1
            """,
            {"since": since},
        )
        committed = (
            await session.execute(
                text(
                    """
                    SELECT (SELECT count(*) FROM guard_simple_assessments
                             WHERE created_at >= :since)
                         + (SELECT count(*) FROM guard_exposure_assessments
                             WHERE created_at >= :since)
                    """
                ),
                {"since": since},
            )
        ).scalar() or 0
        monitored = (
            await session.execute(
                text("SELECT count(DISTINCT user_id) FROM whatsapp_connections")
            )
        ).scalar() or 0
        total_cost = float(overview["total_cost_usd"] or 0)
        return {
            "available": True,
            "overview": overview,
            "by_provider": by_provider,
            "by_operation": by_operation,
            "by_model": by_model,
            "by_child": by_child,
            "daily": daily,
            "cost_per_monitored_user_usd": (
                round(total_cost / monitored, 6) if monitored else None
            ),
            "cost_per_committed_analysis_usd": (
                round(total_cost / committed, 6) if committed else None
            ),
            "notes": (
                "estimated_cost_usd is list-price exposure from the "
                "versioned pricing.yaml (measured usage), NOT the billed "
                "amount — see AGENTS.md incentivized tier. Retries are "
                "approximated by calls - logical_operations (logical_ref "
                "groups related attempts; best-effort). Model latency "
                "requires call sites passing duration_ms; older rows have "
                "no sample (N/A). Infrastructure/VM costs are not in Echo."
            ),
        }

    # -- user drill-down helpers ---------------------------------------------------------------------

    async def _user_row(
        self, session: AsyncSession, user_id: str
    ) -> dict[str, Any] | None:
        rows = await self._rows(
            session,
            """
            SELECT u.id::text AS user_id, u.phone_number, u.first_name,
                   u.timezone, u.onboarding_status, u.created_at
              FROM users u
             WHERE u.id::text = :user_id
            """,
            {"user_id": user_id},
        )
        if not rows:
            return None
        data = dict(rows[0])
        data["phone_masked"] = _mask_phone(data.pop("phone_number"))
        return data

    async def _connections_for_user(
        self, session: AsyncSession, user_id: str
    ) -> list[dict[str, Any]]:
        return await self._rows(
            session,
            """
            SELECT provider, connection_status, provider_raw_status,
                   updated_at, created_at, last_authenticated_at
              FROM whatsapp_connections
             WHERE user_id::text = :user_id
             ORDER BY updated_at DESC
            """,
            {"user_id": user_id},
        )

    async def _user_messages(
        self, session: AsyncSession, user_id: str, since: datetime
    ) -> list[dict[str, Any]]:
        return await self._rows(
            session,
            """
            SELECT date_trunc('day', created_at) AS day, count(*) AS messages,
                   count(*) FILTER (WHERE direction = 'inbound') AS incoming,
                   count(*) FILTER (WHERE direction = 'outbound') AS phone_sent,
                   count(*) FILTER (WHERE chat_id LIKE '%@g.us') AS group_messages
              FROM messages
             WHERE user_id::text = :user_id AND created_at >= :since
             GROUP BY 1
             ORDER BY 1
            """,
            {"user_id": user_id, "since": since},
        )

    async def _user_inbox(
        self, session: AsyncSession, user_id: str
    ) -> dict[str, Any]:
        rows = await self._rows(
            session,
            """
            SELECT i.status, count(*) AS count,
                   min(i.first_received_at) FILTER (
                       WHERE i.status IN ('pending', 'failed')) AS oldest_at
              FROM waha_event_inbox i
              JOIN whatsapp_connections c
                ON c.provider = 'waha'
               AND c.provider_connection_id = i.session
             WHERE c.user_id::text = :user_id
             GROUP BY i.status
            """,
            {"user_id": user_id},
        )
        return {"waha_inbox": rows}

    async def _user_analyses(
        self, session: AsyncSession, user_id: str, since: datetime
    ) -> dict[str, Any]:
        security = await self._one(
            session,
            """
            SELECT count(*) AS committed,
                   count(*) FILTER (WHERE parent_should_know IS TRUE)
                       AS parent_should_know_true,
                   max(created_at) AS last_at
              FROM guard_simple_assessments
             WHERE child_user_id::text = :user_id AND created_at >= :since
            """,
            {"user_id": user_id, "since": since},
        )
        exposure = await self._one(
            session,
            """
            SELECT count(*) AS committed,
                   count(*) FILTER (WHERE significant_exposure IS TRUE)
                       AS significant_true,
                   max(created_at) AS last_at
              FROM guard_exposure_assessments
             WHERE child_user_id::text = :user_id AND created_at >= :since
            """,
            {"user_id": user_id, "since": since},
        )
        chats = await self._rows(
            session,
            """
            SELECT chat_id, chat_name, is_group,
                   activity_version - last_analyzed_version
                       AS pending_version_delta,
                   next_analysis_at, last_analysis_at,
                   (next_analysis_at <= now()
                       AND activity_version > last_analyzed_version) AS overdue
              FROM guard_chat_state
             WHERE child_user_id::text = :user_id
               AND (next_analysis_at IS NOT NULL
                    OR activity_version > last_analyzed_version)
            """,
            {"user_id": user_id},
        )
        return {
            "security": security,
            "exposure": exposure,
            "pending_chats": chats,
        }

    async def _user_notifications(
        self, session: AsyncSession, user_id: str, since: datetime
    ) -> dict[str, Any]:
        security = await self._rows(
            session,
            """
            SELECT status, count(*) AS count, max(updated_at) AS last_at
              FROM guard_parent_attention_delivery_outcomes
             WHERE child_user_id::text = :user_id AND created_at >= :since
             GROUP BY status
            """,
            {"user_id": user_id, "since": since},
        )
        exposure = await self._rows(
            session,
            """
            SELECT status, count(*) AS count, max(updated_at) AS last_at
              FROM guard_exposure_delivery_outcomes
             WHERE child_user_id::text = :user_id AND created_at >= :since
             GROUP BY status
            """,
            {"user_id": user_id, "since": since},
        )
        incidents = await self._one(
            session,
            """
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE closed_at IS NULL) AS open
              FROM alerted_incidents
             WHERE child_user_id = :user_id AND created_at >= :since
            """,
            {"user_id": user_id, "since": since},
        )
        return {
            "security_outcomes": security,
            "exposure_outcomes": exposure,
            "incidents": incidents,
        }


def _mask_phone(phone: str | None) -> str:
    if not phone:
        return "unknown"
    return phone[:-4] + "****" if len(phone) > 4 else "****"
