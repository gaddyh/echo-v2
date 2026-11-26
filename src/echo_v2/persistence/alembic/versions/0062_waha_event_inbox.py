"""Add waha_event_inbox table for WAHA batch-agent message ingestion.

Revision ID: 0062_waha_event_inbox
Revises: 0061_ops_digests

Stores one row per ``(session, message_id)`` durably POSTed by a WAHA GOWS
batch agent. ``data`` is the exact raw Whatsmeow JSON string (never
re-serialized); ``sha256`` is the agent's digest of its UTF-8 bytes. A
payload change (different ``sha256``) bumps ``revision`` and resets
``status`` to ``pending`` so downstream processing is revision-aware.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0062_waha_event_inbox"
down_revision = "0061_ops_digests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "waha_event_inbox",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("session", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("chat_jid", sa.Text(), nullable=False),
        sa.Column("whatsapp_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("from_me", sa.Integer(), nullable=False),
        sa.Column("is_real", sa.Integer(), nullable=False),
        sa.Column("data", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("agent_first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "first_received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.UniqueConstraint("session", "message_id", name="uq_waha_event_inbox_session_message"),
        sa.CheckConstraint("from_me IN (0, 1)", name="ck_waha_event_inbox_from_me"),
        sa.CheckConstraint("is_real IN (0, 1)", name="ck_waha_event_inbox_is_real"),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'processed', 'failed')",
            name="ck_waha_event_inbox_status",
        ),
    )
    op.create_index("ix_waha_event_inbox_batch_id", "waha_event_inbox", ["batch_id"])
    op.create_index(
        "ix_waha_event_inbox_pending",
        "waha_event_inbox",
        ["first_received_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_waha_event_inbox_pending", table_name="waha_event_inbox")
    op.drop_index("ix_waha_event_inbox_batch_id", table_name="waha_event_inbox")
    op.drop_table("waha_event_inbox")
