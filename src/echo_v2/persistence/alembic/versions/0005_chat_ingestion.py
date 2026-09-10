"""Chat ingestion: messages + chats tables.

Revision ID: 0005_chat_ingestion
Revises: 0004_send_bot_message
Create Date: 2026-09-10

Two tables:

* ``messages`` — immutable message records, deduplicated by
  ``(connection_id, provider_message_id)``. This is the sole dedup
  mechanism for ``ProviderMessageEvent``; status/state events keep
  using ``provider_webhook_events``.

* ``chats`` — compact per-chat state that also serves as the analysis
  queue. ``activity_version`` increments on every new message;
  ``next_analysis_at`` is set on inbound and cleared on outbound. The
  worker polls for chats where ``next_analysis_at`` has passed.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0005_chat_ingestion"
down_revision = "0004_send_bot_message"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- messages ---------------------------------------------------------
    op.create_table(
        "messages",
        sa.Column("id", UUID(as_uuid=False), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("connection_id", UUID(as_uuid=False), sa.ForeignKey("whatsapp_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chat_id", sa.Text, nullable=False),
        sa.Column("provider_message_id", sa.Text, nullable=False),
        sa.Column("direction", sa.Text, nullable=False),
        sa.Column("sender_id", sa.Text, nullable=True),
        sa.Column("timestamp", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("message_type", sa.Text, nullable=False),
        sa.Column("text", sa.Text, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("connection_id", "provider_message_id", name="messages_connection_provider_key"),
        sa.CheckConstraint("direction IN ('inbound', 'outbound')", name="messages_direction_check"),
    )
    op.create_index(
        "ix_messages_user_chat_timestamp",
        "messages",
        ["user_id", "chat_id", "timestamp"],
    )

    # --- chats ------------------------------------------------------------
    op.create_table(
        "chats",
        sa.Column("user_id", UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chat_id", sa.Text, nullable=False),
        sa.Column("activity_version", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("last_message_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_direction", sa.Text, nullable=False),
        sa.Column("next_analysis_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_processed_version", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("user_id", "chat_id", name="chats_pkey"),
        sa.CheckConstraint("last_direction IN ('inbound', 'outbound')", name="chats_last_direction_check"),
    )
    op.create_index("ix_chats_next_analysis_at", "chats", ["next_analysis_at"])


def downgrade() -> None:
    op.drop_index("ix_chats_next_analysis_at", table_name="chats")
    op.drop_table("chats")
    op.drop_index("ix_messages_user_chat_timestamp", table_name="messages")
    op.drop_table("messages")
