"""Add send_bot_message to scheduled_actions type constraint.

Revision ID: 0004_send_bot_message
Revises: 0003_contacts
Create Date: 2026-09-04

Allows scheduling self-reminders sent via the 360dialog bot channel
(no Green API connection needed).
"""

from alembic import op

revision = "0004_send_bot_message"
down_revision = "0003_contacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("scheduled_actions_type_check", "scheduled_actions")
    op.create_check_constraint(
        "scheduled_actions_type_check",
        "scheduled_actions",
        "type IN ('send_whatsapp_message','send_reminder','send_bot_message')",
    )


def downgrade() -> None:
    op.drop_constraint("scheduled_actions_type_check", "scheduled_actions")
    op.create_check_constraint(
        "scheduled_actions_type_check",
        "scheduled_actions",
        "type IN ('send_whatsapp_message','send_reminder')",
    )
