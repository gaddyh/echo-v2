"""Add onboarding_status to users.

Revision ID: 0011_onboarding_status
Revises: 0010_sender_chat_names
Create Date: 2026-09-12

Tracks the onboarding state machine:
- ``pending``  — user created, Green instance created, OTP sent, waiting for pairing
- ``connected`` — Green API confirmed connection (stateInstanceChanged webhook)
- ``failed``   — pairing failed or timed out
- ``active``   — user completed onboarding (asked for name, ready to use)
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_onboarding_status"
down_revision = "0010_sender_chat_names"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("onboarding_status", sa.Text, nullable=True),
    )
    op.execute(
        "UPDATE users SET onboarding_status = 'active' "
        "WHERE account_status = 'active'"
    )


def downgrade() -> None:
    op.drop_column("users", "onboarding_status")
