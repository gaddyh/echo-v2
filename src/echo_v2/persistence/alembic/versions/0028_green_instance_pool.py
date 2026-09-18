"""Add green_instance_pool table.

Revision ID: 0028_green_instance_pool
Revises: 0027_result_next_owner
Create Date: 2026-09-18

A pre-created Green API instance pool for fast onboarding. Lifecycle:
``creating`` → ``available`` → ``claimed`` → (deleted by ``finalize_claim``).
``failed`` is terminal (cleaned on next ``ensure_capacity``, with a
best-effort Green ``deleteInstance`` if it carries a ``provider_connection_id``).

Capacity reservation: ``creating`` + ``available`` count toward the target
size; ``claimed`` does not (it's out of the reserve pool).

Crash safety:
* ``provider_connection_id`` / ``api_token`` / ``webhook_token_hash`` are
  NULL while ``creating`` (before ``createInstance`` returns). They are
  persisted immediately after ``createInstance`` (via ``mark_created``)
  — *before* the ready poll — so a crash during waiting leaves a
  recoverable row with the Green instance id.
* ``claimed`` always carries ``claimed_by_user_id`` + ``claimed_at``.
* ``available`` / ``claimed`` always carry credentials (enforced by CHECK
  constraints).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0028_green_instance_pool"
down_revision = "0027_result_next_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "green_instance_pool",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("provider_connection_id", sa.Text(), nullable=True),
        sa.Column("api_token", sa.LargeBinary(), nullable=True),
        sa.Column("webhook_token_hash", sa.LargeBinary(), nullable=True),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default="creating",
        ),
        sa.Column("claimed_by_user_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column(
            "claimed_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("green_instance_pool_pkey")),
        sa.UniqueConstraint(
            "provider_connection_id", name="uq_green_pool_provider_id",
        ),
        sa.CheckConstraint(
            "state IN ('creating', 'available', 'claimed', 'failed')",
            name="green_pool_state_check",
        ),
        sa.CheckConstraint(
            "state = 'creating' OR provider_connection_id IS NOT NULL",
            name="green_pool_id_when_not_creating_check",
        ),
        sa.CheckConstraint(
            "state IN ('creating', 'failed') OR api_token IS NOT NULL",
            name="green_pool_credentials_when_ready_check",
        ),
        sa.CheckConstraint(
            "state IN ('creating', 'available', 'failed') "
            "OR webhook_token_hash IS NOT NULL",
            name="green_pool_webhook_hash_when_ready_check",
        ),
        sa.CheckConstraint(
            "state <> 'claimed' "
            "OR (claimed_by_user_id IS NOT NULL AND claimed_at IS NOT NULL)",
            name="green_pool_claimed_ownership_check",
        ),
    )
    op.create_index(
        "ix_green_pool_state",
        "green_instance_pool",
        ["state"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_green_pool_state", table_name="green_instance_pool")
    op.drop_table("green_instance_pool")
