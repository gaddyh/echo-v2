"""Add Guard relationships, independent queue state, and observations.

Revision ID: 0033_guard_shadow
Revises: 0032_message_source
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0033_guard_shadow"
down_revision = "0032_message_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guardian_child_links",
        sa.Column("id", postgresql.UUID(as_uuid=False), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("guardian_user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("child_user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("child_consented_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("safety_enabled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="guardian_child_links_pkey"),
        sa.ForeignKeyConstraint(["guardian_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["child_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("guardian_user_id", "child_user_id", name="guardian_child_links_pair_key"),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="guardian_child_links_status_check"),
    )
    op.create_index(
        "ix_guardian_child_links_child_status",
        "guardian_child_links",
        ["child_user_id", "status"],
    )

    op.create_table(
        "guard_chat_state",
        sa.Column("child_user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("activity_version", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_message_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_analyzed_version", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_decision", sa.Text(), server_default="none", nullable=False),
        sa.Column("last_analysis_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("pending_since", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("next_analysis_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("chat_name", sa.Text(), nullable=True),
        sa.Column("is_group", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("child_user_id", "chat_id", name="guard_chat_state_pkey"),
        sa.ForeignKeyConstraint(["child_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "last_decision IN ('none', 'watch', 'concerning', 'urgent')",
            name="guard_chat_state_decision_check",
        ),
    )
    op.create_index("ix_guard_chat_state_next_analysis_at", "guard_chat_state", ["next_analysis_at"])

    op.create_table(
        "guard_analysis_results",
        sa.Column("id", postgresql.UUID(as_uuid=False), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("child_user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("target_version", sa.BigInteger(), nullable=False),
        sa.Column("signals", postgresql.JSONB(), nullable=False),
        sa.Column("categories", postgresql.JSONB(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence_message_ids", postgresql.JSONB(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("analyzer_version", sa.Text(), nullable=False),
        sa.Column("taxonomy_version", sa.Text(), nullable=False),
        sa.Column("diagnostics", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="guard_analysis_results_pkey"),
        sa.ForeignKeyConstraint(["child_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["connection_id"], ["whatsapp_connections.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "child_user_id", "chat_id", "target_version", "analyzer_version",
            name="guard_analysis_results_version_key",
        ),
        sa.CheckConstraint(
            "decision IN ('none', 'watch', 'concerning', 'urgent')",
            name="guard_analysis_results_decision_check",
        ),
    )
    op.create_index(
        "ix_guard_analysis_results_child_chat_created",
        "guard_analysis_results",
        ["child_user_id", "chat_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_guard_analysis_results_child_chat_created", table_name="guard_analysis_results")
    op.drop_table("guard_analysis_results")
    op.drop_index("ix_guard_chat_state_next_analysis_at", table_name="guard_chat_state")
    op.drop_table("guard_chat_state")
    op.drop_index("ix_guardian_child_links_child_status", table_name="guardian_child_links")
    op.drop_table("guardian_child_links")
