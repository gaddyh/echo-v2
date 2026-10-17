"""Add a default UUID to pipeline event IDs.

Revision ID: 0048_pipeline_event_id_default
Revises: 0047_pipeline_events
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0048_pipeline_event_id_default"
down_revision = "0047_pipeline_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "pipeline_events",
        "id",
        server_default=sa.text("gen_random_uuid()"),
    )


def downgrade() -> None:
    op.alter_column("pipeline_events", "id", server_default=None)
