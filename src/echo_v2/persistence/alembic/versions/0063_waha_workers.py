"""Add waha_workers table for WAHA batch-agent registration/heartbeats.

Revision ID: 0063_waha_workers
Revises: 0062_waha_event_inbox

One row per batch agent, upserted idempotently by ``worker_id``.
``last_seen_at`` is Echo's server-side receive time (the authoritative
heartbeat); ``first_registered_at`` is set once on insert and never updated.
Availability is derived at read time (fresh ``last_seen_at`` +
``health = 'ok'``) — there is no background sweeper.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0063_waha_workers"
down_revision = "0062_waha_event_inbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "waha_workers",
        sa.Column("worker_id", sa.Text(), primary_key=True),
        sa.Column("region", sa.Text(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("max_sessions", sa.Integer(), nullable=False),
        sa.Column("active_sessions", sa.Integer(), nullable=False),
        sa.Column("agent_version", sa.Text(), nullable=False),
        sa.Column("health", sa.Text(), nullable=False),
        sa.Column("reported_heartbeat", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "first_registered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("pending", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "health IN ('starting', 'ok', 'degraded')",
            name="ck_waha_workers_health",
        ),
    )


def downgrade() -> None:
    op.drop_table("waha_workers")
