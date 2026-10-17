"""Add guardian authorization and pending parent-child onboarding state.

Revision ID: 0049_parent_child_onboarding
Revises: 0048_pipeline_event_id_default
"""

from __future__ import annotations

from alembic import op

revision = "0049_parent_child_onboarding"
down_revision = "0048_pipeline_event_id_default"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE guardian_child_links "
        "RENAME COLUMN child_consented_at TO guardian_authorized_at"
    )
    op.execute("ALTER TABLE guardian_child_links DROP CONSTRAINT IF EXISTS guardian_child_links_status_check")
    op.create_check_constraint(
        "guardian_child_links_status_check",
        "guardian_child_links",
        "status IN ('pending', 'active', 'revoked')",
    )


def downgrade() -> None:
    op.execute("ALTER TABLE guardian_child_links DROP CONSTRAINT IF EXISTS guardian_child_links_status_check")
    op.create_check_constraint(
        "guardian_child_links_status_check",
        "guardian_child_links",
        "status IN ('active', 'revoked')",
    )
    op.execute(
        "ALTER TABLE guardian_child_links "
        "RENAME COLUMN guardian_authorized_at TO child_consented_at"
    )
