"""Store the selected Echo Guard connection preference on waitlist signups.

Revision ID: 0037_waitlist_preference
Revises: 0036_guard_eval
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0037_waitlist_preference"
down_revision = "0036_guard_eval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "waitlist_signups",
        sa.Column("preference", sa.Text, nullable=True),
    )
    op.create_check_constraint(
        "waitlist_preference_check",
        "waitlist_signups",
        "preference IN ('android', 'box', 'both')",
    )


def downgrade() -> None:
    op.drop_constraint("waitlist_preference_check", "waitlist_signups")
    op.drop_column("waitlist_signups", "preference")
