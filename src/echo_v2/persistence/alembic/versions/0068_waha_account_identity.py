"""WAHA account identity on sessions.

Revision ID: 0068_waha_account_identity
Revises: 0067_ops_center_telemetry

``waha_sessions`` gains the provider-reported WhatsApp account identity of
the paired account:

* ``account_phone`` — the phone number as reported by the provider in the
  phone-number JID (``<digits>@c.us`` / ``<digits>@s.whatsapp.net`` user
  part, digits only, no ``+``). Never derived from an opaque LID, a message
  sender, the session name or user input.
* ``account_identity_at`` — when the identity was last observed by Echo.

Both columns are nullable and backward-compatible: agents that do not
report identity simply leave them NULL, which the onboarding verification
treats as unverifiable (never as verified). Identity is cleared on any
applied transition into a non-WORKING lifecycle state (new pairing,
stop, failure) so a stale identity can never survive re-pairing.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0068_waha_account_identity"
down_revision = "0067_ops_center_telemetry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "waha_sessions",
        sa.Column("account_phone", sa.Text(), nullable=True),
    )
    op.add_column(
        "waha_sessions",
        sa.Column("account_identity_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("waha_sessions", "account_identity_at")
    op.drop_column("waha_sessions", "account_phone")
