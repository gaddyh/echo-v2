"""Track authenticated boundaries for Baileys reconnect episodes.

Revision ID: 0054_baileys_auth_episodes
Revises: 0053_guard_reconnect_requests
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0054_baileys_auth_episodes"
down_revision = "0053_guard_reconnect_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("whatsapp_connections", sa.Column("last_authenticated_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("whatsapp_connections", sa.Column("auth_loss_episode_key", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE whatsapp_connections c
           SET last_authenticated_at = c.updated_at
         WHERE c.provider = 'baileys'
           AND EXISTS (
               SELECT 1 FROM guardian_child_links l
                WHERE l.child_user_id = c.user_id
                  AND l.status = 'active'
                  AND l.guardian_authorized_at IS NOT NULL
                  AND l.safety_enabled_at IS NOT NULL
           )
        """
    )


def downgrade() -> None:
    op.drop_column("whatsapp_connections", "auth_loss_episode_key")
    op.drop_column("whatsapp_connections", "last_authenticated_at")
