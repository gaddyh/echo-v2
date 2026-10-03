"""Add Echo-owned Baileys connector event processing ledger."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030_baileys_event_processing"
down_revision = "0029_message_media_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "baileys_connector_event_processing",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("inbox_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="claimed"),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("notification_state", sa.Text(), nullable=False, server_default="none"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("inbox_id"),
        sa.CheckConstraint("status IN ('claimed','processed','failed')", name="baileys_event_processing_status_check"),
        sa.CheckConstraint("notification_state IN ('none','pending','sent')", name="baileys_event_processing_notification_check"),
    )
    op.create_index(
        "ix_baileys_event_processing_claim",
        "baileys_connector_event_processing",
        ["status", "claimed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_baileys_event_processing_claim", table_name="baileys_connector_event_processing")
    op.drop_table("baileys_connector_event_processing")
