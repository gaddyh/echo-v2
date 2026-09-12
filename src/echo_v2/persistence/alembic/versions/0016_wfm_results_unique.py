"""Add UNIQUE constraint on (user_id, chat_id, target_version) to wfm_results.

Revision ID: 0016_wfm_results_unique
Revises: 0015_result_summary
Create Date: 2026-09-14

Adds ``UNIQUE (user_id, chat_id, target_version)`` to
``waiting_for_me_results``. This enforces the logical key documented in
the ORM model: one result per (chat, version) pair. Without this
constraint, a crash during analysis could produce duplicate results for
the same version.

The migration runs a preflight query to detect existing duplicates.
If duplicates are found, the migration aborts with a clear error —
duplicates must be reconciled manually before rerunning, because
active rows and feedback may reference a specific ``result_id``.
"""

import sqlalchemy as sa
from alembic import op

revision = "0016_wfm_results_unique"
down_revision = "0015_result_summary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Preflight: detect duplicate (user_id, chat_id, target_version) groups.
    conn = op.get_bind()
    duplicates = conn.execute(
        sa.text(
            "SELECT user_id, chat_id, target_version, COUNT(*) AS cnt "
            "FROM waiting_for_me_results "
            "GROUP BY user_id, chat_id, target_version "
            "HAVING COUNT(*) > 1"
        )
    ).fetchall()

    if duplicates:
        dup_count = len(duplicates)
        first = duplicates[0]
        raise RuntimeError(
            f"Cannot add UNIQUE constraint: found {dup_count} duplicate "
            f"(user_id, chat_id, target_version) group(s) in "
            f"waiting_for_me_results. First duplicate: "
            f"user_id={first[0]}, chat_id={first[1]}, "
            f"target_version={first[2]}, count={first[3]}. "
            f"Reconcile manually before rerunning the migration — "
            f"active rows and feedback may reference a specific result_id."
        )

    op.create_unique_constraint(
        "uq_wfm_results_user_chat_version",
        "waiting_for_me_results",
        ["user_id", "chat_id", "target_version"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_wfm_results_user_chat_version",
        "waiting_for_me_results",
        type_="unique",
    )
