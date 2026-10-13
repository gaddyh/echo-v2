"""Add isolated Guard Live Compare runs, snapshots, and judgments.

Revision ID: 0036_guard_eval
Revises: 0035_guard_review_chat_name
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0036_guard_eval"
down_revision = "0035_guard_review_chat_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guard_eval_runs",
        sa.Column("id", postgresql.UUID(as_uuid=False), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("scenario_id", sa.Text(), nullable=False),
        sa.Column("requester_key", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("gold_version", sa.Text(), nullable=False),
        sa.Column("gold_label", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("analyzer_version", sa.Text(), nullable=False),
        sa.Column("taxonomy_version", sa.Text(), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="guard_eval_runs_pkey"),
        sa.UniqueConstraint("requester_key", "request_id", name="guard_eval_runs_request_key"),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed')",
            name="guard_eval_runs_status_check",
        ),
    )
    op.create_index("ix_guard_eval_runs_created_at", "guard_eval_runs", ["created_at"])
    op.create_index(
        "ix_guard_eval_runs_requester_created",
        "guard_eval_runs",
        ["requester_key", "created_at"],
    )
    op.create_index(
        "ix_guard_eval_runs_status_created",
        "guard_eval_runs",
        ["status", "created_at"],
    )

    op.create_table(
        "guard_eval_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=False), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("scenario_id", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("after_message_id", sa.Text(), nullable=False),
        sa.Column("expected", postgresql.JSONB(), nullable=False),
        sa.Column("actual", postgresql.JSONB(), nullable=False),
        sa.Column("diff", postgresql.JSONB(), nullable=False),
        sa.Column("has_difference", sa.Boolean(), nullable=False),
        sa.Column("failure", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="guard_eval_snapshots_pkey"),
        sa.ForeignKeyConstraint(["run_id"], ["guard_eval_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "ordinal", name="guard_eval_snapshots_run_ordinal_key"),
    )
    op.create_index(
        "ix_guard_eval_snapshots_run_ordinal",
        "guard_eval_snapshots",
        ["run_id", "ordinal"],
    )

    op.create_table(
        "guard_eval_judgments",
        sa.Column("id", postgresql.UUID(as_uuid=False), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("reviewer_key", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="guard_eval_judgments_pkey"),
        sa.ForeignKeyConstraint(["snapshot_id"], ["guard_eval_snapshots.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "snapshot_id", "reviewer_key", name="guard_eval_judgments_snapshot_reviewer_key"
        ),
        sa.CheckConstraint(
            "label IN ('accepted_difference', 'real_failure', 'gold_needs_correction', 'undecided')",
            name="guard_eval_judgments_label_check",
        ),
    )
    op.create_index(
        "ix_guard_eval_judgments_snapshot",
        "guard_eval_judgments",
        ["snapshot_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_guard_eval_judgments_snapshot", table_name="guard_eval_judgments")
    op.drop_table("guard_eval_judgments")
    op.drop_index("ix_guard_eval_snapshots_run_ordinal", table_name="guard_eval_snapshots")
    op.drop_table("guard_eval_snapshots")
    op.drop_index("ix_guard_eval_runs_status_created", table_name="guard_eval_runs")
    op.drop_index("ix_guard_eval_runs_requester_created", table_name="guard_eval_runs")
    op.drop_index("ix_guard_eval_runs_created_at", table_name="guard_eval_runs")
    op.drop_table("guard_eval_runs")
