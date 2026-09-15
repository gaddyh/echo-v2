"""Add willingness_to_pay to waitlist_signups.

Revision ID: 0022_waitlist_wtp
Revises: 0021_waitlist_signups
Create Date: 2026-09-15

Optional willingness-to-pay signal collected at signup ("how much would
you pay per month?"): ``free`` / ``under_30`` / ``30_70`` / ``70_120`` / ``120_plus``.
Nullable — the question is optional and skippable.
"""

import sqlalchemy as sa
from alembic import op

revision = "0022_waitlist_wtp"
down_revision = "0021_waitlist_signups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "waitlist_signups",
        sa.Column("willingness_to_pay", sa.Text, nullable=True),
    )
    op.create_check_constraint(
        "waitlist_wtp_check",
        "waitlist_signups",
        "willingness_to_pay IN ('free', 'under_30', '30_70', '70_120', '120_plus')",
    )


def downgrade() -> None:
    op.drop_constraint("waitlist_wtp_check", "waitlist_signups")
    op.drop_column("waitlist_signups", "willingness_to_pay")
