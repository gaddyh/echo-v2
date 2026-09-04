"""Contacts table for saved vCard contacts per user.

Revision ID: 0003_contacts
Revises: 0002_scheduled_actions
Create Date: 2026-09-04

Stores contacts extracted from vCards sent to the bot, so users can later
type a name instead of re-sending the vCard. Unique on (user_id, phone_number).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0003_contacts"
down_revision = "0002_scheduled_actions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "contacts",
        sa.Column("id", UUID(as_uuid=False), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("phone_number", sa.Text, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "phone_number", name="contacts_user_phone_key"),
    )
    op.create_index("ix_contacts_user_id", "contacts", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_contacts_user_id", table_name="contacts")
    op.drop_table("contacts")
