"""Add ai_usage_events table for the AI cost ledger.

Revision ID: 0059_ai_usage_events
Revises: 0058_message_media_size

Stores one row per actual provider call across OpenAI, Gemini and Modal,
with provider-reported usage, a configurable-price-derived estimated cost,
and a cost_source classification (provider_reported / measured_usage_priced
/ fallback_estimate / unknown). The unique ``invocation_id`` is the
idempotency key: persistence retries upsert, while a genuinely separate
billable attempt (provider retry, reanalysis, duplicate analysis) gets a
fresh invocation_id and its own row.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0059_ai_usage_events"
down_revision = "0058_message_media_size"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_usage_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "invocation_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            unique=True,
        ),
        sa.Column("logical_ref", sa.Text(), nullable=True),
        sa.Column(
            "child_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("usage_type", sa.Text(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "usage_details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("cost_source", sa.Text(), nullable=False),
        sa.Column("price_version", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("provider_request_id", sa.Text(), nullable=True),
        sa.Column("pipeline_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("error_type", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "provider IN ('openai', 'google', 'modal')",
            name="ai_usage_events_provider_check",
        ),
        sa.CheckConstraint(
            "usage_type IN ('tokens', 'gpu_seconds', 'audio_seconds', 'image_units')",
            name="ai_usage_events_usage_type_check",
        ),
        sa.CheckConstraint(
            "cost_source IN ('provider_reported', 'measured_usage_priced', "
            "'fallback_estimate', 'unknown')",
            name="ai_usage_events_cost_source_check",
        ),
        sa.CheckConstraint(
            "status IN ('success', 'error')",
            name="ai_usage_events_status_check",
        ),
    )
    op.create_index(
        "ix_ai_usage_events_child_created",
        "ai_usage_events",
        ["child_user_id", "created_at"],
    )
    op.create_index(
        "ix_ai_usage_events_provider_operation_created",
        "ai_usage_events",
        ["provider", "operation", "created_at"],
    )
    op.create_index(
        "ix_ai_usage_events_model_created",
        "ai_usage_events",
        ["model", "created_at"],
    )
    op.create_index(
        "ix_ai_usage_events_created_at",
        "ai_usage_events",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_usage_events_created_at", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_model_created", table_name="ai_usage_events")
    op.drop_index(
        "ix_ai_usage_events_provider_operation_created", table_name="ai_usage_events"
    )
    op.drop_index(
        "ix_ai_usage_events_child_created", table_name="ai_usage_events"
    )
    op.drop_table("ai_usage_events")
