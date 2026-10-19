"""Add a persistent state for the Guard guardian-name step.

Revision ID: 0027_guardian_name_onboarding
Revises: 0026_chat_not_interested_clicks
Create Date: 2026-10-20
"""

from alembic import op

revision = "0027_guardian_name_onboarding"
down_revision = "0026_chat_not_interested_clicks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("users_onboarding_status_check", "users", type_="check")
    op.create_check_constraint(
        "users_onboarding_status_check",
        "users",
        "onboarding_status IS NULL OR onboarding_status IN "
        "('pending','failed','active','awaiting_guardian_name')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE users SET onboarding_status = 'active' "
        "WHERE onboarding_status = 'awaiting_guardian_name'"
    )
    op.drop_constraint("users_onboarding_status_check", "users", type_="check")
    op.create_check_constraint(
        "users_onboarding_status_check",
        "users",
        "onboarding_status IS NULL OR onboarding_status IN ('pending','failed','active')",
    )
