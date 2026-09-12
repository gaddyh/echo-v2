"""Waiting-list web sessions: opaque token hash for the mini web app.

Revision ID: 0014_waiting_list_sessions
Revises: 0013_feedback_hardening
Create Date: 2026-09-13

Stores SHA-256 hashes of one-time URL tokens used to authenticate the
waiting-list mini web app. The raw token is never persisted — only its
hash. A token is exchanged for a Secure HttpOnly cookie on first page
open, so the raw token never appears in API paths or logs.

Columns:
* ``token_hash`` — SHA-256 of the raw ``secrets.token_urlsafe(32)`` value.
* ``user_id`` — the user this session belongs to.
* ``expires_at`` — when the session expires (default 48h).
* ``opened_at`` — first page open (set once, idempotent).
* ``last_action_at`` — last action timestamp (touched on each action).
* ``revoked_at`` — when the session was revoked (NULL = active).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0014_waiting_list_sessions"
down_revision = "0013_feedback_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "waiting_list_sessions",
        sa.Column("id", UUID(as_uuid=False), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("token_hash", sa.LargeBinary, nullable=False),
        sa.Column(
            "user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("opened_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_action_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_wls_token_hash"),
    )
    op.create_index("ix_wls_user_id", "waiting_list_sessions", ["user_id"])
    op.create_index("ix_wls_expires_at", "waiting_list_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_wls_expires_at", table_name="waiting_list_sessions")
    op.drop_index("ix_wls_user_id", table_name="waiting_list_sessions")
    op.drop_table("waiting_list_sessions")
