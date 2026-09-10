"""Feedback, actions, snooze, acknowledge, and chat mutes.

Revision ID: 0012_feedback_actions_mutes
Revises: 0011_onboarding_status
Create Date: 2026-09-12

Four concerns, cleanly separated:

1. ``acknowledged_at`` and ``snoozed_until`` on ``waiting_for_me_active``.
   ``acknowledged_at`` — set when the user taps "מטפל עכשיו".
   ``snoozed_until`` — set when the user taps "הזכר לי מחר"; the item
   is suppressed from digests until that time.

2. ``waiting_for_me_feedback`` — correctness signal only. Stores whether
   the model's analysis was correct (``correct``, ``false_positive``,
   ``false_negative``). Does NOT mutate active state. Linked to the exact
   ``result_id`` + ``target_version`` that the user is judging.

3. ``waiting_for_me_actions`` — user actions on active items. Each row
   records what the user asked Echo to do (``acknowledge``, ``snooze``,
   ``resolve``, ``mute_chat``, ``unmute_chat``). The action handler
   atomically records the event AND mutates current state. Idempotent
   via ``UNIQUE(user_id, provider_event_id)``.

4. ``chat_mutes`` — per-user, per-chat mute records. ``permanent = true``
   means muted forever (``muted_until IS NULL``). ``permanent = false``
   with a ``muted_until`` timestamp means a temporary mute. The CHECK
   constraint enforces the invariant.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0012_feedback_actions_mutes"
down_revision = "0011_onboarding_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add acknowledged_at + snoozed_until to waiting_for_me_active.
    op.add_column(
        "waiting_for_me_active",
        sa.Column("acknowledged_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "waiting_for_me_active",
        sa.Column("snoozed_until", sa.TIMESTAMP(timezone=True), nullable=True),
    )

    # 2. waiting_for_me_feedback — correctness signal only.
    op.create_table(
        "waiting_for_me_feedback",
        sa.Column("id", UUID(as_uuid=False), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chat_id", sa.Text, nullable=False),
        sa.Column("result_id", UUID(as_uuid=False), sa.ForeignKey("waiting_for_me_results.id", ondelete="SET NULL"), nullable=True),
        sa.Column("target_version", sa.Integer, nullable=True),
        sa.Column("verdict", sa.Text, nullable=False),
        sa.Column("conversation_snapshot", sa.dialects.postgresql.JSONB, nullable=True),
        sa.Column("provider_event_id", sa.Text, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_wfm_feedback_user_chat_created",
        "waiting_for_me_feedback",
        ["user_id", "chat_id", "created_at"],
    )
    op.create_unique_constraint(
        "uq_wfm_feedback_event",
        "waiting_for_me_feedback",
        ["user_id", "provider_event_id"],
    )
    op.create_check_constraint(
        "wfm_feedback_verdict_check",
        "waiting_for_me_feedback",
        "verdict IN ('correct', 'false_positive', 'false_negative')",
    )

    # 3. waiting_for_me_actions — user actions on active items.
    op.create_table(
        "waiting_for_me_actions",
        sa.Column("id", UUID(as_uuid=False), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chat_id", sa.Text, nullable=False),
        sa.Column("active_id", sa.Text, nullable=True),
        sa.Column("target_version", sa.Integer, nullable=True),
        sa.Column("action_type", sa.Text, nullable=False),
        sa.Column("action_payload", sa.dialects.postgresql.JSONB, nullable=True),
        sa.Column("provider_event_id", sa.Text, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_wfm_actions_user_chat_created",
        "waiting_for_me_actions",
        ["user_id", "chat_id", "created_at"],
    )
    op.create_unique_constraint(
        "uq_wfm_actions_event",
        "waiting_for_me_actions",
        ["user_id", "provider_event_id"],
    )
    op.create_check_constraint(
        "wfm_action_type_check",
        "waiting_for_me_actions",
        "action_type IN ('acknowledge', 'snooze', 'resolve', 'mute_chat', 'unmute_chat')",
    )

    # 4. chat_mutes table.
    op.create_table(
        "chat_mutes",
        sa.Column("user_id", UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("chat_id", sa.Text, primary_key=True),
        sa.Column("muted_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("permanent", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "(permanent = true AND muted_until IS NULL) "
            "OR (permanent = false AND muted_until IS NOT NULL)",
            name="chat_mutes_consistency_check",
        ),
    )


def downgrade() -> None:
    op.drop_table("chat_mutes")
    op.drop_constraint("wfm_action_type_check", "waiting_for_me_actions", type_="check")
    op.drop_constraint("uq_wfm_actions_event", "waiting_for_me_actions", type_="unique")
    op.drop_index("ix_wfm_actions_user_chat_created", table_name="waiting_for_me_actions")
    op.drop_table("waiting_for_me_actions")
    op.drop_constraint("wfm_feedback_verdict_check", "waiting_for_me_feedback", type_="check")
    op.drop_constraint("uq_wfm_feedback_event", "waiting_for_me_feedback", type_="unique")
    op.drop_index("ix_wfm_feedback_user_chat_created", table_name="waiting_for_me_feedback")
    op.drop_table("waiting_for_me_feedback")
    op.drop_column("waiting_for_me_active", "snoozed_until")
    op.drop_column("waiting_for_me_active", "acknowledged_at")
