"""Add media_size column to chatmessage.

Revision ID: 0058_message_media_size
Revises: 0057_alerted_incidents

Stores the file size (in bytes) of downloaded media alongside the existing
media_reference/media_mime_type/media_file_name columns. Populated by the
connector event payload after successful ingestion-time download.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0058_message_media_size"
down_revision = "0057_alerted_incidents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("media_size", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "media_size")
