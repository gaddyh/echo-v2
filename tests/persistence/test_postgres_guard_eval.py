"""PostgreSQL tests for isolated Guard Eval runs and judgments."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

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


async def test_eval_admission_enforces_limits_and_recovers_stale_runs(
    session_factory, clean_db
) -> None:
    repository = PostgresGuardEvalRepository(session_factory)
    now = datetime.now(timezone.utc)

    def request(request_id: str, **limits: object) -> dict[str, object]:
        values: dict[str, object] = {
            "scenario_id": "gold-1", "requester_key": "requester-2", "request_id": request_id,
            "gold_version": "5", "gold_label": "gold", "model": "model",
            "prompt_version": "prompt", "analyzer_version": "analyzer", "taxonomy_version": "v1",
            "metadata": {}, "requester_window": timedelta(hours=1), "now": now,
            "max_active": 2, "daily_limit": 10, "requester_limit": 3,
        }
        values.update(limits)
        return values

    first = await repository.admit_run(**request("first"))
    assert first.allowed
    active_limited = await repository.admit_run(**request("active", max_active=1))
    assert not active_limited.allowed and active_limited.reason == "active_limit"

    daily_limited = await repository.admit_run(**request("daily", max_active=10, daily_limit=1))
    assert not daily_limited.allowed and daily_limited.reason == "daily_limit"

    requester_limited = await repository.admit_run(
        **request("requester", max_active=10, daily_limit=10, requester_limit=1)
    )
    assert not requester_limited.allowed and requester_limited.reason == "requester_limit"

    recovered = await repository.recover_stale_runs(
        stale_after=timedelta(seconds=1), now=now + timedelta(hours=2)
    )
    assert recovered == 1
    missing_id = str(uuid4())
    assert await repository.get_run(missing_id) is None
    assert await repository.complete_run(missing_id) is None
    assert await repository.fail_run(missing_id, error="missing") is None
    assert await repository.set_judgment(
        snapshot_id=missing_id, reviewer_key="reviewer", label=GuardEvalJudgmentLabel.UNDECIDED,
        note=None, reason=None
    ) is None
    runs = await repository.list_runs(scenario_id="gold-1")
    assert len(runs) == 1 and runs[0].status is GuardEvalRunStatus.FAILED
