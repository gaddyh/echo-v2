"""Add waitlist_signups table for the landing page.

Revision ID: 0021_waitlist_signups
Revises: 0020_result_model_version
Create Date: 2026-09-15

Landing-page waitlist: name + canonical E.164 phone number, deduplicated
by phone. Not linked to ``users`` — signups happen before onboarding.
"""

import sqlalchemy as sa
from alembic import op

revision = "0021_waitlist_signups"
down_revision = "0020_result_model_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "waitlist_signups",
        sa.Column(
            "id",
            sa.Uuid,
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("phone_number", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("phone_number", name="uq_waitlist_phone"),
    )


def downgrade() -> None:
    op.drop_table("waitlist_signups")
