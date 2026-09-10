"""WaitingForMe active state table.

Revision ID: 0007_waiting_for_me_active
Revises: 0006_waiting_for_me_results
Create Date: 2026-09-12

One row per chat representing the current active waiting state.
``target_version`` links to the ``activity_version`` the state was
computed from. When a new message arrives, ``activity_version``
increments and the row becomes stale until re-analyzed.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0007_waiting_for_me_active"
down_revision = "0006_waiting_for_me_results"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "waiting_for_me_active",
        sa.Column("user_id", UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("chat_id", sa.Text, primary_key=True),
        sa.Column("target_version", sa.Integer, nullable=False),
        sa.Column("result_id", UUID(as_uuid=False), sa.ForeignKey("waiting_for_me_results.id", ondelete="CASCADE"), nullable=False),
        sa.Column("waiting_since", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("notified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("waiting_for_me_active")
