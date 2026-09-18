"""Rename audio_* columns to media_* on messages.

Revision ID: 0029_message_media_metadata
Revises: 0028_green_instance_pool
Create Date: 2026-09-19

The ``messages`` table previously stored media download metadata only
for audio messages under ``audio_*`` columns (migration ``0024``).
Media support is being generalized to cover images, video, and
documents, so the columns are renamed to ``media_*``:

- ``audio_download_url`` -> ``media_download_url``
- ``audio_mime_type``    -> ``media_mime_type``
- ``audio_file_name``    -> ``media_file_name``

Data is preserved (``ALTER TABLE ... RENAME COLUMN``); existing audio
rows keep their values under the new column names.
"""

import sqlalchemy as sa
from alembic import op

revision = "0029_message_media_metadata"
down_revision = "0028_green_instance_pool"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "messages",
        "audio_download_url",
        new_column_name="media_download_url",
        existing_type=sa.Text,
        nullable=True,
    )
    op.alter_column(
        "messages",
        "audio_mime_type",
        new_column_name="media_mime_type",
        existing_type=sa.Text,
        nullable=True,
    )
    op.alter_column(
        "messages",
        "audio_file_name",
        new_column_name="media_file_name",
        existing_type=sa.Text,
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "messages",
        "media_download_url",
        new_column_name="audio_download_url",
        existing_type=sa.Text,
        nullable=True,
    )
    op.alter_column(
        "messages",
        "media_mime_type",
        new_column_name="audio_mime_type",
        existing_type=sa.Text,
        nullable=True,
    )
    op.alter_column(
        "messages",
        "media_file_name",
        new_column_name="audio_file_name",
        existing_type=sa.Text,
        nullable=True,
    )
