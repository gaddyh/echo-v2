"""Daily digests table.

Revision ID: 0008_daily_digests
Revises: 0007_waiting_for_me_active
Create Date: 2026-09-12

One row per user per local_date. The UNIQUE(user_id, local_date)
constraint provides dedup — if the worker crashes after sending but
before updating status, the row already exists and won't be re-sent.

Statuses: processing, sent, empty, indeterminate, failed.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0008_daily_digests"
down_revision = "0007_waiting_for_me_active"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_digests",
        sa.Column("id", UUID(as_uuid=False), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("local_date", sa.Date, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="processing"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("provider_message_id", sa.Text, nullable=True),
        sa.Column("item_count", sa.Integer, nullable=False, server_default="0"),
        sa.UniqueConstraint("user_id", "local_date", name="daily_digests_user_date_key"),
        sa.CheckConstraint(
            "status IN ('processing', 'sent', 'empty', 'indeterminate', 'failed')",
            name="daily_digests_status_check",
        ),
    )
    op.create_index("ix_daily_digests_user_date", "daily_digests", ["user_id", "local_date"])


def downgrade() -> None:
    op.drop_index("ix_daily_digests_user_date", table_name="daily_digests")
    op.drop_table("daily_digests")
