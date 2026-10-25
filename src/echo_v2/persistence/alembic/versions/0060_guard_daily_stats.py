"""Add guard_daily_stats table for daily reporting aggregation.

Revision ID: 0060_guard_daily_stats
Revises: 0059_ai_usage_events

Stores one row per (child_user_id, local_date) with aggregated message counts,
media counts, detection/alert counts, coverage health, and derived coverage
status. Idempotent upsert — rebuilding a day yields the same row. Source data
comes from existing tables (messages, message_enrichments, guard_simple/exposure
_assessments, delivery outcomes, pipeline_events, whatsapp_connections).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0060_guard_daily_stats"
down_revision = "0059_ai_usage_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guard_daily_stats",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "child_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        # Message counts
        sa.Column("messages_received", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("private_messages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("group_messages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_private_chats", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_group_chats", sa.Integer(), nullable=False, server_default="0"),
        # Media received counts
        sa.Column("images_received", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("videos_received", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("audio_received", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("links_received", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("documents_received", sa.Integer(), nullable=False, server_default="0"),
        # Media enriched counts
        sa.Column("images_enriched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("videos_enriched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("audio_enriched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("links_enriched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("documents_enriched", sa.Integer(), nullable=False, server_default="0"),
        # Assessment + detection counts
        sa.Column("guard_assessments", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("security_detections", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("exposure_detections", sa.Integer(), nullable=False, server_default="0"),
        # Alert delivery counts
        sa.Column("security_alerts_sent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("exposure_alerts_sent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("alerts_failed", sa.Integer(), nullable=False, server_default="0"),
        # Failure counts
        sa.Column("analysis_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("media_enrichment_failures", sa.Integer(), nullable=False, server_default="0"),
        # Coverage
        sa.Column("coverage_health", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("security_attention", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("coverage_status", sa.Text(), nullable=False, server_default="protected"),
        sa.Column("connection_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "child_user_id",
            "local_date",
            name="guard_daily_stats_child_date_key",
        ),
        sa.CheckConstraint(
            "coverage_status IN ('protected', 'attention', 'incomplete')",
            name="guard_daily_stats_coverage_status_check",
        ),
    )
    op.create_index(
        "ix_guard_daily_stats_local_date",
        "guard_daily_stats",
        ["local_date"],
    )
    op.create_index(
        "ix_guard_daily_stats_coverage_status",
        "guard_daily_stats",
        ["coverage_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_guard_daily_stats_coverage_status", table_name="guard_daily_stats")
    op.drop_index("ix_guard_daily_stats_local_date", table_name="guard_daily_stats")
    op.drop_table("guard_daily_stats")
