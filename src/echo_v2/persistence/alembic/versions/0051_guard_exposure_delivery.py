"""Add isolated Exposure notification delivery state and outcomes.

Revision ID: 0051_guard_exposure_delivery
Revises: 0050_guard_exposure_assessments
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0051_guard_exposure_delivery"
down_revision = "0050_guard_exposure_assessments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guard_exposure_delivery",
        sa.Column("child_user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("last_scheduled_assessment_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("last_scheduled_evidence_ids", postgresql.JSONB(), nullable=False),
        sa.Column("last_action_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("child_user_id", "chat_id", name="guard_exposure_delivery_pkey"),
        sa.ForeignKeyConstraint(["child_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["last_scheduled_assessment_id"],
            ["guard_exposure_assessments.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["last_action_id"], ["scheduled_actions.id"], ondelete="SET NULL"),
    )
    op.create_table(
        "guard_exposure_delivery_outcomes",
        sa.Column("assessment_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("child_user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("evidence_message_ids", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("scheduled_action_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("scheduled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("failed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("assessment_id", name="guard_exposure_delivery_outcomes_pkey"),
        sa.ForeignKeyConstraint(["assessment_id"], ["guard_exposure_assessments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["child_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["scheduled_action_id"], ["scheduled_actions.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "status IN ('not_sent', 'scheduled', 'sent', 'failed', 'indeterminate')",
            name="guard_exposure_delivery_outcome_status_check",
        ),
    )
    op.create_index(
        "ix_guard_exposure_delivery_outcomes_child_chat",
        "guard_exposure_delivery_outcomes",
        ["child_user_id", "chat_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_guard_exposure_delivery_outcomes_child_chat",
        table_name="guard_exposure_delivery_outcomes",
    )
    op.drop_table("guard_exposure_delivery_outcomes")
    op.drop_table("guard_exposure_delivery")
