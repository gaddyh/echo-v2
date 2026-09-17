"""chat_not_interested_clicks — escalating chat snooze counter.

Revision ID: 0026_chat_not_interested_clicks
Revises: 0025_onboarding_drop_connected
Create Date: 2026-09-17

Per-user, per-chat counter for "לא מעניין, שיחכו" clicks. Drives the
escalating chat snooze (24h → 48h → 1 week → permanent mute). The
counter is reset (row deleted) when the user engages with the chat
via "טופל" / "בוצע".
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0026_chat_not_interested_clicks"
down_revision = "0025_onboarding_drop_connected"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_not_interested_clicks",
        sa.Column(
            "user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("chat_id", sa.Text, primary_key=True, nullable=False),
        sa.Column("click_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "last_click_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("chat_not_interested_clicks")
