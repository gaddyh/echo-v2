"""Local preview server for the Echo Operations Center dashboard.

Starts a disposable Postgres (testcontainer), applies migrations, seeds a
realistic mixed-provider fleet with telemetry, and serves the REAL admin
routes (page + API) on localhost:8765. Prints the admin session link.
Not for production — local preview only.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import uvicorn
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

sys.path.insert(0, "src")
sys.path.insert(0, "tests/persistence")

from echo_v2.app.guard_operations_routes import build_guard_operations_router
from echo_v2.persistence.db import run_migrations
from echo_v2.persistence.waiting_list_tokens import (
    InMemoryWaitingListSessionRepository,
)
from echo_v2.services.ops_center_service import (
    OpsCenterService,
    OpsCenterSettings,
)
from echo_v2.services.ops_error_view import OpsErrorView, OpsErrorViewSettings
from echo_v2.services.ops_health_rules import OpsHealthRules
from echo_v2.services.waiting_list_token_service import WaitingListTokenService

ADMIN_USER = "00000000-0000-0000-0000-000000000001"
NOW = datetime.now(timezone.utc)


async def seed(session_factory) -> None:
    async def exec(sql: str, params: dict | None = None) -> None:
        async with session_factory() as session:
            await session.execute(text(sql), params or {})
            await session.commit()

    def user(phone: str, name: str | None = None) -> str:
        return str(uuid.uuid4())

    # Users: admin, 2 WAHA children, 1 Baileys child, 1 Baileys pairing.
    ids = {
        "admin": ADMIN_USER,
        "shay": user("+972546610653"),
        "dana": user("+972541112233"),
        "bailey_kid": user("+972501112222"),
        "bailey_pairing": user("+972503334444"),
    }
    for key, uid in ids.items():
        await exec(
            "INSERT INTO users (id, phone_number, first_name) "
            "VALUES (:id, :phone, :name) ON CONFLICT (id) DO NOTHING",
            {
                "id": uid,
                "phone": f"+9725000{abs(hash(key)) % 1000000:06d}",
                "name": key if key != "admin" else "Admin",
            },
        )

    async def connection(uid: str, provider: str, status: str, pcid: str) -> str:
        cid = uuid.uuid4()
        await exec(
            """
            INSERT INTO whatsapp_connections (id, user_id, provider,
                provider_connection_id, credentials, webhook_token_hash,
                connection_status, updated_at)
            VALUES (:id, :uid, :provider, :pcid, 'x', 'y', :status, now())
            """,
            {"id": cid, "uid": uid, "provider": provider, "pcid": pcid, "status": status},
        )
        return str(cid)

    waha_conn_shay = await connection(ids["shay"], "waha", "connected", "echo-waha-1")
    waha_conn_dana = await connection(ids["dana"], "waha", "connected", "echo-waha-2")
    await connection(ids["bailey_kid"], "baileys", "connected", "b-conn-1")
    await connection(ids["bailey_pairing"], "baileys", "pairing_required", "b-conn-2")

    # Workers: one healthy with resources, one stale, one degraded.
    for wid, health, seen_minutes_ago, resources in (
        (
            "upcloud-vm-01",
            "ok",
            0,
            {
                "vm_cpu_percent": 37.5,
                "vm_ram_total_bytes": 2147483648,
                "vm_ram_available_bytes": 1073741824,
                "waha_container_memory_bytes": 512000000,
                "waha_container_cpu_percent": 22.0,
                "agent_memory_bytes": 48000000,
                "disk_total_bytes": 80000000000,
                "disk_used_bytes": 41000000000,
                "sqlite_db_bytes": 1048576,
                "agent_state_db_bytes": 2048,
                "vm_uptime_seconds": 864000,
                "waha_container_uptime_seconds": 36000,
                "agent_uptime_seconds": 860000,
                "waha_version": "WWE-4.6",
                "last_batch_delivery_at": (NOW - timedelta(seconds=30)).timestamp(),
            },
        ),
        ("upcloud-vm-02", "degraded", 1, None),
        ("upcloud-vm-03", "ok", 240, None),
    ):
        await exec(
            """
            INSERT INTO waha_workers (worker_id, region, endpoint, max_sessions,
                active_sessions, agent_version, health, reported_heartbeat,
                last_seen_at, first_registered_at, pending, management_protocol,
                resources, resources_observed_at)
            VALUES (:wid, 'il-1', 'https://w.example', 10, :active, '1.4.0',
                    :health, now() - :seen, now() - :seen, now() - interval '10 days',
                    :pending, 1, CAST(:resources AS jsonb), :observed)
            """,
            {
                "wid": wid,
                "active": 1 if health == "ok" else 0,
                "health": health,
                "seen": timedelta(minutes=seen_minutes_ago),
                "pending": 3 if wid == "upcloud-vm-01" else 0,
                "resources": json.dumps(resources) if resources else None,
                "observed": NOW if resources else None,
            },
        )

    # WAHA sessions for the two WAHA children.
    for uid, session_name, worker, status in (
        (ids["shay"], "echo-waha-1", "upcloud-vm-01", "connected"),
        (ids["dana"], "echo-waha-2", "upcloud-vm-01", "connected"),
    ):
        await exec(
            """
            INSERT INTO waha_sessions (session_name, user_id, worker_id, status,
                raw_status, last_status_at, event_seq, qr_updated_at, created_at)
            VALUES (:sn, :uid, :worker, :status, 'WORKING', now(),
                    1, now() - interval '40 minutes', now() - interval '2 hours')
            """,
            {"sn": session_name, "uid": uid, "worker": worker, "status": status},
        )
        await exec(
            """
            INSERT INTO waha_commands (command_id, worker_id, session_name, action,
                status, created_at, claimed_at, completed_at, attempts)
            VALUES (gen_random_uuid(), :worker, :sn, 'create_and_start',
                    'completed', now() - interval '2 hours',
                    now() - interval '2 hours' + interval '20 seconds',
                    now() - interval '2 hours' + interval '85 seconds', 1)
            """,
            {"worker": worker, "sn": session_name},
        )
    await exec(
        """
        INSERT INTO waha_commands (command_id, worker_id, session_name, action,
            status, created_at, attempts, last_error)
        VALUES (gen_random_uuid(), 'upcloud-vm-01', 'echo-waha-9', 'get_qr',
                'pending', now() - interval '10 minutes', 1, NULL),
               (gen_random_uuid(), 'upcloud-vm-02', 'echo-waha-8', 'start',
                'failed', now() - interval '30 minutes', 5,
                'attempt limit reached')
        """
    )

    # Inbox rows: processed, pending, retryable, exhausted, expired lease.
    for mid, status, attempts, has_lease, from_me, is_real in (
        ("m-1", "processed", 1, False, 0, 1),
        ("m-2", "processed", 1, False, 0, 1),
        ("m-3", "processed", 1, False, 1, 1),
        ("m-4", "pending", 0, False, 0, 1),
        ("m-5", "failed", 1, False, 0, 1),
        ("m-6", "failed", 10, False, 0, 1),
        ("m-7", "processing", 1, True, 0, 1),
        ("m-8", "pending", 0, False, 0, 0),
    ):
        lease_sql = (
            "now() - interval '10 minutes'" if has_lease else "NULL"
        )
        await exec(
            f"""
            INSERT INTO waha_event_inbox (session, message_id, chat_jid,
                whatsapp_timestamp, from_me, is_real, data, sha256, seq,
                agent_first_seen_at, first_received_at, last_updated_at, status,
                revision, batch_id, attempt_count, lease_expires_at, claimed_at,
                processed_at, last_error)
            VALUES ('echo-waha-1', :mid, '972501234567@c.us',
                    now() - interval '30 minutes', :from_me, :is_real,
                    '{{"text": "hi"}}', :sha, 1, now() - interval '29 minutes',
                    now() - interval '28 minutes', now(), :status, 1, 'batch-1',
                    :attempts, {lease_sql}, now() - interval '27 minutes',
                    CASE WHEN :status = 'processed'
                         THEN now() - interval '26 minutes' ELSE NULL END,
                    CASE WHEN :status = 'failed'
                         THEN 'WahaMediaDecryptError: plaintext SHA-256 mismatch'
                         ELSE NULL END)
            """,
            {
                "mid": mid,
                "from_me": from_me,
                "is_real": is_real,
                "sha": uuid.uuid4().hex + uuid.uuid4().hex,
                "status": status,
                "attempts": attempts,
            },
        )

    # Messages across providers with media + enrichments.
    for i in range(24):
        mtype = ["text", "text", "image", "video", "audio", "document"][i % 6]
        direction = "inbound" if i % 4 else "outbound"
        chat = "class-group@g.us" if i % 3 else "friend@c.us"
        mid = uuid.uuid4()
        await exec(
            """
            INSERT INTO messages (id, user_id, connection_id, chat_id,
                provider_message_id, direction, timestamp, message_type, text)
            VALUES (:id, :uid, :conn, :chat, :pmid, :direction,
                    now() - :ago, :mtype, 'hello')
            """,
            {
                "id": mid,
                "uid": ids["shay"] if i % 2 else ids["bailey_kid"],
                "conn": waha_conn_shay if i % 2 else waha_conn_dana,
                "chat": chat,
                "pmid": f"msg-{i}",
                "direction": direction,
                "ago": timedelta(hours=1 + i),
                "mtype": mtype,
            },
        )
        if mtype in ("image", "video", "audio", "document"):
            status = (
                "completed" if i % 3 == 0
                else "terminal" if i % 3 == 1 else "pending"
            )
            await exec(
                """
                INSERT INTO message_enrichments (id, message_id,
                    enrichment_version, status, kind, model, error, attempts,
                    created_at, updated_at)
                VALUES (gen_random_uuid(), :mid, '2026-10-23.video.1', :status,
                        :kind, 'gemini-3.5-flash-lite', :error, 1, now(), now())
                """,
                {
                    "mid": mid,
                    "status": status,
                    "kind": "transcription" if mtype == "audio" else "media_summary",
                    "error": "VideoTooLargeError" if status == "terminal" else None,
                },
            )

    # Guard assessments, chat state, delivery outcomes, incidents, AI usage.
    for uid, psk, sig in (
        (ids["shay"], True, True),
        (ids["dana"], False, None),
    ):
        await exec(
            """
            INSERT INTO guard_simple_assessments (id, child_user_id,
                connection_id, chat_id, chat_type, timezone, window_start,
                window_end, parent_should_know, child_role, summary, reason,
                evidence_message_ids, input_message_ids, model, prompt_version,
                analyzer_version, created_at)
            VALUES (gen_random_uuid(), :uid,
                    (SELECT id FROM whatsapp_connections WHERE user_id = :uid
                     AND provider = 'waha'),
                    'friend@c.us', 'private', 'Asia/Jerusalem',
                    now() - interval '3 hours', now() - interval '1 hour',
                    :psk, 'target', 'summary', 'reason', '[]', '[]',
                    'gpt-5.6-luna', 'baseline-b-v4', '2026-10-21.2',
                    now() - interval '1 hour')
            """,
            {"uid": uid, "psk": psk},
        )
        if sig is not None:
            await exec(
                """
                INSERT INTO guard_exposure_assessments (id, child_user_id,
                    connection_id, chat_id, chat_type, timezone, window_start,
                    window_end, target_version, significant_exposure, category,
                    summary, reason, evidence_message_ids, input_message_ids,
                    model, prompt_version, analyzer_version, created_at)
                VALUES (gen_random_uuid(), :uid,
                        (SELECT id FROM whatsapp_connections WHERE user_id = :uid
                         AND provider = 'waha'),
                        'class-group@g.us', 'group', 'Asia/Jerusalem',
                        now() - interval '3 hours', now() - interval '1 hour',
                        1, :sig, 'bullying', 'summary', 'reason', '[]', '[]',
                        'gpt-5.6-luna', 'exposure-shadow-v2', '2026-10-21.1',
                        now() - interval '1 hour')
                """,
                {"uid": uid, "sig": sig},
            )
    await exec(
        """
        INSERT INTO guard_chat_state (child_user_id, chat_id, chat_name,
            activity_version, last_message_at, last_analyzed_version,
            next_analysis_at, pending_since, is_group, created_at, updated_at)
        VALUES (:uid, 'friend@c.us', 'Friend', 7, now() - interval '2 hours', 5,
                now() - interval '30 minutes', now() - interval '2 hours', false,
                now(), now())
        """,
        {"uid": ids["shay"]},
    )
    assessment_id = await _scalar(
        session_factory,
        "SELECT id FROM guard_simple_assessments LIMIT 1",
    )
    guardian2 = "00000000-0000-0000-0000-000000000002"
    await exec(
        "INSERT INTO users (id, phone_number, first_name) VALUES "
        "(:id, '+972500000099', 'Guardian2') ON CONFLICT (id) DO NOTHING",
        {"id": guardian2},
    )
    for status, guardian in (("sent", ADMIN_USER), ("failed", guardian2)):
        await exec(
            """
            INSERT INTO guard_parent_attention_delivery_outcomes (
                assessment_id, guardian_user_id, child_user_id, chat_id, status,
                reason, scheduled_at, sent_at, failed_at, created_at, updated_at)
            VALUES (:aid, :guardian,
                    (SELECT child_user_id FROM guard_simple_assessments
                     WHERE id = :aid), 'friend@c.us', :status, 'ok',
                    now() - interval '50 minutes',
                    CASE WHEN :status = 'sent'
                         THEN now() - interval '49 minutes' ELSE NULL END,
                    CASE WHEN :status = 'failed'
                         THEN now() - interval '49 minutes' ELSE NULL END,
                    now() - interval '49 minutes', now() - interval '49 minutes')
            """,
            {"aid": assessment_id, "guardian": guardian, "status": status},
        )
    await exec(
        """
        INSERT INTO alerted_incidents (id, child_user_id, chat_id, alert_type,
            summary, severity_summary, notified_severity_summary,
            evidence_message_ids, assessment_ids, last_seen_at, created_at,
            updated_at, notification_version)
        VALUES ('inc-prev', :uid, 'friend@c.us', 'security', 'summary', 'sev',
                'sev', '[]', '[]', now(), now() - interval '2 hours', now(), 1)
        """,
        {"uid": ids["shay"]},
    )
    for provider, operation, model, cost, duration in (
        ("openai", "guard_security", "gpt-5.6-luna", 0.0031, 1400.0),
        ("openai", "guard_exposure", "gpt-5.6-luna", 0.0022, 1100.0),
        ("google", "media_video", "gemini-3.5-flash-lite", 0.0004, 12000.0),
        ("modal", "media_audio", "whisper-large-v3", 0.006, None),
    ):
        await exec(
            """
            INSERT INTO ai_usage_events (invocation_id, provider, operation,
                model, usage_type, prompt_tokens, completion_tokens,
                total_tokens, estimated_cost_usd, cost_source, status,
                duration_ms, created_at)
            VALUES (gen_random_uuid(), :provider, :operation, :model,
                    CASE WHEN :provider = 'modal' THEN 'audio_seconds'
                         ELSE 'tokens' END,
                    1500, 300, 1800, :cost, 'measured_usage_priced', 'success',
                    :duration, now() - interval '2 hours')
            """,
            {
                "provider": provider,
                "operation": operation,
                "model": model,
                "cost": cost,
                "duration": duration,
            },
        )
    # Pipeline events: batches + media + analysis + failures.
    for i in range(12):
        await exec(
            """
            INSERT INTO pipeline_events (event_name, pipeline, status,
                duration_ms, event_id, metadata, created_at)
            VALUES ('waha.batch.ingested', 'waha_ingestion', 'success',
                    38.0 + :i, 'preview-batch-:i',
                    CAST(:meta AS jsonb), now() - :ago)
            """,
            {
                "i": i,
                "ago": timedelta(hours=i),
                "meta": json.dumps(
                    {
                        "received": 6,
                        "acked": 6,
                        "inserted": 5,
                        "duplicated": 1,
                        "updated": 0,
                        "in_batch_dup": 1,
                        "discarded_revisions": 0,
                        "body_bytes": 2048,
                        "persistence_ms": 11.2,
                    }
                ),
            },
        )
    await exec(
        """
        INSERT INTO pipeline_events (event_name, pipeline, status, duration_ms,
            error_type, error_message, metadata, created_at)
        VALUES ('media.enrichment.completed', 'media_enrichment', 'completed',
                14500.0, NULL, NULL, '{"kind": "media_summary"}',
                now() - interval '2 hours'),
               ('media.enrichment.failed', 'media_enrichment', 'terminal', NULL,
                'WahaMediaDecryptError', 'plaintext SHA-256 mismatch',
                '{"retry_at": null}', now() - interval '2 hours'),
               ('guard.analysis.completed', 'guard_analysis', 'committed',
                2300.0, NULL, NULL,
                '{"analyzer": "security", "model": "gpt-5.6-luna"}',
                now() - interval '1 hour'),
               ('guard.analysis.failed', 'guard_analysis', 'failed', NULL,
                'OpenAIError', 'rate limited', '{}', now() - interval '3 hours'),
               ('waha.batch.rejected', 'waha_ingestion', 'validation', NULL,
                'validation', 'invalid sha256', '{}', now() - interval '4 hours')
        """
    )


async def _scalar(session_factory, sql: str):
    async with session_factory() as session:
        result = await session.execute(text(sql))
        return result.scalar()


async def main() -> None:
    container = PostgresContainer("postgres:16-alpine", driver="psycopg")
    container.start()
    url = container.get_connection_url()
    saved = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = ""
    try:
        run_migrations(url)
    finally:
        if saved is not None:
            os.environ["DATABASE_URL"] = saved
        else:
            os.environ.pop("DATABASE_URL", None)

    engine = create_async_engine(url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    await seed(session_factory)

    sessions = InMemoryWaitingListSessionRepository()
    tokens = WaitingListTokenService(sessions)
    # Pre-issue the admin session so the preview link works directly.
    session_id, token = await tokens.issue(user_id=ADMIN_USER)

    ops = OpsCenterService(
        session_factory=session_factory,
        settings=OpsCenterSettings(),
    )
    rules = OpsHealthRules()
    error_view = OpsErrorView(
        session_factory=session_factory,
        settings=OpsErrorViewSettings(),
    )

    # A stand-in activity service (the ops center page itself is the target).
    class _ActivityStub:
        async def list_activity(self, **kwargs):
            return None

    app = FastAPI()
    app.include_router(
        build_guard_operations_router(
            service=_ActivityStub(),  # type: ignore[arg-type]
            token_service=tokens,
            admin_user_ids={ADMIN_USER},
            ops_center_service=ops,
            ops_health_rules=rules,
            ops_error_view=error_view,
        )
    )

    print("=" * 70)
    print("Echo Operations Center — local preview (seeded, real routes)")
    print("=" * 70)
    print("1. Open this link once to set your admin session cookie:")
    print(f"   http://localhost:8765/q/guard/operations/{token}")
    print("2. Then open the dashboard:")
    print("   http://localhost:8765/debug/guard/admin")
    print(f"   (session id: {session_id})")
    print("=" * 70)
    sys.stdout.flush()

    config = uvicorn.Config(app, host="0.0.0.0", port=8765, log_level="warning")
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        container.stop()


if __name__ == "__main__":
    asyncio.run(main())
