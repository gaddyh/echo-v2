"""Add Guard Activity provenance and per-assessment delivery outcomes.

Revision ID: 0042_guard_activity_telemetry
Revises: 0041_parent_attention
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0042_guard_activity_telemetry"
down_revision = "0041_parent_attention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "guard_simple_assessments",
        sa.Column(
            "input_message_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "guard_simple_assessments",
        sa.Column("input_content_hash", sa.Text(), nullable=True),
    )
    op.alter_column("guard_parent_attention_delivery", "last_delivered_at", new_column_name="last_scheduled_at")
    op.create_table(
        "guard_parent_attention_delivery_outcomes",
        sa.Column(
            "assessment_id",
            postgresql.UUID(as_uuid=False),
            nullable=False,
        ),
        sa.Column("child_user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("scheduled_action_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("scheduled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("failed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("assessment_id", name="guard_parent_attention_delivery_outcomes_pkey"),
        sa.ForeignKeyConstraint(
            ["assessment_id"], ["guard_simple_assessments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["child_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["scheduled_action_id"], ["scheduled_actions.id"], ondelete="SET NULL"
        ),
        sa.CheckConstraint(
            "status IN ('not_sent', 'scheduled', 'sent', 'failed', 'indeterminate')",
            name="guard_parent_attention_delivery_outcome_status_check",
        ),
    )
    op.create_index(
        "ix_guard_parent_attention_delivery_outcomes_status_updated",
        "guard_parent_attention_delivery_outcomes",
        ["status", "updated_at"],
    )
    op.create_index(
        "ix_guard_parent_attention_delivery_outcomes_child_chat",
        "guard_parent_attention_delivery_outcomes",
        ["child_user_id", "chat_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_guard_parent_attention_delivery_outcomes_child_chat",
        table_name="guard_parent_attention_delivery_outcomes",
    )
    op.drop_index(
        "ix_guard_parent_attention_delivery_outcomes_status_updated",
        table_name="guard_parent_attention_delivery_outcomes",
    )
    op.drop_table("guard_parent_attention_delivery_outcomes")
    op.alter_column("guard_parent_attention_delivery", "last_scheduled_at", new_column_name="last_delivered_at")
    op.drop_column("guard_simple_assessments", "input_content_hash")
    op.drop_column("guard_simple_assessments", "input_message_ids")
