"""Fence stale Baileys connection-state events by connector state version.

Revision ID: 0055_baileys_state_version
Revises: 0054_baileys_auth_episodes
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0055_baileys_state_version"
down_revision = "0054_baileys_auth_episodes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "whatsapp_connections",
        sa.Column("baileys_state_version", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("whatsapp_connections", "baileys_state_version")
