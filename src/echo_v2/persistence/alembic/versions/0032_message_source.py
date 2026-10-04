"""Persist provider message source metadata."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032_message_source"
down_revision = "0031_message_media_reference"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("source", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "source")
