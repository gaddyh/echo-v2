"""Agent-based WAHA management: command queue + worker protocol columns.

Revision ID: 0066_waha_agent_management
Revises: 0065_waha_sessions

Echo is the control plane; the per-VM agent is the execution layer. Echo
enqueues management commands (``waha_commands``) that the assigned agent
claims by polling, executes against its local WAHA (127.0.0.1:3000), and
reports back. Claims are lease-bounded and attempt-capped; commands survive
Echo restarts and temporary worker unavailability.

``waha_workers`` gains the backward-compatible ``management_protocol``
column (0 = batch-only agent, never eligible for onboarding assignment) and
``management_token_hash`` — the SHA-256 of the per-worker management token
secret issued on the worker's first management-capable registration. The
token binds the new /internal/waha/commands and /internal/waha/events
endpoints to a single worker identity.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0066_waha_agent_management"
down_revision = "0065_waha_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "waha_commands",
        sa.Column(
            "command_id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("session_name", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "action IN ('create_and_start', 'start', 'get_qr', 'get_status')",
            name="ck_waha_commands_action",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'completed', 'failed')",
            name="ck_waha_commands_status",
        ),
    )
    op.create_index(
        "ix_waha_commands_claim",
        "waha_commands",
        ["worker_id", "status", "created_at"],
    )
    op.create_index(
        "ix_waha_commands_session",
        "waha_commands",
        ["session_name", "action", "status"],
    )

    op.add_column(
        "waha_workers",
        sa.Column(
            "management_protocol",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "waha_workers",
        sa.Column("management_token_hash", sa.LargeBinary(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("waha_workers", "management_token_hash")
    op.drop_column("waha_workers", "management_protocol")
    op.drop_index("ix_waha_commands_session", table_name="waha_commands")
    op.drop_index("ix_waha_commands_claim", table_name="waha_commands")
    op.drop_table("waha_commands")
