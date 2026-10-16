"""Reconcile older Baseline B assessment schemas with the current model.

Revision ID: 0044_guard_assessment_compat
Revises: 0043_guard_assessment_chat_name
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044_guard_assessment_compat"
down_revision = "0043_guard_assessment_chat_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("guard_simple_assessments")}

    if "chat_name" not in columns:
        op.add_column(
            "guard_simple_assessments",
            sa.Column("chat_name", sa.Text(), nullable=False, server_default=""),
        )
    if "parent_should_know" not in columns:
        op.add_column(
            "guard_simple_assessments",
            sa.Column("parent_should_know", sa.Boolean(), nullable=True),
        )
        if "status" in columns:
            bind.execute(
                sa.text(
                    "UPDATE guard_simple_assessments "
                    "SET parent_should_know = CASE "
                    "WHEN status IN ('attention', 'urgent') THEN true "
                    "WHEN status = 'safe' THEN false "
                    "ELSE NULL END"
                )
            )
    if "status" in columns:
        op.alter_column(
            "guard_simple_assessments",
            "status",
            existing_type=sa.Text(),
            nullable=True,
        )
    if "first_detected_at" not in columns:
        op.add_column(
            "guard_simple_assessments",
            sa.Column("first_detected_at", sa.TIMESTAMP(timezone=True), nullable=True),
        )
    if "last_evidence_at" not in columns:
        op.add_column(
            "guard_simple_assessments",
            sa.Column("last_evidence_at", sa.TIMESTAMP(timezone=True), nullable=True),
        )


def downgrade() -> None:
    # Compatibility columns are intentionally retained during downgrade to
    # avoid destructive changes to deployed assessment history.
    pass
