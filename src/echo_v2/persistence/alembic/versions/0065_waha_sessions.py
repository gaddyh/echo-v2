"""Add waha_sessions table for WAHA session assignments (Phase 3).

Revision ID: 0065_waha_sessions
Revises: 0064_waha_inbox_claim_columns

One row per Echo user: the stable WAHA session name (generated once at
initial allocation) pinned to one registered worker. Lifecycle state is
persisted independently of ``whatsapp_connections`` so WAHA ``session.status``
callbacks can arrive before the connection record is created.
``pending_effect`` / ``pending_effect_at`` implement retry-safe side effects:
a transition's effect stays queued until it completes, independently of the
already-committed state. ``last_qr_image`` holds the latest agent-delivered
QR image short-lived (for on-demand re-send on explicit user action); it is
cleared when pairing completes and is never logged.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0065_waha_sessions"
down_revision = "0064_waha_inbox_claim_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "waha_sessions",
        sa.Column("session_name", sa.Text(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("raw_status", sa.Text(), nullable=True),
        sa.Column("last_status_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_seq", sa.BigInteger(), nullable=False),
        sa.Column("pending_effect", sa.Text(), nullable=True),
        sa.Column("pending_effect_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_qr_hash", sa.Text(), nullable=True),
        sa.Column("last_qr_image", sa.Text(), nullable=True),
        sa.Column("qr_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("user_id", name="uq_waha_sessions_user"),
        sa.CheckConstraint(
            "status IN ("
            "'provisioning','connecting','pairing_required','connected',"
            "'degraded','blocked','suspended','unknown'"
            ")",
            name="ck_waha_sessions_status",
        ),
    )
    op.create_index(
        "ix_waha_sessions_worker_id",
        "waha_sessions",
        ["worker_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_waha_sessions_worker_id", table_name="waha_sessions")
    op.drop_table("waha_sessions")
