"""Persist Guard reconnect requests per authentication-loss episode.

Revision ID: 0053_guard_reconnect_requests
Revises: 0053_guardian_name_onboarding
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0053_guard_reconnect_requests"
down_revision = "0053_guardian_name_onboarding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guard_reconnect_requests",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("child_user_id", sa.Uuid(), nullable=False),
        sa.Column("guardian_user_id", sa.Uuid(), nullable=False),
        sa.Column("episode_key", sa.Text(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["child_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["guardian_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["connection_id"], ["whatsapp_connections.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "guardian_user_id", "child_user_id", "episode_key",
            name="guard_reconnect_requests_guardian_child_episode_key",
        ),
        sa.CheckConstraint("status IN ('open', 'closed')", name="guard_reconnect_requests_status_check"),
    )
    op.create_index(
        "ix_guard_reconnect_requests_open_child_episode",
        "guard_reconnect_requests", ["child_user_id", "episode_key"],
        postgresql_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_index("ix_guard_reconnect_requests_open_child_episode", table_name="guard_reconnect_requests")
    op.drop_table("guard_reconnect_requests")
