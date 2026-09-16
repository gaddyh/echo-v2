"""Add audio metadata columns to messages.

Revision ID: 0024_message_audio_metadata
Revises: 0023_waitlist_wtp_ranges
Create Date: 2026-09-16

Adds three nullable columns to the ``messages`` table to store audio
download metadata for inbound ``audioMessage`` events from Green API:

- ``audio_download_url`` — direct link to the audio file (from
  ``fileMessageData.downloadUrl``)
- ``audio_mime_type`` — media type (e.g. ``audio/ogg``)
- ``audio_file_name`` — auto-generated file name with extension
  (e.g. ``voice-message.ogg``)

These are populated at ingestion time. The ``ChatAnalysisProcessor``
uses them to lazily transcribe audio messages before LLM analysis,
then persists the transcript in the existing ``text`` column via
``MessageRepository.update_text``.
"""

import sqlalchemy as sa
from alembic import op

revision = "0024_message_audio_metadata"
down_revision = "0023_waitlist_wtp_ranges"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("audio_download_url", sa.Text, nullable=True))
    op.add_column("messages", sa.Column("audio_mime_type", sa.Text, nullable=True))
    op.add_column("messages", sa.Column("audio_file_name", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "audio_file_name")
    op.drop_column("messages", "audio_mime_type")
    op.drop_column("messages", "audio_download_url")
