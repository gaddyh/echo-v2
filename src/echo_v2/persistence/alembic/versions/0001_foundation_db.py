"""Foundation DB: users, whatsapp_connections, provider_webhook_events, idempotency_operations.

Revision ID: 0001_foundation_db
Revises:
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_foundation_db"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- users -------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=False), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("phone_number", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=True),
        sa.Column("locale", sa.Text(), nullable=True),
        sa.Column("account_status", sa.Text(), server_default="active", nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("users_pkey")),
        sa.UniqueConstraint("phone_number", name=op.f("users_phone_number_key")),
        sa.CheckConstraint(
            "account_status IN ('active', 'suspended', 'deleted')",
            name="users_account_status_check",
        ),
    )

    # --- whatsapp_connections ---------------------------------------------
    op.create_table(
        "whatsapp_connections",
        sa.Column("id", postgresql.UUID(as_uuid=False), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_connection_id", sa.Text(), nullable=False),
        sa.Column("credentials", sa.LargeBinary(), nullable=False),
        sa.Column("webhook_token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("connection_status", sa.Text(), nullable=False),
        sa.Column("provider_raw_status", sa.Text(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name=op.f("whatsapp_connections_user_id_fkey")),
        sa.PrimaryKeyConstraint("id", name=op.f("whatsapp_connections_pkey")),
        sa.UniqueConstraint("user_id", "provider", name="whatsapp_connections_user_provider_key"),
        sa.UniqueConstraint("provider", "provider_connection_id", name="whatsapp_connections_provider_id_key"),
        sa.CheckConstraint(
            "connection_status IN ("
            "'provisioning','connecting','pairing_required','connected',"
            "'degraded','blocked','suspended','unknown'"
            ")",
            name="whatsapp_connections_status_check",
        ),
    )

    # --- provider_webhook_events ------------------------------------------
    op.create_table(
        "provider_webhook_events",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=True),
        sa.Column("received_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["whatsapp_connections.id"], ondelete="CASCADE", name=op.f("provider_webhook_events_connection_id_fkey")),
        sa.PrimaryKeyConstraint("event_id", name=op.f("provider_webhook_events_pkey")),
    )

    # --- idempotency_operations -------------------------------------------
    op.create_table(
        "idempotency_operations",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("owner_token", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("lease_expires_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("outcome", postgresql.JSONB(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("error_type", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("idempotency_operations_pkey")),
        sa.CheckConstraint(
            "state IN ('IN_PROGRESS', 'SUCCESS', 'FAILURE', 'INDETERMINATE')",
            name="idempotency_operations_state_check",
        ),
    )
    op.create_index(
        "ix_idempotency_operations_state_lease",
        "idempotency_operations",
        ["state", "lease_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_idempotency_operations_state_lease", table_name="idempotency_operations")
    op.drop_table("idempotency_operations")
    op.drop_table("provider_webhook_events")
    op.drop_table("whatsapp_connections")
    op.drop_table("users")
