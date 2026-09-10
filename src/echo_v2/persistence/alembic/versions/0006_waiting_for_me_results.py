"""WaitingForMe results table.

Revision ID: 0006_waiting_for_me_results
Revises: 0005_chat_ingestion
Create Date: 2026-09-12

Stores immutable analysis results — one row per analysis run. The
``target_version`` links the result to the ``activity_version`` it was
computed from.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0006_waiting_for_me_results"
down_revision = "0005_chat_ingestion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "waiting_for_me_results",
        sa.Column("id", UUID(as_uuid=False), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chat_id", sa.Text, nullable=False),
        sa.Column("target_version", sa.Integer, nullable=False),
        sa.Column("decision", sa.Text, nullable=False),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "decision IN ('waiting_for_me', 'not_waiting_for_me', 'uncertain')",
            name="wfm_decision_check",
        ),
    )
    op.create_index(
        "ix_wfm_user_chat_created",
        "waiting_for_me_results",
        ["user_id", "chat_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_wfm_user_chat_created", table_name="waiting_for_me_results")
    op.drop_table("waiting_for_me_results")
