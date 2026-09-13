"""Add is_starred column to contacts table.

Revision ID: 0018_contact_starred
Revises: 0017_bot_webhook_inbox
Create Date: 2026-09-13

Adds a boolean ``is_starred`` column to the ``contacts`` table, allowing
users to mark contacts as important from the waiting-list mini web app.
Starred contacts are sorted first in the single-card triage view.

The column defaults to ``FALSE`` so existing contacts are unaffected.
"""

import sqlalchemy as sa
from alembic import op

revision = "0018_contact_starred"
down_revision = "0017_bot_webhook_inbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "contacts",
        sa.Column("is_starred", sa.Boolean, nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("contacts", "is_starred")
