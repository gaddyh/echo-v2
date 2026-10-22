"""Add alerted_incidents table for the Incident Tracker.

Revision ID: 0057_alerted_incidents
Revises: 0056_multi_guardian_outcomes

Stores incidents that have triggered a parent alert, serving as semantic
deduplication memory for subsequent YES assessments.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0057_alerted_incidents"
down_revision = "0056_multi_guardian_outcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alerted_incidents",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("child_user_id", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("alert_type", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("severity_summary", sa.Text(), nullable=False),
        sa.Column("notified_severity_summary", sa.Text(), nullable=True),
        sa.Column("evidence_message_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("assessment_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("related_incident_id", sa.Text(), sa.ForeignKey("alerted_incidents.id"), nullable=True),
        sa.Column("notification_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "alert_type IN ('security', 'exposure')",
            name="alerted_incidents_alert_type_check",
        ),
    )
    op.create_index(
        "idx_alerted_incidents_lookup",
        "alerted_incidents",
        ["child_user_id", "chat_id", "alert_type", "closed_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_alerted_incidents_lookup", table_name="alerted_incidents")
    op.drop_table("alerted_incidents")
