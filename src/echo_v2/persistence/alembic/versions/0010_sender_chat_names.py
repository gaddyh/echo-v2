"""Add sender_name, chat_name to messages + chat_name to chats.

Revision ID: 0010_sender_chat_names
Revises: 0009_user_first_name
Create Date: 2026-09-12

Green API sends senderData with chatName, senderName, and senderContactName
on every webhook. We were throwing this data away, causing the digest to
show raw phone numbers instead of names.

This migration adds:
- messages.sender_name (nullable) — the sender's display name
- messages.chat_name (nullable) — the chat's display name
- chats.chat_name (nullable) — the latest known chat name (updated on
  each message)
"""

import sqlalchemy as sa
from alembic import op

revision = "0010_sender_chat_names"
down_revision = "0009_user_first_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("sender_name", sa.Text, nullable=True))
    op.add_column("messages", sa.Column("chat_name", sa.Text, nullable=True))
    op.add_column("chats", sa.Column("chat_name", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("chats", "chat_name")
    op.drop_column("messages", "chat_name")
    op.drop_column("messages", "sender_name")
