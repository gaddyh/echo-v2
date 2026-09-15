"""Add model, prompt_version, analyzer_version to waiting_for_me_results.

Revision ID: 0020_result_model_version
Revises: 0019_contact_labels_tags
Create Date: 2026-09-15

Adds three nullable TEXT columns to ``waiting_for_me_results`` so that
feedback (false positives / false negatives) can be correlated with the
exact model, prompt version, and analyzer version that produced the
result. Old rows have ``NULL`` — no backfill is needed.

Deploy order: run this migration BEFORE deploying the application code
that writes these columns, otherwise inserts will fail.
"""

import sqlalchemy as sa
from alembic import op

revision = "0020_result_model_version"
down_revision = "0019_contact_labels_tags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "waiting_for_me_results",
        sa.Column("model", sa.Text, nullable=True),
    )
    op.add_column(
        "waiting_for_me_results",
        sa.Column("prompt_version", sa.Text, nullable=True),
    )
    op.add_column(
        "waiting_for_me_results",
        sa.Column("analyzer_version", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("waiting_for_me_results", "analyzer_version")
    op.drop_column("waiting_for_me_results", "prompt_version")
    op.drop_column("waiting_for_me_results", "model")
