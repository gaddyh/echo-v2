"""Add claim/lease columns to waha_event_inbox for consumer processing.

Revision ID: 0064_waha_inbox_claim_columns
Revises: 0063_waha_workers

Phase 1 persisted rows as ``pending`` with no consumer. Phase 2 adds a
consumer: rows are claimed (``processing`` + lease), completed
(revision-guarded ``processed``), or failed (retryable up to
``attempt_count``). The partial pending index now also covers failed and
expired leases so the claim poll finds every retryable row.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0064_waha_inbox_claim_columns"
down_revision = "0063_waha_workers"
branch_labels = None
depends_on = None

# Both predicates must be immutable — Postgres rejects now() in index
# predicates. Stale 'processing' leases are found by the claim query's
# WHERE clause; the lease index keeps that scan cheap.


def upgrade() -> None:
    op.add_column(
        "waha_event_inbox",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("waha_event_inbox", sa.Column("lease_owner", sa.Text(), nullable=True))
    op.add_column(
        "waha_event_inbox",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("waha_event_inbox", sa.Column("last_error", sa.Text(), nullable=True))
    op.drop_index("ix_waha_event_inbox_pending", table_name="waha_event_inbox")
    op.create_index(
        "ix_waha_event_inbox_claimable",
        "waha_event_inbox",
        ["first_received_at"],
        postgresql_where=sa.text("status IN ('pending', 'failed')"),
    )
    op.create_index(
        "ix_waha_event_inbox_leases",
        "waha_event_inbox",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'processing'"),
    )


def downgrade() -> None:
    op.drop_index("ix_waha_event_inbox_leases", table_name="waha_event_inbox")
    op.drop_index("ix_waha_event_inbox_claimable", table_name="waha_event_inbox")
    op.create_index(
        "ix_waha_event_inbox_pending",
        "waha_event_inbox",
        ["first_received_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_column("waha_event_inbox", "last_error")
    op.drop_column("waha_event_inbox", "lease_expires_at")
    op.drop_column("waha_event_inbox", "lease_owner")
    op.drop_column("waha_event_inbox", "attempt_count")
