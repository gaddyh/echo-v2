"""Add live Baseline B queue and parent-attention delivery state.

Revision ID: 0041_guard_parent_attention_delivery
Revises: 0040_guard_simple_assessments
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0041_parent_attention"
down_revision = "0040_guard_simple_assessments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "guard_chat_state",
        sa.Column("last_parent_should_know", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "guard_chat_state",
        sa.Column("connection_id", postgresql.UUID(as_uuid=False), nullable=True),
    )
    op.create_foreign_key(
        "guard_chat_state_connection_id_fkey",
        "guard_chat_state",
        "whatsapp_connections",
        ["connection_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "guard_parent_attention_delivery",
        sa.Column("child_user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("last_delivered_assessment_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("last_delivered_evidence_ids", postgresql.JSONB(), nullable=False),
        sa.Column("last_delivered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_action_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("child_user_id", "chat_id", name="guard_parent_attention_delivery_pkey"),
        sa.ForeignKeyConstraint(["child_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["last_delivered_assessment_id"],
            ["guard_simple_assessments.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["last_action_id"], ["scheduled_actions.id"], ondelete="SET NULL"
        ),
    )


def downgrade() -> None:
    op.drop_table("guard_parent_attention_delivery")
    op.drop_constraint(
        "guard_chat_state_connection_id_fkey",
        "guard_chat_state",
        type_="foreignkey",
    )
    op.drop_column("guard_chat_state", "connection_id")
    op.drop_column("guard_chat_state", "last_parent_should_know")
