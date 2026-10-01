"""Persist Guard analyzer evaluation results as JSON and Markdown."""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from tests.evaluation.guard.guard_cases import GuardEvalCase

__all__ = ["GuardCaseResult", "SnapshotResult", "save_guard_eval_run"]

_RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
@dataclass
class SnapshotResult:
    """Result of running the analyzer at one snapshot point."""

    after_message_id: str
    actual_signals: tuple[str, ...] = ()
    actual_categories: tuple[str, ...] = ()
    actual_evidence_message_ids: tuple[str, ...] = ()
    signals_pass: bool = False
    any_signal_pass: bool = True
    categories_pass: bool = True
    error: str | None = None
    latency_ms: float | None = None
    raw_response: str = ""


@dataclass
class GuardCaseResult:
    """Result of running all analyzer snapshots for one case."""

    case: GuardEvalCase
    snapshots: list[SnapshotResult] = field(default_factory=list)


def _analysis_pass(result: SnapshotResult) -> bool:
    return (
        result.error is None
        and result.signals_pass
        and result.any_signal_pass
        and result.categories_pass
    )


def _latency_stats(results: list[SnapshotResult]) -> dict[str, float | None]:
    latencies = [result.latency_ms for result in results if result.latency_ms is not None]
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


def _snapshot_to_dict(case: GuardEvalCase, result: SnapshotResult) -> dict[str, object]:
    prefix_length = next(
        (index + 1 for index, message in enumerate(case.messages)
         if message.id == result.after_message_id),
        len(case.messages),
    )
    expected = next(
        snapshot for snapshot in case.snapshots
        if snapshot.after_message_id == result.after_message_id
    )
    return {
        "after_message_id": result.after_message_id,
        "prefix_length": prefix_length,
        "expected": {
            "required_categories": list(expected.required_categories),
            "required_signals": list(expected.required_signals),
            "required_signal_any_of": list(expected.required_signal_any_of),
            "forbidden_signals": list(expected.forbidden_signals),
        },
        "actual": {
            "signals": list(result.actual_signals),
            "categories": list(result.actual_categories),
            "evidence_message_ids": list(result.actual_evidence_message_ids),
        },
        "pass": {
            "analyzer": _analysis_pass(result),
            "required_signals": result.signals_pass,
            "required_signal_any_of": result.any_signal_pass,
            "categories": result.categories_pass,
        },
        "error": result.error,
        "latency_ms": result.latency_ms,
        "raw_response": result.raw_response,
    }


def _flatten(case_results: list[GuardCaseResult]) -> list[SnapshotResult]:
    return [result for case in case_results for result in case.snapshots]


def _build_report(
    run_id: str,
    label: str,
    model: str,
    prompt_version: str,
    case_results: list[GuardCaseResult],
) -> tuple[dict[str, object], str]:
    results = _flatten(case_results)
    total = len(results)
    passed = sum(_analysis_pass(result) for result in results)
    output = {
        "run_id": run_id,
        "label": label,
        "model": model,
        "prompt_version": prompt_version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_cases": len(case_results),
            "total_snapshots": total,
            "passed_snapshots": passed,
            "analyzer_accuracy": passed / total if total else 0.0,
            "errors": sum(result.error is not None for result in results),
            "latency": _latency_stats(results),
        },
        "cases": [
            {
                "case_id": case.case.case_id,
                "family": case.case.family,
                "source": case.case.source,
                "notes": case.case.notes,
                "messages": [
                    {"id": message.id, "sender": message.sender, "text": message.text}
                    for message in case.case.messages
                ],
                "snapshots": [
                    _snapshot_to_dict(case.case, result)
                    for result in case.snapshots
                ],
            }
            for case in case_results
        ],
    }

    lines = [
        f"# Guard Analyzer Evaluation Report — {label}",
        "",
        f"- **Run ID:** `{run_id}`",
        f"- **Model:** `{model}`",
        f"- **Prompt:** `{prompt_version}`",
        f"- **Snapshots:** {total}",
        f"- **Analyzer pass:** {passed}/{total} ({passed / total:.1%})" if total else "- **Analyzer pass:** 0/0",
        "",
        "| Case | After | Evidence | Signals | Categories | Status |",
        "|---|---|---|---|---|---|"
    ]
    for case in case_results:
        for result in case.snapshots:
            lines.append(
                f"| {case.case.case_id} | {result.after_message_id} | "
                f"{','.join(result.actual_evidence_message_ids) or '-'} | "
                f"{result.actual_signals} | {result.actual_categories} | "
                f"{'PASS' if _analysis_pass(result) else 'FAIL'} |"
            )
    return output, "\n".join(lines) + "\n"


def save_guard_eval_run(
    label: str,
    model: str,
    case_results: list[GuardCaseResult],
    prompt_version: str = "",
) -> str:
    """Save an analyzer-only Guard eval run and return its run ID."""
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_dir = _RESULTS_DIR / f"{run_id}_guard_{label.lower().replace(' ', '_')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    output, report = _build_report(run_id, label, model, prompt_version, case_results)
    (run_dir / "results.json").write_text(
        json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    return run_id
