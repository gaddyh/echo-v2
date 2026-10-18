"""Store child details for Echo Guard pilot signups.

Revision ID: 0052_waitlist_guard_children
Revises: 0051_guard_exposure_delivery
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0052_waitlist_guard_children"
down_revision = "0051_guard_exposure_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("waitlist_signups", sa.Column("children_count", sa.Text(), nullable=True))
    op.add_column("waitlist_signups", sa.Column("children_ages", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("waitlist_signups", "children_ages")
    op.drop_column("waitlist_signups", "children_count")
