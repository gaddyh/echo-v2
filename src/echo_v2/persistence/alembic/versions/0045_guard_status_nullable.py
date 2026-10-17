"""Allow current Baseline B writes on legacy assessment tables.

Revision ID: 0045_guard_status_nullable
Revises: 0044_guard_assessment_compat
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0045_guard_status_nullable"
down_revision = "0044_guard_assessment_compat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("guard_simple_assessments")}
    if "status" in columns:
        op.alter_column(
            "guard_simple_assessments",
            "status",
            existing_type=sa.Text(),
            nullable=True,
        )


def downgrade() -> None:
    # Keep the legacy column nullable; current Baseline B writes do not use it.
    pass
