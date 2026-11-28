"""Postgres integration tests for the Echo Operations Center snapshot.

Covers the provider-neutral read model end-to-end: mixed WAHA + Baileys
fleets, worker states, inbox semantics (retryable vs exhausted, expired
leases, revisions), media metrics, Guard analysis, notifications,
management, AI costs, latency timestamps, and health-rule evaluation —
plus empty-database and no-WAHA-HTTP behavior.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from echo_v2.persistence.postgres_ops_metrics import PostgresOpsMetricsRepository
from echo_v2.persistence.postgres_waha_event_inbox import (
    PostgresWahaEventInboxRepository,
)
from echo_v2.services.ops_center_service import (
    OpsCenterService,
    OpsCenterSettings,
)
from echo_v2.services.ops_health_rules import OpsHealthRules

NOW = datetime(2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc)


class _FixedClock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


async def _exec(session_factory, sql: str, params: dict | None = None) -> None:
    async with session_factory() as session:
        await session.execute(text(sql), params or {})
        await session.commit()


async def insert_user(session_factory, phone: str) -> str:
    user_id = uuid.uuid4()
    await _exec(
        session_factory,
        "INSERT INTO users (id, phone_number) VALUES (:id, :phone)",
        {"id": user_id, "phone": phone},
    )
    return str(user_id)


async def insert_connection(
    session_factory,
    user_id: str,
    provider: str,
    status: str,
    provider_connection_id: str | None = None,
) -> str:
    conn_id = uuid.uuid4()
    await _exec(
        session_factory,
        """
        INSERT INTO whatsapp_connections (
            id, user_id, provider, provider_connection_id, credentials,
            webhook_token_hash, connection_status)
        VALUES (:id, :user_id, :provider, :pcid, :cred, :hash, :status)
        """,
        {
            "id": conn_id,
            "user_id": user_id,
            "provider": provider,
            "pcid": provider_connection_id or f"echo-{uuid.uuid4()}",
            "cred": b"x",
            "hash": b"y",
            "status": status,
        },
    )
    return str(conn_id)


async def insert_message(
    session_factory,
    *,
    user_id: str,
    connection_id: str,
    chat_id: str,
    direction: str = "inbound",
    message_type: str = "text",
    provider_message_id: str | None = None,
    created_at: datetime | None = None,
) -> str:
    message_id = uuid.uuid4()
    await _exec(
        session_factory,
        """
        INSERT INTO messages (id, user_id, connection_id, chat_id,
            provider_message_id, direction, timestamp, message_type)
        VALUES (:id, :user_id, :conn, :chat, :pmid, :direction, :ts, :mtype)
        """,
        {
            "id": message_id,
            "user_id": user_id,
            "conn": connection_id,
            "chat": chat_id,
            "pmid": provider_message_id or str(uuid.uuid4()),
            "direction": direction,
            "ts": created_at or NOW - timedelta(hours=1),
            "mtype": message_type,
        },
    )
    return str(message_id)


async def insert_worker(
    session_factory,
    worker_id: str,
    *,
    health: str = "ok",
    last_seen_at: datetime | None = None,
    max_sessions: int = 10,
    active_sessions: int = 0,
    pending: int = 0,
    management_protocol: int = 1,
    resources: dict | None = None,
) -> None:
    # ``last_seen_at=None`` (default) stamps DB now() so the worker's
    # heartbeat is fresh; the service derives age from the DB clock.
    seen_sql = ":seen" if last_seen_at is not None else "now()"
    await _exec(
        session_factory,
        f"""
        INSERT INTO waha_workers (worker_id, region, endpoint, max_sessions,
            active_sessions, agent_version, health, reported_heartbeat,
            last_seen_at, first_registered_at, pending, management_protocol,
            resources, resources_observed_at)
        VALUES (:wid, 'il-1', 'https://w', :max, :active, '1.0.0', :health,
                {seen_sql}, {seen_sql}, {seen_sql}, :pending, :proto,
                CAST(:resources AS jsonb), :observed)
        """,
        {
            "wid": worker_id,
            "max": max_sessions,
            "active": active_sessions,
            "health": health,
            "seen": last_seen_at,
            "pending": pending,
            "proto": management_protocol,
            "resources": (
                __import__("json").dumps(resources) if resources else None
            ),
            "observed": NOW if resources else None,
        },
    )


async def insert_inbox_row(
    session_factory,
    *,
    session: str,
    message_id: str,
    status: str = "pending",
    from_me: int = 0,
    is_real: int = 1,
    attempt_count: int = 0,
    revision: int = 1,
    lease_expires_at: datetime | None = None,
    whatsapp_timestamp: datetime | None = None,
    received_at: datetime | None = None,
    data: str = "{}",
) -> None:
    await _exec(
        session_factory,
        """
        INSERT INTO waha_event_inbox (session, message_id, chat_jid,
            whatsapp_timestamp, from_me, is_real, data, sha256, seq,
            agent_first_seen_at, first_received_at, last_updated_at, status,
            revision, batch_id, attempt_count, lease_expires_at)
        VALUES (:session, :mid, 'chat@c.us', :wts, :from_me, :is_real, :data,
                :sha, 1, :seen, :received, :received, :status, :revision,
                'batch-1', :attempts, :lease)
        """,
        {
            "session": session,
            "mid": message_id,
            "wts": whatsapp_timestamp or NOW - timedelta(minutes=5),
            "from_me": from_me,
            "is_real": is_real,
            "data": data,
            "sha": uuid.uuid4().hex + uuid.uuid4().hex,
            "seen": NOW - timedelta(minutes=5),
            "received": received_at or NOW - timedelta(minutes=4),
            "status": status,
            "revision": revision,
            "attempts": attempt_count,
            "lease": lease_expires_at,
        },
    )


def _service(session_factory, **overrides) -> OpsCenterService:
    settings = OpsCenterSettings(
        waha_heartbeat_timeout_seconds=90.0,
        waha_message_max_attempts=3,
        **overrides,
    )
    return OpsCenterService(
        session_factory=session_factory,
        settings=settings,
        clock=_FixedClock(NOW),
    )


async def test_snapshot_with_mixed_provider_fleet(
    session_factory, clean_db
) -> None:
    waha_child = await insert_user(session_factory, "+972500000001")
    baileys_child = await insert_user(session_factory, "+972500000002")
    baileys_pairing = await insert_user(session_factory, "+972500000003")
    await insert_connection(
        session_factory, waha_child, "waha", "connected", "echo-waha-1"
    )
    await insert_connection(
        session_factory, baileys_child, "baileys", "connected", "b-conn-1"
    )
    await insert_connection(
        session_factory, baileys_pairing, "baileys", "pairing_required", "b-conn-2"
    )
    await insert_worker(
        session_factory,
        "worker-1",
        active_sessions=1,
        resources={"vm_cpu_percent": 41.5, "waha_version": "WWE-4.6"},
    )
    await _exec(
        session_factory,
        """
        INSERT INTO waha_sessions (session_name, user_id, worker_id, status,
            raw_status, last_status_at, event_seq, qr_updated_at, created_at)
        VALUES ('echo-waha-1', :user, 'worker-1', 'connected', 'WORKING',
                :now, 1, :now - interval '10 minutes',
                :now - interval '1 hour')
        """,
        {"user": waha_child, "now": NOW},
    )

    snapshot = await _service(session_factory).snapshot(period="24h")
    sections = snapshot["sections"]

    # --- providers: WAHA users appear, Baileys users keep appearing,
    # no double counting across multiple connection records.
    providers = sections["providers"]
    assert providers["available"] is True
    assert providers["connection_summary"]["total_users"] == 3
    by_name = {p["provider"]: p for p in providers["providers"]}
    assert by_name["waha"]["connected"] == 1
    assert by_name["baileys"]["connected"] == 1
    assert by_name["baileys"]["pairing_required"] == 1
    assert "green" not in by_name  # no rows → no fabricated provider

    # --- WAHA infrastructure: healthy worker, assigned (authoritative) vs
    # reported counts, resources present.
    waha = sections["waha_infrastructure"]
    assert waha["worker_summary"]["healthy"] == 1
    assert waha["worker_summary"]["assigned_sessions"] == 1
    worker = waha["workers"][0]
    assert worker["assigned_sessions"] == 1
    assert worker["reported_active_sessions"] == 1
    assert worker["resources_available"] is True
    assert worker["resources"]["vm_cpu_percent"] == 41.5

    # --- per-user health: monitoring health vs pairing separated.
    users = {u["user_id"]: u for u in sections["users"]["users"]}
    assert users[waha_child]["monitoring_health"] == "connected_processing"
    assert users[waha_child]["worker_id"] == "worker-1"
    assert users[baileys_child]["monitoring_health"] == "connected_processing"
    assert users[baileys_pairing]["monitoring_health"] == "awaiting_pairing"
    # Phone numbers are masked, names preferred.
    assert users[waha_child]["display_name"].endswith("****")
    assert users[waha_child]["display_name"].startswith("+97250")

    # Overview KPIs derived across sections.
    overview = sections["overview"]
    assert overview["monitored_users"] == 3
    assert overview["connected_users"] == 2
    assert overview["pairing_required_users"] == 1
    assert overview["healthy_workers"] == 1


async def test_snapshot_inbox_semantics_and_latency(
    session_factory, clean_db
) -> None:
    child = await insert_user(session_factory, "+972500000010")
    conn = await insert_connection(
        session_factory, child, "waha", "connected", "echo-waha-1"
    )
    await insert_worker(session_factory, "worker-1")

    # pending, processed (with latency timestamps), retryable failed,
    # exhausted failed, expired lease, revision > 1, non-real, phone-sent.
    repo = PostgresWahaEventInboxRepository(session_factory)
    await insert_inbox_row(
        session_factory, session="echo-waha-1", message_id="m-pending"
    )
    await insert_inbox_row(
        session_factory,
        session="echo-waha-1",
        message_id="m-retryable",
        status="failed",
        attempt_count=1,
    )
    await insert_inbox_row(
        session_factory,
        session="echo-waha-1",
        message_id="m-exhausted",
        status="failed",
        attempt_count=3,
    )
    await insert_inbox_row(
        session_factory,
        session="echo-waha-1",
        message_id="m-expired",
        status="processing",
        attempt_count=1,
        lease_expires_at=NOW - timedelta(minutes=5),
    )
    await insert_inbox_row(
        session_factory,
        session="echo-waha-1",
        message_id="m-revised",
        revision=2,
    )
    await insert_inbox_row(
        session_factory,
        session="echo-waha-1",
        message_id="m-nonreal",
        is_real=0,
    )
    await insert_inbox_row(
        session_factory,
        session="echo-waha-1",
        message_id="m-fromme",
        from_me=1,
    )
    # A processed row that also became a canonical message: claim +
    # complete via the repository so claimed_at/processed_at are set.
    await insert_inbox_row(
        session_factory,
        session="echo-waha-1",
        message_id="m-done",
        whatsapp_timestamp=NOW - timedelta(minutes=12),
        received_at=NOW - timedelta(minutes=11),
    )
    await insert_message(
        session_factory,
        user_id=child,
        connection_id=conn,
        chat_id="chat@c.us",
        provider_message_id="m-done",
    )
    claimed = await repo.claim_pending(
        "consumer-1", batch_size=1, lease_seconds=60
    )
    done = [c for c in claimed if c.message.message_id == "m-done"]
    assert done
    assert await repo.complete_claimed(done[0])

    # One ingested batch event with delivery counters.
    await _exec(
        session_factory,
        """
        INSERT INTO pipeline_events (event_name, pipeline, status, duration_ms,
            metadata, created_at)
        VALUES ('waha.batch.ingested', 'waha_ingestion', 'success', 42.0,
                CAST(:meta AS jsonb), :at)
        """,
        {
            "meta": (
                '{"received": 7, "acked": 7, "inserted": 5, "duplicated": 2,'
                ' "updated": 0, "in_batch_dup": 1, "discarded_revisions": 0,'
                ' "body_bytes": 1024, "persistence_ms": 9.5}'
            ),
            "at": NOW - timedelta(hours=1),
        },
    )

    snapshot = await _service(session_factory).snapshot(period="24h")
    ingestion = snapshot["sections"]["ingestion"]
    assert ingestion["available"] is True
    waha = ingestion["waha_inbox"]
    assert waha["period"]["unique_rows"] == 8
    assert waha["period"]["real_messages"] == 7
    assert waha["period"]["non_real_events"] == 1
    assert waha["period"]["incoming"] == 7
    assert waha["period"]["phone_sent"] == 1
    assert waha["period"]["pending"] == 4
    assert waha["period"]["processed"] == 1
    assert waha["period"]["failed_retryable"] == 1
    assert waha["period"]["failed_exhausted"] == 1
    assert waha["period"]["expired_leases"] == 1
    assert waha["period"]["revised_rows"] == 1
    current = waha["current"]
    assert current["failed_exhausted"] == 1
    assert current["expired_leases"] == 1
    assert current["unknown_session_rows"] == 0

    delivery = waha["batch_delivery"]
    assert delivery["batches"] == 1
    assert delivery["message_rows_submitted"] == 7
    assert delivery["new_unique_rows"] == 5
    assert delivery["duplicate_deliveries"] == 2
    assert delivery["bytes_received"] == 1024

    latency = waha["latency"]
    assert latency["receipt_to_claim"]["samples"] >= 1
    assert latency["claim_to_completion"]["samples"] >= 1
    assert latency["whatsapp_to_receipt"]["samples"] >= 1
    assert (
        latency["receipt_to_canonical_ingestion"]["samples"] == 1
    )
    assert latency["whatsapp_to_receipt"]["p50_seconds"] is not None


async def test_snapshot_media_guard_notifications_management_costs(
    session_factory, clean_db
) -> None:
    child = await insert_user(session_factory, "+972500000020")
    conn = await insert_connection(
        session_factory, child, "waha", "connected", "echo-waha-1"
    )
    await insert_worker(session_factory, "worker-1")
    await _exec(
        session_factory,
        """
        INSERT INTO waha_sessions (session_name, user_id, worker_id, status,
            raw_status, last_status_at, event_seq, qr_updated_at, created_at)
        VALUES ('echo-waha-1', :user, 'worker-1', 'connected', 'WORKING',
                :now, 1, :now - interval '20 minutes',
                :now - interval '1 hour')
        """,
        {"user": child, "now": NOW},
    )
    await _exec(
        session_factory,
        """
        INSERT INTO waha_commands (command_id, worker_id, session_name,
            action, status, created_at, claimed_at, completed_at, attempts)
        VALUES (gen_random_uuid(), 'worker-1', 'echo-waha-1',
                'create_and_start', 'completed', :c, :c2, :c3, 1)
        """,
        {
            "c": NOW - timedelta(hours=1),
            "c2": NOW - timedelta(minutes=59),
            "c3": NOW - timedelta(minutes=58),
        },
    )

    # Media: image completed, video failed terminal, audio pending.
    image = await insert_message(
        session_factory, user_id=child, connection_id=conn,
        chat_id="c@c.us", message_type="image",
    )
    video = await insert_message(
        session_factory, user_id=child, connection_id=conn,
        chat_id="c@c.us", message_type="video",
    )
    await insert_message(
        session_factory, user_id=child, connection_id=conn,
        chat_id="g@g.us", message_type="audio",
    )
    await _exec(
        session_factory,
        """
        INSERT INTO message_enrichments (id, message_id, enrichment_version,
            status, kind, attempts, created_at, updated_at)
        VALUES (gen_random_uuid(), :mid, 'v1', 'completed', 'media_summary', 1,
                :at, :at)
        """,
        {"mid": image, "at": NOW - timedelta(hours=1)},
    )
    await _exec(
        session_factory,
        """
        INSERT INTO message_enrichments (id, message_id, enrichment_version,
            status, kind, error, attempts, created_at, updated_at)
        VALUES (gen_random_uuid(), :mid, 'v1', 'terminal', 'media_summary',
                'WahaMediaDecryptError: mismatch', 3, :at, :at)
        """,
        {"mid": video, "at": NOW - timedelta(hours=1)},
    )
    await _exec(
        session_factory,
        """
        INSERT INTO pipeline_events (event_name, pipeline, status, duration_ms,
            error_type, metadata, created_at)
        VALUES ('media.enrichment.completed', 'media_enrichment', 'completed',
                1500.0, NULL, '{"kind": "media_summary"}', :at),
               ('media.enrichment.failed', 'media_enrichment', 'terminal',
                NULL, 'WahaMediaDecryptError', '{}', :at)
        """,
        {"at": NOW - timedelta(hours=1)},
    )

    # Guard: security assessments with real decision distribution,
    # exposure assessment, overdue scheduled work.
    await _exec(
        session_factory,
        """
        INSERT INTO guard_simple_assessments (id, child_user_id, connection_id,
            chat_id, chat_type, timezone, window_start, window_end,
            parent_should_know, child_role, summary, reason,
            evidence_message_ids, input_message_ids, model, prompt_version,
            analyzer_version, diagnostics, created_at)
        VALUES (gen_random_uuid(), :child, :conn, 'c@c.us', 'private',
                'Asia/Jerusalem', :ws, :we, TRUE, 'target', 's', 'r', '[]',
                '[]', 'gpt-5.6-luna', 'pv', 'av', '{}', :at),
               (gen_random_uuid(), :child, :conn, 'c@c.us', 'private',
                'Asia/Jerusalem', :ws, :we, FALSE, 'target', 's2', 'r2', '[]',
                '[]', 'gpt-5.6-luna', 'pv', 'av', '{}', :at),
               (gen_random_uuid(), :child, :conn, 'c@c.us', 'private',
                'Asia/Jerusalem', :ws, :we, NULL, 'unknown', 's3', 'r3', '[]',
                '[]', 'gpt-5.6-luna', 'pv', 'av', '{}', :at),
               (gen_random_uuid(), :child, :conn, 'c@c.us', 'private',
                'Asia/Jerusalem', :ws, :we, NULL, 'participant', 's4', 'r4', '[]',
                '[]', 'gpt-5.6-luna', 'pv', 'av', '{"semantic_uncertainty": true}', :at)
        """,
        {
            "child": child,
            "conn": conn,
            "ws": NOW - timedelta(hours=2),
            "we": NOW - timedelta(hours=1),
            "at": NOW - timedelta(hours=1),
        },
    )
    await _exec(
        session_factory,
        """
        INSERT INTO guard_exposure_assessments (id, child_user_id,
            connection_id, chat_id, chat_type, timezone, window_start,
            window_end, target_version, significant_exposure, category,
            summary, reason, evidence_message_ids, input_message_ids, model,
            prompt_version, analyzer_version, created_at)
        VALUES (gen_random_uuid(), :child, :conn, 'g@g.us', 'group',
                'Asia/Jerusalem', :ws, :we, 1, TRUE, 'bullying', 's', 'r',
                '[]', '[]', 'gpt-5.6-luna', 'pv', 'av', :at)
        """,
        {
            "child": child,
            "conn": conn,
            "ws": NOW - timedelta(hours=2),
            "we": NOW - timedelta(hours=1),
            "at": NOW - timedelta(hours=1),
        },
    )
    await _exec(
        session_factory,
        """
        INSERT INTO guard_chat_state (child_user_id, chat_id, chat_name,
            activity_version, last_message_at, last_analyzed_version,
            next_analysis_at, pending_since, is_group, created_at, updated_at)
        VALUES (:child, 'c@c.us', 'chat', 5, :at, 0, :due, :due, false,
                :at, :at)
        """,
        {
            "child": child,
            "at": NOW - timedelta(hours=1),
            "due": NOW - timedelta(minutes=30),
        },
    )

    # Notifications: security sent + failed outcomes, incident, AI usage.
    guardian = await insert_user(session_factory, "+972500000021")
    guardian2 = await insert_user(session_factory, "+972500000022")
    assessment_id = await _scalar(
        session_factory,
        "SELECT id FROM guard_simple_assessments LIMIT 1",
    )
    await _exec(
        session_factory,
        """
        INSERT INTO guard_parent_attention_delivery_outcomes (
            assessment_id, guardian_user_id, child_user_id, chat_id, status,
            reason, scheduled_at, sent_at, failed_at, created_at, updated_at)
        VALUES (:aid, :guardian, :child, 'c@c.us', 'sent', 'ok', :s, :s,
                NULL, :s, :s),
               (:aid, :guardian2, :child, 'c@c.us', 'failed', 'boom', :s,
                NULL, :s, :s, :s)
        """,
        {"aid": assessment_id, "child": child, "guardian": guardian,
         "guardian2": guardian2, "s": NOW - timedelta(hours=1)},
    )
    await _exec(
        session_factory,
        """
        INSERT INTO alerted_incidents (id, child_user_id, chat_id, alert_type,
            summary, severity_summary, notified_severity_summary,
            evidence_message_ids, assessment_ids, last_seen_at, created_at,
            updated_at, notification_version)
        VALUES ('inc-1', :child, 'c@c.us', 'security', 'sum', 'sev', 'sev',
                '[]', '[]', :at, :at, :at, 2)
        """,
        {"child": child, "at": NOW - timedelta(hours=1)},
    )
    await _exec(
        session_factory,
        """
        INSERT INTO ai_usage_events (invocation_id, provider, operation,
            model, usage_type, prompt_tokens, completion_tokens, total_tokens,
            estimated_cost_usd, cost_source, status, duration_ms, created_at)
        VALUES (gen_random_uuid(), 'openai', 'guard_security',
                'gpt-5.6-luna', 'tokens', 1000, 200, 1200, 0.001,
                'measured_usage_priced', 'success', 850.0, :at),
               (gen_random_uuid(), 'google', 'media_video',
                'gemini-3.5-flash-lite', 'tokens', 500, 100, 600, 0.0005,
                'measured_usage_priced', 'success', 2500.0, :at)
        """,
        {"at": NOW - timedelta(hours=1)},
    )

    snapshot = await _service(session_factory).snapshot(period="24h")
    sections = snapshot["sections"]

    media = sections["media"]
    by_type = {row["media_type"]: row for row in media["by_media_type"]}
    assert by_type["image"]["enriched"] == 1
    assert by_type["video"]["terminal"] == 1
    assert by_type["audio"]["pending"] == 0  # no enrichment row → skip
    assert by_type["audio"]["no_enrichment_row"] == 1
    failure_categories = {
        row["error_type"]: row["count"] for row in media["failure_categories"]
    }
    assert failure_categories["WahaMediaDecryptError"] == 1
    assert media["enrichment_latency_samples"] == 1

    guard = sections["guard_analysis"]
    assert guard["security"]["committed"] == 4
    assert guard["security"]["parent_should_know_true"] == 1
    assert guard["security"]["parent_should_know_false"] == 1
    assert guard["security"]["parent_should_know_null"] == 2
    assert guard["security"]["parent_should_know_semantic_uncertain"] == 1
    assert guard["security"]["parent_should_know_operational_uncertain"] == 1
    assert guard["exposure"]["committed"] == 1
    assert guard["exposure"]["significant_true"] == 1
    assert guard["scheduled"]["overdue"] == 1
    assert guard["scheduled"]["significantly_overdue"] == 1

    notifications = sections["notifications"]
    assert notifications["summary"]["sent"] == 1
    assert notifications["summary"]["failed"] == 1
    assert notifications["incidents"]["re_notified"] == 1

    management = sections["management"]
    assert management["session_statuses"][0]["status"] == "connected"
    assert management["onboarding"]["paired"] == 1
    commands = {
        (row["action"], row["status"]): row
        for row in management["commands_by_action"]
    }
    assert commands[("create_and_start", "completed")]["count"] == 1
    assert (
        commands[("create_and_start", "completed")]["pickup_p50_s"] is not None
    )

    costs = sections["ai_costs"]
    assert float(costs["overview"]["total_cost_usd"]) == pytest.approx(0.0015)
    assert costs["overview"]["calls"] == 2
    assert costs["overview"]["latency_p50_ms"] is not None

    users = {u["user_id"]: u for u in sections["users"]["users"]}
    assert users[child]["overdue_analyses"] == 1
    assert users[child]["media_discovered_period"] == 3
    assert users[child]["media_failed_period"] == 1
    assert users[child]["notifications_sent_period"] == 1


async def test_health_rules_flag_exhausted_retries_and_stale_workers(
    session_factory, clean_db
) -> None:
    child = await insert_user(session_factory, "+972500000030")
    await insert_connection(
        session_factory, child, "waha", "connected", "echo-waha-1"
    )
    await insert_worker(
        session_factory,
        "worker-stale",
        last_seen_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    await insert_inbox_row(
        session_factory,
        session="echo-waha-1",
        message_id="m-dead",
        status="failed",
        attempt_count=3,
    )
    old_media_message = await insert_message(
        session_factory,
        user_id=child,
        connection_id=await _scalar(
            session_factory,
            "SELECT id FROM whatsapp_connections LIMIT 1",
        ),
        chat_id="c@c.us",
        message_type="image",
    )
    await _exec(
        session_factory,
        """
        INSERT INTO message_enrichments (id, message_id, enrichment_version,
            status, kind, attempts, created_at, updated_at)
        VALUES (gen_random_uuid(), :mid, 'v1', 'pending',
                'media_summary', 0, :old, :old)
        """,
        {"mid": old_media_message, "old": NOW - timedelta(hours=48)},
    )

    service = _service(session_factory, media_url_expiry_seconds=86400)
    snapshot = await service.snapshot(period="24h")
    health = OpsHealthRules(service._settings).evaluate(snapshot)

    assert health["status"] == "critical"
    categories = {issue["category"] for issue in health["issues"]}
    assert "worker_stale" in categories
    assert "inbox_retries_exhausted" in categories
    assert "media_backlog_url_expiry" in categories
    stale = next(i for i in health["issues"] if i["category"] == "worker_stale")
    assert stale["worker_id"] == "worker-stale"
    assert "worker-stale" in stale["message"]


async def test_snapshot_empty_database_is_all_na_not_zero(
    session_factory, clean_db
) -> None:
    snapshot = await _service(session_factory).snapshot(period="24h")
    sections = snapshot["sections"]
    for name in (
        "providers",
        "waha_infrastructure",
        "users",
        "ingestion",
        "media",
        "guard_analysis",
        "notifications",
        "management",
        "ai_costs",
    ):
        assert sections[name]["available"] is True, name
    overview = sections["overview"]
    assert overview["monitored_users"] == 0
    assert overview["healthy_workers"] == 0
    # Provider with no rows still renders N/A backlog rather than zero.
    providers = sections["providers"]["providers"]
    assert providers == []
    health = OpsHealthRules().evaluate(snapshot)
    assert health["status"] == "healthy"
    assert health["issues"] == []


async def test_snapshot_section_failure_is_isolated(session_factory, clean_db) -> None:
    child = await insert_user(session_factory, "+972500000040")
    await insert_connection(
        session_factory, child, "waha", "connected", "echo-waha-1"
    )

    class _BrokenMediaSection:
        async def __call__(self, session, since, now):
            raise RuntimeError("boom")

    service = _service(session_factory)
    service._media = _BrokenMediaSection()  # type: ignore[method-assign]
    snapshot = await service.snapshot(period="24h")
    sections = snapshot["sections"]
    assert sections["media"] == {"available": False, "error": "RuntimeError"}
    assert sections["providers"]["available"] is True
    health = OpsHealthRules().evaluate(snapshot)
    assert health["status"] == "warning"
    assert "media" in health["unavailable_sections"]


async def test_user_detail_drill_down(session_factory, clean_db) -> None:
    child = await insert_user(session_factory, "+972500000050")
    conn = await insert_connection(
        session_factory, child, "waha", "connected", "echo-waha-1"
    )
    await insert_message(
        session_factory, user_id=child, connection_id=conn, chat_id="c@c.us"
    )
    await insert_inbox_row(
        session_factory, session="echo-waha-1", message_id="m-1"
    )

    detail = await _service(session_factory).user_detail(child)
    assert detail["available"] is True
    assert detail["user"]["phone_masked"].endswith("****")
    assert detail["connections"][0]["provider"] == "waha"
    assert detail["messages_7d"][0]["messages"] == 1
    assert detail["inbox"]["waha_inbox"][0]["status"] == "pending"

    missing = await _service(session_factory).user_detail(str(uuid.uuid4()))
    assert missing["available"] is False


async def test_ops_metrics_flush_and_series(session_factory, clean_db) -> None:
    repo = PostgresOpsMetricsRepository(session_factory)
    from echo_v2.persistence.ops_metrics import OpsMetricPoint

    bucket = NOW.replace(second=0, microsecond=0)
    await repo.flush([
        OpsMetricPoint(
            bucket_start=bucket, metric="waha.batch.latency_ms",
            provider="waha", count=1, sum_ms=100.0, max_ms=100.0,
        ),
        OpsMetricPoint(
            bucket_start=bucket, metric="waha.inbox.pending",
            provider="waha", value=5,
        ),
    ])
    # Concurrent flush to the same bucket merges (upsert-increment).
    await repo.flush([
        OpsMetricPoint(
            bucket_start=bucket, metric="waha.batch.latency_ms",
            provider="waha", count=2, sum_ms=50.0, max_ms=300.0,
        ),
        OpsMetricPoint(
            bucket_start=bucket, metric="waha.inbox.pending",
            provider="waha", value=7,
        ),
    ])
    latency = await repo.series(
        metric="waha.batch.latency_ms", since=bucket - timedelta(minutes=1),
        provider="waha",
    )
    assert latency[0].count == 3
    assert latency[0].sum_ms == pytest.approx(150.0)
    assert latency[0].max_ms == pytest.approx(300.0)
    gauge = await repo.series(
        metric="waha.inbox.pending", since=bucket - timedelta(minutes=1),
        provider="waha",
    )
    assert gauge[0].value == 7  # last write wins

    deleted = await repo.delete_before(bucket - timedelta(days=31))
    assert deleted == 0


async def _scalar(session_factory, sql: str):
    async with session_factory() as session:
        result = await session.execute(text(sql))
        return result.scalar()


async def test_error_view_merges_sanitizes_and_paginates(
    session_factory, clean_db
) -> None:
    from echo_v2.services.ops_error_view import OpsErrorView, OpsErrorViewSettings

    child = await insert_user(session_factory, "+972500000060")
    await insert_connection(
        session_factory, child, "waha", "connected", "echo-waha-1"
    )
    await insert_worker(session_factory, "worker-1")
    # Two failed inbox rows (one exhausted), one with a messy error string.
    await insert_inbox_row(
        session_factory,
        session="echo-waha-1",
        message_id="m-1",
        status="failed",
        attempt_count=1,
    )
    await insert_inbox_row(
        session_factory,
        session="echo-waha-1",
        message_id="m-2",
        status="failed",
        attempt_count=3,
        data="{}",
    )
    await _exec(
        session_factory,
        """
        UPDATE waha_event_inbox
           SET last_error = :err
         WHERE message_id = 'm-1'
        """,
        {"err": "boom\nwith newlines and a very long " + "x" * 400},
    )
    await _exec(
        session_factory,
        """
        INSERT INTO ai_usage_events (invocation_id, provider, operation, model,
            usage_type, cost_source, status, error_type, created_at)
        VALUES (gen_random_uuid(), 'openai', 'guard_security', 'gpt-5.6-luna',
                'tokens', 'unknown', 'error', 'RateLimitError', :at)
        """,
        {"at": NOW - timedelta(hours=1)},
    )

    view = OpsErrorView(
        session_factory=session_factory,
        settings=OpsErrorViewSettings(waha_message_max_attempts=3),
    )
    result = await view.list_errors()
    assert result["available"] is True
    # Sanitization: no control characters, truncated description.
    for error in result["errors"]:
        assert "\n" not in (error["description"] or "")
        if error["description"]:
            assert len(error["description"]) <= 200
    states = {e["state"] for e in result["errors"]}
    assert "terminal" in states
    subsystems = {e["subsystem"] for e in result["errors"]}
    assert "waha_message_consumer" in subsystems
    assert "ai_provider" in subsystems
    assert result["total_in_window"] >= 3

    # Filtering by subsystem.
    filtered = await view.list_errors(subsystem="ai_provider")
    assert all(e["subsystem"] == "ai_provider" for e in filtered["errors"])

    # Pagination with a small page size returns disjoint pages.
    page1_result = await view.list_errors(limit=2)
    page1 = {e["description"] for e in page1_result["errors"]}
    page2 = await view.list_errors(limit=2, offset=2)
    assert page2["errors"]
    assert not (page1 & {e["description"] for e in page2["errors"]})

    # Aggregates by category.
    categories = {
        (a["subsystem"], a["category"]): a["count"]
        for a in result["aggregates"]
    }
    assert categories[("waha_message_consumer", "inbox_retry_exhausted")] == 1


async def test_error_view_survives_missing_connector_schema(
    session_factory, clean_db
) -> None:
    from echo_v2.services.ops_error_view import OpsErrorView

    view = OpsErrorView(session_factory=session_factory)
    result = await view.list_errors()
    assert result["available"] is True
    # The Baileys source is unavailable but the rest of the view still loads.
    assert isinstance(result["errors"], list)


async def test_baileys_backlog_counts_connector_inbox(
    session_factory, clean_db
) -> None:
    """Regression: FILTER (EXISTS ...) was a syntax error — the whole Baileys
    backlog silently reported nulls via the 'inbox unavailable' fallback."""
    await _exec(
        session_factory,
        """
        CREATE SCHEMA IF NOT EXISTS whatsapp_connector;
        CREATE TABLE IF NOT EXISTS whatsapp_connector.event_inbox (
            id bigserial primary key,
            event_id text, connection_id text, event_type text,
            provider_message_id text, payload text,
            received_at timestamptz, processed_at timestamptz);
        """,
    )
    await _exec(
        session_factory,
        """
        INSERT INTO whatsapp_connector.event_inbox
            (event_id, connection_id, event_type, provider_message_id,
             payload, received_at, processed_at)
        VALUES
            ('e-pending', 'c1', 'message', 'm1', '{}', :now, NULL),
            ('e-done', 'c1', 'message', 'm2', '{}', :now, :now),
            ('e-state', 'c1', 'connection_state', 'm3', '{}', :now, NULL)
        """,
        {"now": NOW},
    )
    await _exec(
        session_factory,
        """
        INSERT INTO baileys_connector_event_processing
            (event_id, inbox_id, status, attempt_count)
        VALUES ('x1', 2, 'failed', 10)
        """,
    )
    baileys_child = await insert_user(session_factory, "+972500000009")
    await insert_connection(
        session_factory, baileys_child, "baileys", "connected", "b-conn-9"
    )

    snapshot = await _service(session_factory).snapshot(period="24h")
    provider = {
        p["provider"]: p
        for p in snapshot["sections"]["providers"]["providers"]
    }["baileys"]
    assert provider["pending_backlog"] == 1  # message events only, not state
    assert provider["failed_backlog"] == 1
    assert provider["oldest_pending_at"] is not None
