"""Async lifecycle service for public Guard Live Compare runs."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from echo_v2.domain.guard_eval import (
    GuardEvalAdmission,
    GuardEvalJudgment,
    GuardEvalJudgmentLabel,
    GuardEvalRun,
    GuardEvalRunBundle,
)
from echo_v2.persistence.guard_eval import GuardEvalRepository
from echo_v2.services.guard_eval_gold import GoldScenario, GuardGoldSet
from echo_v2.services.guard_live_compare import GuardLiveCompareRunner

_logger = logging.getLogger("echo_v2.services.guard_live_compare_service")


class GuardLiveCompareService:
    def __init__(
        self,
        *,
        repository: GuardEvalRepository,
        gold: GuardGoldSet,
        runner: GuardLiveCompareRunner,
        max_active_runs: int = 2,
        daily_limit: int = 30,
        requester_limit: int = 3,
        requester_window_seconds: int = 3600,
        stale_after_seconds: int = 1800,
    ) -> None:
        if min(max_active_runs, daily_limit, requester_limit) < 1:
            raise ValueError("Guard Live Compare limits must be positive")
        self._repository = repository
        self._gold = gold
        self._runner = runner
        self._max_active_runs = max_active_runs
        self._daily_limit = daily_limit
        self._requester_limit = requester_limit
        self._requester_window = timedelta(seconds=requester_window_seconds)
        self._stale_after = timedelta(seconds=stale_after_seconds)
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def start(
        self, *, scenario_id: str, requester_key: str, request_id: str
    ) -> GuardEvalAdmission:
        scenario = self._gold.scenario(scenario_id)
        await self._repository.recover_stale_runs(stale_after=self._stale_after)
        admission = await self._repository.admit_run(
            scenario_id=scenario.scenario_id,
            requester_key=requester_key,
            request_id=request_id,
            gold_version=self._gold.version,
            gold_label=self._gold.label,
            model=self._runner.model,
            prompt_version=self._runner.prompt_version,
            analyzer_version=self._runner.analyzer_version,
            taxonomy_version=self._runner.taxonomy_version,
            metadata={
                "execution": "async",
                "timing": "ordinal_only",
                "model_configuration": {
                    "model": self._runner.model,
                    "prompt_version": self._runner.prompt_version,
                    "analyzer_version": self._runner.analyzer_version,
                    "taxonomy_version": self._runner.taxonomy_version,
                },
            },
            max_active=self._max_active_runs,
            daily_limit=self._daily_limit,
            requester_limit=self._requester_limit,
            requester_window=self._requester_window,
        )
        run = admission.run
        if run is not None and not admission.duplicate:
            task = asyncio.create_task(self._execute(run.id, scenario))
            self._tasks[run.id] = task
            task.add_done_callback(lambda _: self._tasks.pop(run.id, None))
        return admission

    async def get(self, run_id: str) -> GuardEvalRunBundle | None:
        await self._repository.recover_stale_runs(stale_after=self._stale_after)
        return await self._repository.get_run(run_id)

    async def list(
        self, *, scenario_id: str | None = None, limit: int = 20
    ) -> tuple[GuardEvalRun, ...]:
        await self._repository.recover_stale_runs(stale_after=self._stale_after)
        return await self._repository.list_runs(scenario_id=scenario_id, limit=limit)

    async def set_judgment(
        self,
        *,
        snapshot_id: str,
        reviewer_key: str,
        label: GuardEvalJudgmentLabel,
        note: str | None,
        reason: str | None,
    ) -> GuardEvalJudgment | None:
        return await self._repository.set_judgment(
            snapshot_id=snapshot_id,
            reviewer_key=reviewer_key,
            label=label,
            note=note,
            reason=reason,
        )

    async def shutdown(self) -> None:
        active = tuple(self._tasks.items())
        for task in self._tasks.values():
            task.cancel()
        if active:
            await asyncio.gather(*(task for _, task in active), return_exceptions=True)
            for run_id, _ in active:
                await self._repository.fail_run(
                    run_id, error="run interrupted by application shutdown"
                )
        self._tasks.clear()

    async def recover_stale_runs(self) -> int:
        return await self._repository.recover_stale_runs(stale_after=self._stale_after)

    async def _execute(self, run_id: str, scenario: GoldScenario) -> None:
        try:
            results = await self._runner.run(scenario)
            for result in results:
                await self._repository.save_snapshot(
                    run_id=run_id,
                    scenario_id=scenario.scenario_id,
                    ordinal=result.ordinal,
                    after_message_id=result.after_message_id,
                    expected=result.expected,
                    actual=result.actual,
                    diff=result.diff,
                    has_difference=result.has_difference,
                    failure=result.failure,
                )
            await self._repository.complete_run(run_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _logger.exception("Guard Live Compare run failed: %s", run_id)
            await self._repository.fail_run(
                run_id, error=f"{type(exc).__name__}: {str(exc)[:800]}"
            )


__all__ = ["GuardLiveCompareService"]
