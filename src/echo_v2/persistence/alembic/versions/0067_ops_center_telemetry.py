"""Echo Operations Center: operational counters, timestamps, worker resources.

Revision ID: 0067_ops_center_telemetry
Revises: 0066_waha_agent_management

Three telemetry additions for the Echo Operations Center dashboard:

1. ``ops_metrics`` — time-bucketed, low-cardinality aggregate counters and
   sampled gauges (bucket_start, metric, provider, worker_id). One row per
   bucket per metric, not per request: empty management polls and heartbeats
   never create rows, and counters are upsert-incremented in batches. Written
   best-effort by the ops metrics recorder; never on the ingestion critical
   path. Retention is enforced by the recorder (default 30 days).

2. ``waha_event_inbox`` gains ``claimed_at`` / ``processed_at`` so ingestion
   latency can be measured in stages (receipt → claim → completion) instead
   of conflating them into ``last_updated_at``. Existing rows are NULL — the
   dashboard shows N/A for them.

3. ``waha_workers`` gains the backward-compatible ``resources`` JSONB +
   ``resources_observed_at`` (latest agent-reported VM/container/disk
   telemetry, sent on registration/heartbeat). Old agents simply never send
   the object; the columns stay NULL and the dashboard shows N/A.

4. ``ai_usage_events`` gains ``duration_ms`` — model latency measured in-run
   where call sites already time the provider call.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0067_ops_center_telemetry"
down_revision = "0066_waha_agent_management"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ops_metrics",
        sa.Column("bucket_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("metric", sa.Text(), primary_key=True),
        sa.Column("provider", sa.Text(), primary_key=True),
        sa.Column("worker_id", sa.Text(), primary_key=True),
        sa.Column("value", sa.Float(), nullable=False, server_default="0"),
        sa.Column("count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("sum_ms", sa.Float(), nullable=True),
        sa.Column("max_ms", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_ops_metrics_metric_bucket",
        "ops_metrics",
        ["metric", "bucket_start"],
    )

    op.add_column(
        "waha_event_inbox",
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "waha_event_inbox",
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "waha_workers",
        sa.Column(
            "resources",
            sa.dialects.postgresql.JSONB(),
            nullable=True,
        ),
    )
    op.add_column(
        "waha_workers",
        sa.Column("resources_observed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "ai_usage_events",
        sa.Column("duration_ms", sa.Float(), nullable=True),
    )

    # Dashboard windowed aggregations.
    op.create_index("ix_messages_created_at", "messages", ["created_at"])
    op.create_index(
        "ix_guard_exposure_delivery_outcomes_status_updated",
        "guard_exposure_delivery_outcomes",
        ["status", "updated_at"],
    )
    op.create_index(
        "ix_baileys_event_processing_failures",
        "baileys_connector_event_processing",
        ["updated_at"],
        postgresql_where=sa.text("status = 'failed' OR last_error IS NOT NULL"),
    )
    op.create_index(
        "ix_pipeline_events_pipeline_created",
        "pipeline_events",
        ["pipeline", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_pipeline_events_pipeline_created", table_name="pipeline_events")
    op.drop_index(
        "ix_baileys_event_processing_failures",
        table_name="baileys_connector_event_processing",
    )
    op.drop_index(
        "ix_guard_exposure_delivery_outcomes_status_updated",
        table_name="guard_exposure_delivery_outcomes",
    )
    op.drop_index("ix_messages_created_at", table_name="messages")
    op.drop_column("ai_usage_events", "duration_ms")
    op.drop_column("waha_workers", "resources_observed_at")
    op.drop_column("waha_workers", "resources")
    op.drop_column("waha_event_inbox", "processed_at")
    op.drop_column("waha_event_inbox", "claimed_at")
    op.drop_index("ix_ops_metrics_metric_bucket", table_name="ops_metrics")
    op.drop_table("ops_metrics")
