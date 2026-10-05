"""Add Guard review scheduling snapshots and operator feedback.

Revision ID: 0034_guard_review
Revises: 0033_guard_shadow
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0034_guard_review"
down_revision = "0033_guard_shadow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("guard_chat_state", sa.Column("next_analysis_reason", sa.Text(), nullable=True))
    op.add_column("guard_analysis_results", sa.Column("schedule_reason", sa.Text(), nullable=True))
    op.add_column("guard_analysis_results", sa.Column("pending_since", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("guard_analysis_results", sa.Column("scheduled_for", sa.TIMESTAMP(timezone=True), nullable=True))
    op.create_table(
        "guard_analysis_feedback",
        sa.Column("result_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("reviewer_user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("result_id"),
        sa.ForeignKeyConstraint(["result_id"], ["guard_analysis_results.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewer_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "label IN ('correct', 'false_positive', 'should_be_stronger', 'irrelevant')",
            name="guard_feedback_label_check",
        ),
    )
    op.create_table(
        "guard_analysis_feedback_events",
        sa.Column("id", postgresql.UUID(as_uuid=False), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("result_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("reviewer_user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("old_label", sa.Text(), nullable=True),
        sa.Column("new_label", sa.Text(), nullable=False),
        sa.Column("old_note", sa.Text(), nullable=True),
        sa.Column("new_note", sa.Text(), nullable=True),
        sa.Column("changed_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["result_id"], ["guard_analysis_results.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewer_user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_guard_feedback_events_result_changed",
        "guard_analysis_feedback_events",
        ["result_id", "changed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_guard_feedback_events_result_changed", table_name="guard_analysis_feedback_events")
    op.drop_table("guard_analysis_feedback_events")
    op.drop_table("guard_analysis_feedback")
    op.drop_column("guard_analysis_results", "scheduled_for")
    op.drop_column("guard_analysis_results", "pending_since")
    op.drop_column("guard_analysis_results", "schedule_reason")
    op.drop_column("guard_chat_state", "next_analysis_reason")
