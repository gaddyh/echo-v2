"""Add isolated Baseline B Guard assessment history.

Revision ID: 0040_guard_simple_assessments
Revises: 0039_guard_analysis_telemetry
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0040_guard_simple_assessments"
down_revision = "0039_guard_analysis_telemetry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guard_simple_assessments",
        sa.Column("id", postgresql.UUID(as_uuid=False), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("child_user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("chat_type", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("window_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("window_end", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("parent_should_know", sa.Boolean(), nullable=True),
        sa.Column("child_role", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence_message_ids", postgresql.JSONB(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("analyzer_version", sa.Text(), nullable=False),
        sa.Column("truncated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("omitted_message_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("identity_verified", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("identity_reason", sa.Text(), nullable=True),
        sa.Column("diagnostics", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("first_detected_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_evidence_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="guard_simple_assessments_pkey"),
        sa.ForeignKeyConstraint(["child_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["connection_id"], ["whatsapp_connections.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "child_role IN ('target', 'participant', 'bystander', 'unknown')",
            name="guard_simple_assessments_child_role_check",
        ),
    )
    op.create_index(
        "ix_guard_simple_assessments_child_chat_created",
        "guard_simple_assessments",
        ["child_user_id", "chat_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_guard_simple_assessments_child_chat_created",
        table_name="guard_simple_assessments",
    )
    op.drop_table("guard_simple_assessments")
