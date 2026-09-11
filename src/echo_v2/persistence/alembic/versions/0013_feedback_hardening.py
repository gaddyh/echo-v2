"""Feedback hardening: surrogate active_id, semantic dedup, column renames.

Revision ID: 0013_feedback_hardening
Revises: 0012_feedback_actions_mutes
Create Date: 2026-09-13

Five structural corrections:

1. Add surrogate ``id`` (UUID PK) to ``waiting_for_me_active`` so callback
   IDs don't expose provider-specific ``chat_id``. The old composite PK
   ``(user_id, chat_id)`` becomes a UNIQUE constraint.

2. Add ``UNIQUE(user_id, result_id)`` to ``waiting_for_me_feedback`` for
   semantic dedup — the first feedback for a given analysis result wins.
   ``NULL`` result_ids (false_negative reports) are allowed to duplicate.

3. Rename ``provider_event_id`` → ``provider_message_id`` on both
   ``waiting_for_me_feedback`` and ``waiting_for_me_actions``. The dedup
   value is the inbound ``wamid.*``, not the webhook delivery itself.

4. Update ``wfm_feedback_verdict_check`` to include ``'uncertain'``.

5. Add ``conversation_snapshot`` (JSONB) to ``waiting_for_me_results`` so
   the exact conversation at analysis time is preserved for training when
   feedback arrives later.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0013_feedback_hardening"
down_revision = "0012_feedback_actions_mutes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Surrogate id on waiting_for_me_active.
    op.add_column(
        "waiting_for_me_active",
        sa.Column("id", UUID(as_uuid=False), server_default=sa.text("gen_random_uuid()"), nullable=False),
    )
    # Drop old composite PK, add surrogate PK, add UNIQUE on old PK.
    op.execute("ALTER TABLE waiting_for_me_active DROP CONSTRAINT waiting_for_me_active_pkey")
    op.execute("ALTER TABLE waiting_for_me_active ADD PRIMARY KEY (id)")
    op.create_unique_constraint(
        "uq_wfm_active_user_chat",
        "waiting_for_me_active",
        ["user_id", "chat_id"],
    )

    # 2. Semantic dedup on feedback: UNIQUE(user_id, result_id).
    op.create_unique_constraint(
        "uq_wfm_feedback_user_result",
        "waiting_for_me_feedback",
        ["user_id", "result_id"],
    )

    # 3. Rename provider_event_id → provider_message_id.
    op.alter_column(
        "waiting_for_me_feedback",
        "provider_event_id",
        new_column_name="provider_message_id",
    )
    op.alter_column(
        "waiting_for_me_actions",
        "provider_event_id",
        new_column_name="provider_message_id",
    )
    # Recreate the dedup unique constraints with the new column name.
    op.drop_constraint("uq_wfm_feedback_event", "waiting_for_me_feedback", type_="unique")
    op.create_unique_constraint(
        "uq_wfm_feedback_message",
        "waiting_for_me_feedback",
        ["user_id", "provider_message_id"],
    )
    op.drop_constraint("uq_wfm_actions_event", "waiting_for_me_actions", type_="unique")
    op.create_unique_constraint(
        "uq_wfm_actions_message",
        "waiting_for_me_actions",
        ["user_id", "provider_message_id"],
    )

    # 4. Update verdict check to include 'uncertain'.
    op.drop_constraint("wfm_feedback_verdict_check", "waiting_for_me_feedback", type_="check")
    op.create_check_constraint(
        "wfm_feedback_verdict_check",
        "waiting_for_me_feedback",
        "verdict IN ('correct', 'false_positive', 'false_negative', 'uncertain')",
    )

    # 5. Add conversation_snapshot to waiting_for_me_results.
    op.add_column(
        "waiting_for_me_results",
        sa.Column("conversation_snapshot", sa.dialects.postgresql.JSONB, nullable=True),
    )


def downgrade() -> None:
    # 5. Remove conversation_snapshot.
    op.drop_column("waiting_for_me_results", "conversation_snapshot")

    # 4. Restore old verdict check.
    op.drop_constraint("wfm_feedback_verdict_check", "waiting_for_me_feedback", type_="check")
    op.create_check_constraint(
        "wfm_feedback_verdict_check",
        "waiting_for_me_feedback",
        "verdict IN ('correct', 'false_positive', 'false_negative')",
    )

    # 3. Rename back.
    op.drop_constraint("uq_wfm_actions_message", "waiting_for_me_actions", type_="unique")
    op.create_unique_constraint(
        "uq_wfm_actions_event",
        "waiting_for_me_actions",
        ["user_id", "provider_event_id"],
    )
    op.drop_constraint("uq_wfm_feedback_message", "waiting_for_me_feedback", type_="unique")
    op.create_unique_constraint(
        "uq_wfm_feedback_event",
        "waiting_for_me_feedback",
        ["user_id", "provider_event_id"],
    )
    op.alter_column(
        "waiting_for_me_actions",
        "provider_message_id",
        new_column_name="provider_event_id",
    )
    op.alter_column(
        "waiting_for_me_feedback",
        "provider_message_id",
        new_column_name="provider_event_id",
    )

    # 2. Drop semantic dedup.
    op.drop_constraint("uq_wfm_feedback_user_result", "waiting_for_me_feedback", type_="unique")

    # 1. Restore composite PK.
    op.drop_constraint("uq_wfm_active_user_chat", "waiting_for_me_active", type_="unique")
    op.execute("ALTER TABLE waiting_for_me_active DROP CONSTRAINT waiting_for_me_active_pkey")
    op.execute("ALTER TABLE waiting_for_me_active ADD PRIMARY KEY (user_id, chat_id)")
    op.drop_column("waiting_for_me_active", "id")
