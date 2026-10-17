"""Add structured pipeline event log.

Revision ID: 0047_pipeline_events
Revises: 0046_message_enrichments
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0047_pipeline_events"
down_revision = "0046_message_enrichments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pipeline_events",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("event_name", sa.Text(), nullable=False),
        sa.Column("pipeline", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("event_id", sa.Text(), nullable=True),
        sa.Column("user_id_hash", sa.Text(), nullable=True),
        sa.Column("chat_id_hash", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("error_type", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_pipeline_events_created_at", "pipeline_events", ["created_at"])
    op.create_index("ix_pipeline_events_message_id", "pipeline_events", ["message_id"])
    op.create_index("ix_pipeline_events_pipeline_status", "pipeline_events", ["pipeline", "status"])


def downgrade() -> None:
    op.drop_index("ix_pipeline_events_pipeline_status", table_name="pipeline_events")
    op.drop_index("ix_pipeline_events_message_id", table_name="pipeline_events")
    op.drop_index("ix_pipeline_events_created_at", table_name="pipeline_events")
    op.drop_table("pipeline_events")
