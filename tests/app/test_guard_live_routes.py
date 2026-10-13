"""API tests for Live Compare run replay and per-snapshot feedback."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from echo_v2.app.guard_demo_routes import build_guard_demo_router
from echo_v2.domain.guard_eval import (
    GuardEvalAdmission,
    GuardEvalJudgment,
    GuardEvalJudgmentLabel,
    GuardEvalRun,
    GuardEvalRunBundle,
    GuardEvalRunStatus,
    GuardEvalSnapshot,
)


class FakeLiveService:
    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        self.run = GuardEvalRun(
            id="run-1", scenario_id="gold_parent_01_he_knows_where_she_waits",
            requester_key="key", request_id="request", gold_version="5", gold_label="gold",
            status=GuardEvalRunStatus.COMPLETED, model="model", prompt_version="prompt",
            analyzer_version="analyzer", taxonomy_version="v1", created_at=now,
        )
        self.snapshot = GuardEvalSnapshot(
            id="snapshot-1", run_id="run-1", scenario_id=self.run.scenario_id,
            ordinal=0, after_message_id="m2", expected={"decision": "none"},
            actual={"decision": "watch"}, diff={"difference": True, "failure": True},
            has_difference=True, failure=True, created_at=now,
        )
        self.judgment: GuardEvalJudgment | None = None

    async def start(self, **kwargs):
        return GuardEvalAdmission(run=self.run, allowed=True)

    async def get(self, run_id: str):
        return GuardEvalRunBundle(
            self.run,
            (self.snapshot,),
            (self.judgment,) if self.judgment else (),
        )

    async def list(self, **kwargs):
        return (self.run,)

    async def set_judgment(self, **kwargs):
        self.judgment = GuardEvalJudgment(
            snapshot_id=kwargs["snapshot_id"], reviewer_key=kwargs["reviewer_key"],
            label=kwargs["label"], note=kwargs["note"], reason=kwargs["reason"],
            created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
        )
        return self.judgment


def _client(service: FakeLiveService) -> AsyncClient:
    app = FastAPI()
    app.include_router(build_guard_demo_router(live_compare=service))
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://test")


async def test_live_run_start_returns_async_location() -> None:
    service = FakeLiveService()
    async with _client(service) as client:
        response = await client.post(
            "/api/demo/guard/live/runs",
            json={"scenario_id": service.run.scenario_id, "request_id": "new-request"},
        )

    assert response.status_code == 202
    assert response.json()["run_id"] == "run-1"
    assert response.headers["location"].endswith("run-1")


async def test_saved_run_bundle_and_snapshot_feedback_are_replayable() -> None:
    service = FakeLiveService()
    async with _client(service) as client:
        runs = await client.get("/api/demo/guard/live/runs?scenario_id=gold_parent_01_he_knows_where_she_waits")
        bundle = await client.get("/api/demo/guard/live/runs/run-1")
        feedback = await client.post(
            "/api/demo/guard/live/judgments",
            json={
                "snapshot_id": "snapshot-1",
                "label": GuardEvalJudgmentLabel.REAL_FAILURE.value,
                "note": "late",
                "reason": "The alert was delayed.",
            },
        )

    assert runs.status_code == 200
    assert runs.json()["runs"][0]["run_id"] == "run-1"
    assert bundle.status_code == 200
    assert bundle.json()["snapshots"][0]["after_message_id"] == "m2"
    assert bundle.json()["snapshots"][0]["failure"] is True
    assert feedback.status_code == 200
    assert feedback.json()["reason"] == "The alert was delayed."
