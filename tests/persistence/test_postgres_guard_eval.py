"""PostgreSQL tests for isolated Guard Eval runs and judgments."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.domain.guard_eval import GuardEvalJudgmentLabel, GuardEvalRunStatus
from echo_v2.persistence.guard_eval import PostgresGuardEvalRepository

pytestmark = pytest.mark.asyncio


async def test_eval_admission_persists_immutable_run_and_mutable_judgment(
    session_factory, clean_db
) -> None:
    repository = PostgresGuardEvalRepository(session_factory)
    now = datetime.now(timezone.utc)
    admission = await repository.admit_run(
        scenario_id="gold-1",
        requester_key="requester-1",
        request_id="request-1",
        gold_version="5",
        gold_label="parent_attention_gold_v3",
        model="test-model",
        prompt_version="test-prompt",
        analyzer_version="test-analyzer",
        taxonomy_version="v1",
        metadata={"timing": "ordinal_only"},
        max_active=2,
        daily_limit=10,
        requester_limit=3,
        requester_window=timedelta(hours=1),
        now=now,
    )

    assert admission.allowed and admission.run is not None
    duplicate = await repository.admit_run(
        scenario_id="gold-1",
        requester_key="requester-1",
        request_id="request-1",
        gold_version="5",
        gold_label="parent_attention_gold_v3",
        model="test-model",
        prompt_version="test-prompt",
        analyzer_version="test-analyzer",
        taxonomy_version="v1",
        metadata={},
        max_active=2,
        daily_limit=10,
        requester_limit=3,
        requester_window=timedelta(hours=1),
        now=now,
    )
    assert duplicate.duplicate and duplicate.run is not None
    assert duplicate.run.id == admission.run.id

    snapshot = await repository.save_snapshot(
        run_id=admission.run.id,
        scenario_id="gold-1",
        ordinal=0,
        after_message_id="m1",
        expected={"decision": "watch"},
        actual={"decision": "watch", "analyzer_input": {"messages": []}},
        diff={"difference": False, "failure": False},
        has_difference=False,
        failure=False,
    )
    judgment = await repository.set_judgment(
        snapshot_id=snapshot.id,
        reviewer_key="anonymous-1",
        label=GuardEvalJudgmentLabel.REAL_FAILURE,
        note="Needs review",
        reason="The alert was too late.",
    )
    assert judgment is not None
    assert judgment.reason == "The alert was too late."
    updated = await repository.set_judgment(
        snapshot_id=snapshot.id,
        reviewer_key="anonymous-1",
        label=GuardEvalJudgmentLabel.ACCEPTED_DIFFERENCE,
        note="Accepted after review",
        reason="The final decision still arrived on time.",
    )
    assert updated is not None
    assert updated.label is GuardEvalJudgmentLabel.ACCEPTED_DIFFERENCE

    completed = await repository.complete_run(admission.run.id, now=now)
    assert completed is not None and completed.status is GuardEvalRunStatus.COMPLETED
    bundle = await repository.get_run(admission.run.id)
    assert bundle is not None
    assert len(bundle.snapshots) == 1
    assert bundle.judgments[0].reason == "The final decision still arrived on time."
