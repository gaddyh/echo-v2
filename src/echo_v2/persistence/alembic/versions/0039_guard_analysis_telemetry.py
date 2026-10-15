"""Add Guard analyzer attempt telemetry.

Revision ID: 0039_guard_analysis_telemetry
Revises: 0038_guard_alerts
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0039_guard_analysis_telemetry"
down_revision = "0038_guard_alerts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guard_analysis_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("child_user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("target_version", sa.BigInteger(), nullable=False),
        sa.Column("scheduled_for", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("pending_since", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("result_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("analyzer_version", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="guard_analysis_attempts_pkey"),
        sa.ForeignKeyConstraint(["child_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["result_id"], ["guard_analysis_results.id"], ondelete="SET NULL"
        ),
        sa.CheckConstraint(
            "status IN ('started', 'committed', 'stale', 'failed')",
            name="guard_analysis_attempts_status_check",
        ),
    )
    op.create_index(
        "ix_guard_analysis_attempts_child_chat_started",
        "guard_analysis_attempts",
        ["child_user_id", "chat_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_guard_analysis_attempts_child_chat_started",
        table_name="guard_analysis_attempts",
    )
    op.drop_table("guard_analysis_attempts")
