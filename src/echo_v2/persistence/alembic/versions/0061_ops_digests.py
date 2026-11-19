"""Add ops_digests table for connector ops-log digests.

Revision ID: 0061_ops_digests
Revises: 0060_guard_daily_stats

Stores one row per hourly ops digest POSTed by the
echo-whatsapp-connector's log monitor: the LLM-written digest
(happened / resolved / still_open), the redacted counters that fed it,
and window metadata. ``digest_key`` (``worker_id:window_to``) is the
idempotency key — connector delivery retries must not duplicate rows.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0061_ops_digests"
down_revision = "0060_guard_daily_stats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ops_digests",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("digest_key", sa.Text(), nullable=False, unique=True),
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False, server_default="connector"),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("attention", sa.Boolean(), nullable=False),
        sa.Column("one_liner", sa.Text(), nullable=False),
        sa.Column("digest", postgresql.JSONB(), nullable=False),
        sa.Column("metrics", postgresql.JSONB(), nullable=True),
        sa.Column("window_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_ops_digests_received_at", "ops_digests", ["received_at"])
    op.create_index("ix_ops_digests_worker_id", "ops_digests", ["worker_id"])


def downgrade() -> None:
    op.drop_index("ix_ops_digests_worker_id", table_name="ops_digests")
    op.drop_index("ix_ops_digests_received_at", table_name="ops_digests")
    op.drop_table("ops_digests")
