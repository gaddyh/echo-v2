"""Persist Guard chat names on immutable review results.

Revision ID: 0035_guard_review_chat_name
Revises: 0034_guard_review
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0035_guard_review_chat_name"
down_revision = "0034_guard_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("guard_analysis_results", sa.Column("chat_name", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("guard_analysis_results", "chat_name")
