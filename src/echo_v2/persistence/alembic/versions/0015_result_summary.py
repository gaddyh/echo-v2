"""Add summary column to waiting_for_me_results.

Revision ID: 0015_result_summary
Revises: 0014_waiting_list_sessions
Create Date: 2026-09-14

Adds a ``summary`` TEXT NULL column to ``waiting_for_me_results``. The
summary is a one-sentence, user-facing Hebrew description of the
situation and what is being waited for. It is produced by the LLM
analyzer alongside the existing decision/confidence/reason fields.

Old results have ``summary = NULL``; the UI falls back to the message
preview when ``summary`` is absent, so no backfill is needed.
"""

import sqlalchemy as sa
from alembic import op

revision = "0015_result_summary"
down_revision = "0014_waiting_list_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "waiting_for_me_results",
        sa.Column("summary", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("waiting_for_me_results", "summary")
