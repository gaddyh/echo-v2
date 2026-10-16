"""Add chat name to existing Baseline B assessment databases.

Revision ID: 0043_guard_simple_assessment_chat_name
Revises: 0042_guard_activity_telemetry
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0043_guard_assessment_chat_name"
down_revision = "0042_guard_activity_telemetry"
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


def downgrade() -> None:
    # This compatibility migration is intentionally non-destructive: the column
    # may have been created by the original 0040 migration on fresh databases.
    pass
