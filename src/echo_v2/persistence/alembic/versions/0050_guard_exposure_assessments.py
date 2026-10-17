"""Add group-level Exposure Guard shadow assessments.

Revision ID: 0050_guard_exposure_assessments
Revises: 0049_parent_child_onboarding
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0050_guard_exposure_assessments"
down_revision = "0049_parent_child_onboarding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guard_exposure_assessments",
        sa.Column("id", postgresql.UUID(as_uuid=False), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("child_user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("chat_type", sa.Text(), nullable=False),
        sa.Column("chat_name", sa.Text(), server_default="", nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("window_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("window_end", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("target_version", sa.Integer(), nullable=False),
        sa.Column("significant_exposure", sa.Boolean(), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence_message_ids", postgresql.JSONB(), nullable=False),
        sa.Column("input_message_ids", postgresql.JSONB(), nullable=False),
        sa.Column("input_content_hash", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("analyzer_version", sa.Text(), nullable=False),
        sa.Column("truncated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("omitted_message_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("diagnostics", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="guard_exposure_assessments_pkey"),
        sa.ForeignKeyConstraint(["child_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["connection_id"], ["whatsapp_connections.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "category IS NULL OR category IN ('violence', 'bullying', 'sexual_harm', 'coercion')",
            name="guard_exposure_assessments_category_check",
        ),
    )
    op.create_index(
        "ix_guard_exposure_assessments_child_chat_created",
        "guard_exposure_assessments",
        ["child_user_id", "chat_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_guard_exposure_assessments_child_chat_created",
        table_name="guard_exposure_assessments",
    )
    op.drop_table("guard_exposure_assessments")
