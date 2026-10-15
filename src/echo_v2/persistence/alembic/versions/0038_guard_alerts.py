"""Add Guard alert events, episode state, and policy evaluation ledger.

Revision ID: 0038_guard_alerts
Revises: 0037_waitlist_preference
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0038_guard_alerts"
down_revision = "0037_waitlist_preference"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guard_threshold_state",
        sa.Column("child_user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("threshold_family", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="inactive", nullable=False),
        sa.Column("episode_start_version", sa.BigInteger(), nullable=True),
        sa.Column("current_kind", sa.Text(), nullable=True),
        sa.Column("last_processed_version", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("last_supporting_version", sa.BigInteger(), nullable=True),
        sa.Column("last_supporting_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("non_actionable_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("non_actionable_since", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint(
            "child_user_id", "chat_id", "threshold_family", name="guard_threshold_state_pkey"
        ),
        sa.ForeignKeyConstraint(["child_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')", name="guard_threshold_state_status_check"
        ),
        sa.CheckConstraint(
            "current_kind IS NULL OR current_kind IN ('notify', 'urgent')",
            name="guard_threshold_state_kind_check",
        ),
    )

    op.create_table(
        "guard_alerts",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("analysis_result_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("guardian_user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("child_user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("categories", postgresql.JSONB(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("policy_reason", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("scheduled_action_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("logical_event_key", sa.Text(), nullable=False),
        sa.Column("threshold_family", sa.Text(), nullable=False),
        sa.Column("episode_start_version", sa.BigInteger(), nullable=False),
        sa.Column("detected_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="guard_alerts_pkey"),
        sa.ForeignKeyConstraint(["analysis_result_id"], ["guard_analysis_results.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["guardian_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["child_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["scheduled_action_id"], ["scheduled_actions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("logical_event_key", name="guard_alerts_event_key"),
        sa.CheckConstraint("kind IN ('notify', 'urgent')", name="guard_alerts_kind_check"),
        sa.CheckConstraint(
            "status IN ('scheduled', 'suppressed', 'expired')",
            name="guard_alerts_status_check",
        ),
    )
    op.create_index(
        "ix_guard_alerts_child_chat_created",
        "guard_alerts",
        ["child_user_id", "chat_id", "created_at"],
    )

    op.create_table(
        "guard_alert_evaluations",
        sa.Column("analysis_result_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("child_user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("target_version", sa.BigInteger(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("evaluated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("alert_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.PrimaryKeyConstraint("analysis_result_id", name="guard_alert_evaluations_pkey"),
        sa.ForeignKeyConstraint(
            ["analysis_result_id"], ["guard_analysis_results.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["child_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["alert_id"], ["guard_alerts.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "outcome IN ('scheduled', 'no_action', 'suppressed')",
            name="guard_alert_evaluations_outcome_check",
        ),
    )
    op.create_index(
        "ix_guard_alert_evaluations_child_created",
        "guard_alert_evaluations",
        ["child_user_id", "evaluated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_guard_alert_evaluations_child_created", table_name="guard_alert_evaluations")
    op.drop_table("guard_alert_evaluations")
    op.drop_index("ix_guard_alerts_child_chat_created", table_name="guard_alerts")
    op.drop_table("guard_alerts")
    op.drop_table("guard_threshold_state")
