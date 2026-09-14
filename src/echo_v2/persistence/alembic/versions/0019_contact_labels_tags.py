"""Add color_label and tags columns to contacts table.

Revision ID: 0019_contact_labels_tags
Revises: 0018_contact_starred
Create Date: 2026-09-14

Adds two columns to the ``contacts`` table:
* ``color_label`` — nullable TEXT with a CHECK constraint limiting
  values to the fixed palette (red, yellow, green, blue, purple) or NULL.
* ``tags`` — NOT NULL TEXT[] defaulting to an empty array. Free-text
  tags for semantic grouping of contacts.

Both columns are used by the waiting-list mini web app for visual
classification and client-side filtering.
"""

import sqlalchemy as sa
from alembic import op

revision = "0019_contact_labels_tags"
down_revision = "0018_contact_starred"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "contacts",
        sa.Column("color_label", sa.Text, nullable=True, server_default=None),
    )
    op.add_column(
        "contacts",
        sa.Column(
            "tags",
            sa.ARRAY(sa.Text),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )
    op.create_check_constraint(
        "contacts_color_label_check",
        "contacts",
        "color_label IS NULL OR color_label IN ('red','yellow','green','blue','purple')",
    )


def downgrade() -> None:
    op.drop_constraint("contacts_color_label_check", "contacts")
    op.drop_column("contacts", "tags")
    op.drop_column("contacts", "color_label")
