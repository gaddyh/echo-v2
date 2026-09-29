"""Persist Guard evaluation results as JSON and Markdown."""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from tests.evaluation.guard.guard_cases import Decision, ExpectedSnapshot, GuardEvalCase

__all__ = ["GuardCaseResult", "SnapshotResult", "save_guard_eval_run"]

_RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
_SHORT: dict[Decision, str] = {
    "none": "NONE",
    "watch": "WATCH",
    "concerning": "CONCERN",
    "urgent": "URGENT",
}


@dataclass
class SnapshotResult:
    """Combined analyzer and policy result at one snapshot point."""

    snapshot: ExpectedSnapshot
    actual_decision: Decision | None = None
    actual_signals: tuple[str, ...] = ()
    actual_categories: tuple[str, ...] = ()
    actual_should_alert: bool | None = None
    decision_pass: bool = False
    signals_pass: bool = False
    categories_pass: bool = True
    alert_pass: bool | None = None
    error: str | None = None
    latency_ms: float | None = None
    raw_response: str = ""


@dataclass
class GuardCaseResult:
    """Result of running all snapshots for one case."""

    case: GuardEvalCase
    snapshots: list[SnapshotResult] = field(default_factory=list)


def _generate_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _latency_stats(snapshot_results: list[SnapshotResult]) -> dict[str, float | None]:
    latencies = [sr.latency_ms for sr in snapshot_results if sr.latency_ms is not None]
    if not latencies:
        return {"avg_ms": None, "p95_ms": None, "min_ms": None, "max_ms": None}
    ordered = sorted(latencies)
    p95_index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95) - 1))
    return {
        "avg_ms": round(statistics.mean(latencies), 1),
        "p95_ms": round(ordered[p95_index], 1),
        "min_ms": round(min(latencies), 1),
        "max_ms": round(max(latencies), 1),
    }


def _analysis_pass(sr: SnapshotResult) -> bool:
    return (
        sr.error is None
        and sr.decision_pass
        and sr.signals_pass
        and sr.categories_pass
    )


def _policy_pass(sr: SnapshotResult) -> bool:
    return sr.alert_pass is None or sr.alert_pass


def _snapshot_to_dict(case: GuardEvalCase, sr: SnapshotResult) -> dict[str, object]:
    prefix_length = next(
        (index + 1 for index, message in enumerate(case.messages)
         if message.id == sr.snapshot.after_message_id),
        len(case.messages),
    )
    return {
        "after_message_id": sr.snapshot.after_message_id,
        "prefix_length": prefix_length,
        "expected": {
            "acceptable_decisions": list(sr.snapshot.acceptable_decisions),
            "required_categories": list(sr.snapshot.required_categories),
            "required_signals": list(sr.snapshot.required_signals),
            "forbidden_signals": list(sr.snapshot.forbidden_signals),
            "should_alert": sr.snapshot.should_alert,
        },
        "actual": {
            "decision": sr.actual_decision,
            "signals": list(sr.actual_signals),
            "categories": list(sr.actual_categories),
            "should_alert": sr.actual_should_alert,
        },
        "pass": {
            "analyzer": _analysis_pass(sr),
            "policy": _policy_pass(sr),
            "end_to_end": _analysis_pass(sr) and _policy_pass(sr),
        },
        "error": sr.error,
        "latency_ms": sr.latency_ms,
        "raw_response": sr.raw_response,
    }


def _flatten(case_results: list[GuardCaseResult]) -> list[SnapshotResult]:
    return [sr for case in case_results for sr in case.snapshots]


def _build_json_output(
    run_id: str,
    label: str,
    model: str,
    case_results: list[GuardCaseResult],
    prompt_version: str,
) -> dict[str, object]:
    snapshots = _flatten(case_results)
    analyzer_passed = sum(_analysis_pass(sr) for sr in snapshots)
    policy_passed = sum(_policy_pass(sr) for sr in snapshots)
    end_to_end_passed = sum(_analysis_pass(sr) and _policy_pass(sr) for sr in snapshots)
    total = len(snapshots)
    errors = sum(sr.error is not None for sr in snapshots)
    return {
        "run_id": run_id,
        "label": label,
        "model": model,
        "prompt_version": prompt_version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_cases": len(case_results),
            "total_snapshots": total,
            "analyzer_passed": analyzer_passed,
            "policy_passed": policy_passed,
            "end_to_end_passed": end_to_end_passed,
            "analyzer_accuracy": analyzer_passed / total if total else 0.0,
            "policy_accuracy": policy_passed / total if total else 0.0,
            "end_to_end_accuracy": end_to_end_passed / total if total else 0.0,
            "errors": errors,
            "latency": _latency_stats(snapshots),
        },
        "cases": [
            {
                "case_id": case.case.case_id,
                "family": case.case.family,
                "source": case.case.source,
                "notes": case.case.notes,
                "messages": [
                    {"id": m.id, "sender": m.sender, "text": m.text}
                    for m in case.case.messages
                ],
                "snapshots": [_snapshot_to_dict(case.case, sr) for sr in case.snapshots],
            }
            for case in case_results
        ],
    }


def _build_markdown_report(
    run_id: str,
    label: str,
    model: str,
    case_results: list[GuardCaseResult],
    prompt_version: str,
) -> str:
    snapshots = _flatten(case_results)
    total = len(snapshots)
    analyzer_passed = sum(_analysis_pass(sr) for sr in snapshots)
    policy_passed = sum(_policy_pass(sr) for sr in snapshots)
    end_to_end_passed = sum(_analysis_pass(sr) and _policy_pass(sr) for sr in snapshots)
    lines = [
        f"# Guard Evaluation Report — {label}",
        "",
        f"- **Run ID:** `{run_id}`",
        f"- **Model:** `{model}`",
        f"- **Prompt:** `{prompt_version}`",
        f"- **Total snapshots:** {total}",
        f"- **Analyzer:** {analyzer_passed}/{total} ({analyzer_passed / total:.1%})" if total else "- **Analyzer:** 0/0",
        f"- **Policy:** {policy_passed}/{total} ({policy_passed / total:.1%})" if total else "- **Policy:** 0/0",
        f"- **End-to-end:** {end_to_end_passed}/{total} ({end_to_end_passed / total:.1%})" if total else "- **End-to-end:** 0/0",
        "",
        "| Case | After | Expected | Actual | Analyzer | Policy | Status |",
        "|---|---|---|---|---|---|---|",
    ]
    for case in case_results:
        for sr in case.snapshots:
            actual = _SHORT.get(sr.actual_decision, "ERR") if sr.actual_decision else "ERR"
            analyzer = "PASS" if _analysis_pass(sr) else "FAIL"
            policy = "PASS" if _policy_pass(sr) else "FAIL"
            status = "PASS" if _analysis_pass(sr) and _policy_pass(sr) else "FAIL"
            expected = "|".join(sr.snapshot.acceptable_decisions)
            lines.append(
                f"| {case.case.case_id} | {sr.snapshot.after_message_id} | "
                f"{expected} | {actual} | {analyzer} | {policy} | {status} |"
            )
    return "\n".join(lines) + "\n"


def save_guard_eval_run(
    label: str,
    model: str,
    case_results: list[GuardCaseResult],
    prompt_version: str = "",
) -> str:
    """Save a Guard eval run and return its run ID."""
    run_id = _generate_run_id()
    run_dir = _RESULTS_DIR / f"{run_id}_guard_{label.lower().replace(' ', '_')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    output = _build_json_output(run_id, label, model, case_results, prompt_version)
    (run_dir / "results.json").write_text(
        json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (run_dir / "report.md").write_text(
        _build_markdown_report(run_id, label, model, case_results, prompt_version),
        encoding="utf-8",
    )
    return run_id
