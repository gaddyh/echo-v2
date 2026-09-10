"""Add first_name to users.

Revision ID: 0009_user_first_name
Revises: 0008_daily_digests
Create Date: 2026-09-12

The morning digest template needs the user's first name for the
greeting: "בוקר טוב {{1}} 👋". This column is nullable — existing
users won't have a name until they set one or we resolve it from
contacts.
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_user_first_name"
down_revision = "0008_daily_digests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("first_name", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("users", "first_name")
