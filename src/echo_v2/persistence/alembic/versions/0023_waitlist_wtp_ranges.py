"""Expand waitlist WTP ranges.

Revision ID: 0023_waitlist_wtp_ranges
Revises: 0022_waitlist_wtp
Create Date: 2026-09-15

Replace the WTP CHECK constraint with the expanded ranges:
``free`` / ``under_30`` / ``30_70`` / ``70_120`` / ``120_plus``.
Any rows that used the old ranges (``under_20`` / ``20_50`` / ``50_plus``)
are migrated to the nearest new bucket before the constraint is swapped.
"""

from alembic import op

revision = "0023_waitlist_wtp_ranges"
down_revision = "0022_waitlist_wtp"
branch_labels = None
depends_on = None

_OLD_VALUES = ("under_20", "20_50", "50_plus")
_NEW_VALUES = ("free", "under_30", "30_70", "70_120", "120_plus")


def upgrade() -> None:
    # Migrate any rows stored under the old ranges to the closest new bucket.
    op.execute(
        "UPDATE waitlist_signups SET willingness_to_pay = 'under_30' "
        "WHERE willingness_to_pay = 'under_20'"
    )
    op.execute(
        "UPDATE waitlist_signups SET willingness_to_pay = '30_70' "
        "WHERE willingness_to_pay = '20_50'"
    )
    op.execute(
        "UPDATE waitlist_signups SET willingness_to_pay = '70_120' "
        "WHERE willingness_to_pay = '50_plus'"
    )

    op.drop_constraint("waitlist_wtp_check", "waitlist_signups")
    op.create_check_constraint(
        "waitlist_wtp_check",
        "waitlist_signups",
        "willingness_to_pay IN ('free', 'under_30', '30_70', '70_120', '120_plus')",
    )


def downgrade() -> None:
    op.drop_constraint("waitlist_wtp_check", "waitlist_signups")
    op.create_check_constraint(
        "waitlist_wtp_check",
        "waitlist_signups",
        "willingness_to_pay IN ('free', 'under_20', '20_50', '50_plus')",
    )
