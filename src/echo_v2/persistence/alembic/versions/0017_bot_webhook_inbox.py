"""Add bot_webhook_events table — persistent inbox for 360dialog webhooks.

Revision ID: 0017_bot_webhook_inbox
Revises: 0016_wfm_results_unique
Create Date: 2026-09-12

The 360dialog (Echo Business Bot) webhook previously deduplicated in memory:
``claim`` inserted the event_id into a set before processing. If processing
crashed mid-way, the in-memory claim was lost on restart, but a provider
retry would still see "duplicate" (if the process survived) or re-process
from scratch (if it restarted) — neither is safe.

This table is a small persistent inbox with a ``status`` column:
``processing`` → ``processed`` (success) or ``failed`` (exception). A
``failed`` event can be re-claimed on the next provider retry. A stale
``processing`` entry past the lease timeout is also reclaimable.
"""

import sqlalchemy as sa
from alembic import op

revision = "0017_bot_webhook_inbox"
down_revision = "0016_wfm_results_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bot_webhook_events",
        sa.Column("event_id", sa.Text, primary_key=True),
        sa.Column(
            "status",
            sa.Text,
            nullable=False,
            server_default="processing",
        ),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("error", sa.Text, nullable=True),
        sa.CheckConstraint(
            "status IN ('processing', 'processed', 'failed')",
            name="ck_bot_webhook_events_status",
        ),
    )
    op.create_index(
        "ix_bot_webhook_events_status",
        "bot_webhook_events",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index("ix_bot_webhook_events_status", table_name="bot_webhook_events")
    op.drop_table("bot_webhook_events")
