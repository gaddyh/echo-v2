"""Add guardian_user_id to Guard delivery outcome and state primary keys.

Revision ID: 0056_multi_guardian_outcomes
Revises: 0055_baileys_state_version

Dev-only migration: drops and recreates the five outcome/state tables whose
primary keys must become guardian-aware. Existing delivery data is discarded.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0056_multi_guardian_outcomes"
down_revision = "0055_baileys_state_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- guard_alert_evaluations: PK (analysis_result_id) -> (analysis_result_id, guardian_user_id)
    op.drop_index("ix_guard_alert_evaluations_child_created", table_name="guard_alert_evaluations")
    op.drop_table("guard_alert_evaluations")
    op.create_table(
        "guard_alert_evaluations",
        sa.Column("analysis_result_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("guardian_user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("child_user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("target_version", sa.BigInteger(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("evaluated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("alert_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.PrimaryKeyConstraint(
            "analysis_result_id", "guardian_user_id", name="guard_alert_evaluations_pkey"
        ),
        sa.ForeignKeyConstraint(
            ["analysis_result_id"], ["guard_analysis_results.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["guardian_user_id"], ["users.id"], ondelete="CASCADE"),
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

    # --- guard_parent_attention_delivery: PK (child_user_id, chat_id) -> (guardian_user_id, child_user_id, chat_id)
    op.drop_table("guard_parent_attention_delivery")
    op.create_table(
        "guard_parent_attention_delivery",
        sa.Column("guardian_user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("child_user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("last_delivered_assessment_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("last_delivered_evidence_ids", postgresql.JSONB(), nullable=False),
        sa.Column("last_scheduled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_action_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint(
            "guardian_user_id", "child_user_id", "chat_id", name="guard_parent_attention_delivery_pkey"
        ),
        sa.ForeignKeyConstraint(["guardian_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["child_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["last_delivered_assessment_id"],
            ["guard_simple_assessments.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["last_action_id"], ["scheduled_actions.id"], ondelete="SET NULL"
        ),
    )

    # --- guard_parent_attention_delivery_outcomes: PK (assessment_id) -> (assessment_id, guardian_user_id)
    op.drop_index(
        "ix_guard_parent_attention_delivery_outcomes_child_chat",
        table_name="guard_parent_attention_delivery_outcomes",
    )
    op.drop_index(
        "ix_guard_parent_attention_delivery_outcomes_status_updated",
        table_name="guard_parent_attention_delivery_outcomes",
    )
    op.drop_table("guard_parent_attention_delivery_outcomes")
    op.create_table(
        "guard_parent_attention_delivery_outcomes",
        sa.Column("assessment_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("guardian_user_id", postgresql.UUID(as_uuid=False), nullable=False),
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
        sa.PrimaryKeyConstraint(
            "assessment_id", "guardian_user_id", name="guard_parent_attention_delivery_outcomes_pkey"
        ),
        sa.ForeignKeyConstraint(
            ["assessment_id"], ["guard_simple_assessments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["guardian_user_id"], ["users.id"], ondelete="CASCADE"),
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

    # --- guard_exposure_delivery: PK (child_user_id, chat_id) -> (guardian_user_id, child_user_id, chat_id)
    op.drop_table("guard_exposure_delivery")
    op.create_table(
        "guard_exposure_delivery",
        sa.Column("guardian_user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("child_user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("last_scheduled_assessment_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("last_scheduled_evidence_ids", postgresql.JSONB(), nullable=False),
        sa.Column("last_action_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint(
            "guardian_user_id", "child_user_id", "chat_id", name="guard_exposure_delivery_pkey"
        ),
        sa.ForeignKeyConstraint(["guardian_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["child_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["last_scheduled_assessment_id"],
            ["guard_exposure_assessments.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["last_action_id"], ["scheduled_actions.id"], ondelete="SET NULL"),
    )

    # --- guard_exposure_delivery_outcomes: PK (assessment_id) -> (assessment_id, guardian_user_id)
    op.drop_index(
        "ix_guard_exposure_delivery_outcomes_child_chat",
        table_name="guard_exposure_delivery_outcomes",
    )
    op.drop_table("guard_exposure_delivery_outcomes")
    op.create_table(
        "guard_exposure_delivery_outcomes",
        sa.Column("assessment_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("guardian_user_id", postgresql.UUID(as_uuid=False), nullable=False),
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
        sa.PrimaryKeyConstraint(
            "assessment_id", "guardian_user_id", name="guard_exposure_delivery_outcomes_pkey"
        ),
        sa.ForeignKeyConstraint(["assessment_id"], ["guard_exposure_assessments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["guardian_user_id"], ["users.id"], ondelete="CASCADE"),
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
    # Reverse: drop guardian_user_id from PKs and restore old schemas.
    op.drop_index(
        "ix_guard_exposure_delivery_outcomes_child_chat",
        table_name="guard_exposure_delivery_outcomes",
    )
    op.drop_table("guard_exposure_delivery_outcomes")
    op.drop_table("guard_exposure_delivery")
    op.drop_index(
        "ix_guard_parent_attention_delivery_outcomes_child_chat",
        table_name="guard_parent_attention_delivery_outcomes",
    )
    op.drop_index(
        "ix_guard_parent_attention_delivery_outcomes_status_updated",
        table_name="guard_parent_attention_delivery_outcomes",
    )
    op.drop_table("guard_parent_attention_delivery_outcomes")
    op.drop_table("guard_parent_attention_delivery")
    op.drop_index("ix_guard_alert_evaluations_child_created", table_name="guard_alert_evaluations")
    op.drop_table("guard_alert_evaluations")
