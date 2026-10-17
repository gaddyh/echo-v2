"""Add versioned message enrichment state.

Revision ID: 0046_message_enrichments
Revises: 0045_guard_status_nullable
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0046_message_enrichments"
down_revision = "0045_guard_status_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "message_enrichments",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("enrichment_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("enriched_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", "enrichment_version", name="message_enrichments_message_version_key"),
        sa.CheckConstraint(
            "status IN ('pending','processing','completed','failed','terminal')",
            name="message_enrichments_status_check",
        ),
    )
    op.create_index("ix_message_enrichments_retry", "message_enrichments", ["status", "next_retry_at"])
    op.create_index("ix_message_enrichments_lease", "message_enrichments", ["status", "lease_expires_at"])


def downgrade() -> None:
    op.drop_index("ix_message_enrichments_lease", table_name="message_enrichments")
    op.drop_index("ix_message_enrichments_retry", table_name="message_enrichments")
    op.drop_table("message_enrichments")
