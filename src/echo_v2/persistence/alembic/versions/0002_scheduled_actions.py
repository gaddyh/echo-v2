"""Scheduled actions table for the Step 1 scheduler.

Revision ID: 0002_scheduled_actions
Revises: 0001_foundation_db
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_scheduled_actions"
down_revision: str | None = "0001_foundation_db"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduled_actions",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("execute_at_utc", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("executed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("claimed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name=op.f("scheduled_actions_user_id_fkey")),
        sa.PrimaryKeyConstraint("id", name=op.f("scheduled_actions_pkey")),
        sa.CheckConstraint(
            "status IN ('pending','in_progress','succeeded','failed',"
            "'cancelled','indeterminate')",
            name="scheduled_actions_status_check",
        ),
        sa.CheckConstraint(
            "type IN ('send_whatsapp_message','send_reminder')",
            name="scheduled_actions_type_check",
        ),
    )
    op.create_index(
        "ix_scheduled_actions_status_execute_at",
        "scheduled_actions",
        ["status", "execute_at_utc"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_scheduled_actions_status_execute_at", table_name="scheduled_actions")
    op.drop_table("scheduled_actions")
