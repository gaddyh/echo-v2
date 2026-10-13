"""Lifecycle tests for asynchronous Live Compare runs."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from echo_v2.domain.guard_eval import (
    GuardEvalAdmission,
    GuardEvalJudgment,
    GuardEvalJudgmentLabel,
    GuardEvalRun,
    GuardEvalRunBundle,
    GuardEvalRunStatus,
    GuardEvalSnapshot,
)
from echo_v2.services.guard_eval_gold import load_guard_gold
from echo_v2.services.guard_live_compare import LiveSnapshotExecution
from echo_v2.services.guard_live_compare_service import GuardLiveCompareService


class FakeRepository:
    def __init__(self) -> None:
        self.run = GuardEvalRun(
            id="run-1",
            scenario_id="gold_parent_01_he_knows_where_she_waits",
            requester_key="requester",
            request_id="request",
            gold_version="5",
            gold_label="gold",
            status=GuardEvalRunStatus.RUNNING,
            model="model",
            prompt_version="prompt",
            analyzer_version="analyzer",
            taxonomy_version="v1",
            created_at=datetime.now(timezone.utc),
        )
        self.snapshots: list[GuardEvalSnapshot] = []
        self.judgment: GuardEvalJudgment | None = None
        self.failed_error: str | None = None

    async def recover_stale_runs(self, *, stale_after, now=None) -> int:
        return 0

    async def admit_run(self, **kwargs) -> GuardEvalAdmission:
        return GuardEvalAdmission(run=self.run, allowed=True)

    async def save_snapshot(self, **kwargs) -> GuardEvalSnapshot:
        snapshot = GuardEvalSnapshot(
            id=f"snapshot-{len(self.snapshots) + 1}",
            run_id=kwargs["run_id"],
            scenario_id=kwargs["scenario_id"],
            ordinal=kwargs["ordinal"],
            after_message_id=kwargs["after_message_id"],
            expected=kwargs["expected"],
            actual=kwargs["actual"],
            diff=kwargs["diff"],
            has_difference=kwargs["has_difference"],
            failure=kwargs["failure"],
            created_at=datetime.now(timezone.utc),
        )
        self.snapshots.append(snapshot)
        return snapshot

    async def complete_run(self, run_id: str, **kwargs) -> GuardEvalRun:
        self.run = GuardEvalRun(**{**self.run.__dict__, "status": GuardEvalRunStatus.COMPLETED})
        return self.run

    async def fail_run(self, run_id: str, *, error: str, **kwargs) -> GuardEvalRun:
        self.failed_error = error
        self.run = GuardEvalRun(**{**self.run.__dict__, "status": GuardEvalRunStatus.FAILED})
        return self.run

    async def get_run(self, run_id: str) -> GuardEvalRunBundle:
        return GuardEvalRunBundle(self.run, tuple(self.snapshots), ())

    async def list_runs(self, **kwargs) -> tuple[GuardEvalRun, ...]:
        return (self.run,)

    async def set_judgment(self, **kwargs) -> GuardEvalJudgment:
        self.judgment = GuardEvalJudgment(
            snapshot_id=kwargs["snapshot_id"],
            reviewer_key=kwargs["reviewer_key"],
            label=kwargs["label"],
            note=kwargs["note"],
            reason=kwargs["reason"],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        return self.judgment


class FakeRunner:
    model = "model"
    prompt_version = "prompt"
    analyzer_version = "analyzer"
    taxonomy_version = "v1"

    async def run(self, scenario, on_snapshot=None):
        result = LiveSnapshotExecution(
            ordinal=0,
            after_message_id=scenario.checkpoints[0].after_message_id,
            expected={"decision": "none"},
            actual={"decision": "none"},
            diff={"difference": False, "failure": False},
            has_difference=False,
            failure=False,
        )
        if on_snapshot is not None:
            await on_snapshot(result)
        return (result,)


class FailingRunner(FakeRunner):
    async def run(self, scenario, on_snapshot=None):
        raise RuntimeError("runner failed")


async def test_service_streams_snapshots_and_supports_saved_run_feedback() -> None:
    repository = FakeRepository()
    service = GuardLiveCompareService(
        repository=repository,
        gold=load_guard_gold(),
        runner=FakeRunner(),
    )

    admission = await service.start(
        scenario_id="gold_parent_01_he_knows_where_she_waits",
        requester_key="requester",
        request_id="request",
    )
    await asyncio.sleep(0)
    assert admission.allowed
    assert len(repository.snapshots) == 1
    bundle = await service.get(repository.run.id)
    assert bundle is not None and len(bundle.snapshots) == 1
    assert (await service.list())[0].id == repository.run.id
    assert await service.recover_stale_runs() == 0

    judgment = await service.set_judgment(
        snapshot_id=repository.snapshots[0].id,
        reviewer_key="reviewer",
        label=GuardEvalJudgmentLabel.REAL_FAILURE,
        note="note",
        reason="reason",
    )
    assert judgment is not None and judgment.reason == "reason"
    await service.shutdown()


async def test_service_persists_failed_run_when_runner_raises() -> None:
    repository = FakeRepository()
    service = GuardLiveCompareService(
        repository=repository,
        gold=load_guard_gold(),
        runner=FailingRunner(),
    )
    await service.start(
        scenario_id="gold_parent_01_he_knows_where_she_waits",
        requester_key="requester",
        request_id="request",
    )
    await asyncio.sleep(0)
    assert repository.failed_error == "RuntimeError: runner failed"


def test_service_rejects_non_positive_limits() -> None:
    try:
        GuardLiveCompareService(
            repository=FakeRepository(), gold=load_guard_gold(), runner=FakeRunner(), max_active_runs=0
        )
    except ValueError as exc:
        assert "limits" in str(exc)
    else:
        raise AssertionError("expected invalid limit failure")
