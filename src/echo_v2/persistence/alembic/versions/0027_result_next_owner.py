"""Add next_owner and open_obligation columns to waiting_for_me_results.

Revision ID: 0027_result_next_owner
Revises: 0026_chat_not_interested_clicks
Create Date: 2026-09-18

Adds two nullable TEXT columns to ``waiting_for_me_results``:

- ``next_owner`` — who owns the next actionable step right now
  (``user`` / ``other`` / ``none`` / ``uncertain``), as emitted by the v3
  prompt. The product ``decision`` is derived deterministically from this
  value, but persisting the raw owner lets the debug view distinguish
  ``NOT_WAITING_FOR_ME / OTHER`` (an obligation exists but we're waiting on
  them) from ``NOT_WAITING_FOR_ME / NONE`` (no open obligation at all).
- ``open_obligation`` — short description of the owed action/reply and its
  owner, when ``next_owner`` is ``user`` or ``other``.

Old results (v0–v2 prompts) have both columns ``NULL``; the UI falls back
to the existing ``decision`` field, so no backfill is needed.
"""

import sqlalchemy as sa
from alembic import op

revision = "0027_result_next_owner"
down_revision = "0026_chat_not_interested_clicks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "waiting_for_me_results",
        sa.Column("next_owner", sa.Text, nullable=True),
    )
    op.add_column(
        "waiting_for_me_results",
        sa.Column("open_obligation", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("waiting_for_me_results", "open_obligation")
    op.drop_column("waiting_for_me_results", "next_owner")
