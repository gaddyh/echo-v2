"""Add durable provider media references to messages."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031_message_media_reference"
down_revision = "0030_baileys_event_processing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("media_reference", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("messages", "media_reference")
