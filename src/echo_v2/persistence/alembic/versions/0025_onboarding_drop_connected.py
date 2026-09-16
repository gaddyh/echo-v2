"""Drop 'connected' from onboarding_status; name collected before provisioning.

Revision ID: 0025_onboarding_drop_connected
Revises: 0024_message_audio_metadata
Create Date: 2026-09-16

Simplifies the onboarding state machine:

Before:
    pending → connected → active
                 (name collected here)

After:
    pending → active
      (name collected before provisioning; connection state is owned
       by whatsapp_connections.status, not by users.onboarding_status)

The ``connected`` onboarding status is no longer used. Any existing
rows in ``connected`` are migrated to ``pending`` (provisioning still
in progress) so the lifecycle poll / webhook will complete them.
The CHECK constraint is replaced to drop ``connected`` from the
allowed values.
"""

from alembic import op

revision = "0025_onboarding_drop_connected"
down_revision = "0024_message_audio_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Migrate any rows still in 'connected' back to 'pending' so the
    # lifecycle poll / webhook can complete them under the new flow.
    op.execute(
        "UPDATE users SET onboarding_status = 'pending' "
        "WHERE onboarding_status = 'connected'"
    )
    # Drop the old constraint if it exists (it may not exist if the DB
    # was created from scratch via Base.metadata.create_all() rather than
    # by running migrations — migration 0011 only adds the column).
    op.execute(
        "ALTER TABLE users DROP CONSTRAINT IF EXISTS users_onboarding_status_check"
    )
    op.create_check_constraint(
        "users_onboarding_status_check",
        "users",
        "onboarding_status IS NULL "
        "OR onboarding_status IN ('pending','failed','active')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "users_onboarding_status_check",
        "users",
        type_="check",
    )
    op.create_check_constraint(
        "users_onboarding_status_check",
        "users",
        "onboarding_status IS NULL "
        "OR onboarding_status IN ('pending','connected','failed','active')",
    )
