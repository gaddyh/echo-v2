"""Persist Guard evaluation run results as JSON + Markdown.

Each run gets a unique ``run_id`` (timestamp-based) and a directory under
``tests/evaluation/results/`` containing:

- ``results.json`` — machine-readable: run metadata, per-case per-snapshot
  details (input prefix, expected, actual, pass/fail, signals, categories,
  raw LLM response, error).
- ``report.md`` — human-readable: summary table, per-snapshot table,
  failures detail.

Usage from the eval harness::

    from tests.evaluation.guard.guard_eval_results import (
        GuardCaseResult,
        save_guard_eval_run,
    )
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from tests.evaluation.guard.guard_cases import (
    Decision,
    ExpectedSnapshot,
    GuardEvalCase,
)

__all__ = [
    "GuardCaseResult",
    "SnapshotResult",
    "save_guard_eval_run",
]

_RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"

_SHORT: dict[Decision, str] = {
    "none": "NONE",
    "watch": "WATCH",
    "concerning": "CONCERN",
    "urgent": "URGENT",
}


@dataclass
class SnapshotResult:
    """Result of running the analyzer at one snapshot point.

    Attributes:
        snapshot: The expected snapshot.
        actual_decision: The analyzer's decision, or ``None`` on error.
        actual_signals: Signals the analyzer reported.
        actual_categories: Categories the analyzer reported.
        decision_pass: Whether the decision is in acceptable_decisions.
        signals_pass: Whether all required_signals are present and
            none of forbidden_signals are present.
        categories_pass: Whether all required_categories are present.
        alert_pass: Whether should_alert matches the analyzer's alert
            behavior (decision != "none"). ``None`` if should_alert is
            not specified.
        error: Error message if the snapshot errored, else ``None``.
        latency_ms: Time to run the snapshot in milliseconds.
        raw_response: The raw LLM response text.
    """

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
    """Result of running all snapshots for one case.

    Attributes:
        case: The original :class:`GuardEvalCase`.
        snapshots: Per-snapshot results.
    """

    case: GuardEvalCase
    snapshots: list[SnapshotResult] = field(default_factory=list)


def _generate_run_id() -> str:
    """Generate a timestamp-based run ID: ``YYYYMMDD_HHMMSS``."""
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _latency_stats(snapshot_results: list[SnapshotResult]) -> dict:
    """Compute latency statistics in milliseconds."""
    latencies = [sr.latency_ms for sr in snapshot_results if sr.latency_ms is not None]
    if not latencies:
        return {"avg_ms": None, "p95_ms": None, "min_ms": None, "max_ms": None}
    sorted_lat = sorted(latencies)
    n = len(sorted_lat)
    p95_idx = max(0, min(n - 1, int(n * 0.95) - 1))
    return {
        "avg_ms": round(statistics.mean(latencies), 1),
        "p95_ms": round(sorted_lat[p95_idx], 1),
        "min_ms": round(min(latencies), 1),
        "max_ms": round(max(latencies), 1),
    }


def _snapshot_to_dict(
    case: GuardEvalCase, sr: SnapshotResult, prefix_len: int
) -> dict:
    """Convert a SnapshotResult to a JSON-serializable dict."""
    return {
        "after_message_id": sr.snapshot.after_message_id,
        "prefix_length": prefix_len,
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
            "decision": sr.decision_pass,
            "signals": sr.signals_pass,
            "categories": sr.categories_pass,
            "alert": sr.alert_pass,
        },
        "error": sr.error,
        "latency_ms": sr.latency_ms,
        "raw_response": sr.raw_response,
    }


def _build_json_output(
    run_id: str,
    label: str,
    model: str,
    case_results: list[GuardCaseResult],
    accuracy: float,
    total_snapshots: int,
    passed_snapshots: int,
    errors: int,
    prompt_version: str = "",
) -> dict:
    """Build the JSON-serializable output dict."""
    all_snapshot_results = [
        sr for cr in case_results for sr in cr.snapshots
    ]
    return {
        "run_id": run_id,
        "label": label,
        "model": model,
        "prompt_version": prompt_version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_cases": len(case_results),
            "total_snapshots": total_snapshots,
            "passed_snapshots": passed_snapshots,
            "errors": errors,
            "accuracy": round(accuracy, 4),
            "latency": _latency_stats(all_snapshot_results),
        },
        "cases": [
            {
                "case_id": cr.case.case_id,
                "family": cr.case.family,
                "source": cr.case.source,
                "notes": cr.case.notes,
                "messages": [
                    {"id": m.id, "sender": m.sender, "text": m.text}
                    for m in cr.case.messages
                ],
                "snapshots": [
                    _snapshot_to_dict(cr.case, sr, _prefix_len(cr.case, sr))
                    for sr in cr.snapshots
                ],
            }
            for cr in case_results
        ],
    }


def _prefix_len(case: GuardEvalCase, sr: SnapshotResult) -> int:
    """Number of messages in the prefix up to and after_message_id."""
    for i, msg in enumerate(case.messages):
        if msg.id == sr.snapshot.after_message_id:
            return i + 1
    return len(case.messages)


def _build_markdown_report(
    run_id: str,
    label: str,
    model: str,
    case_results: list[GuardCaseResult],
    accuracy: float,
    total_snapshots: int,
    passed_snapshots: int,
    errors: int,
    prompt_version: str = "",
) -> str:
    """Build a human-readable Markdown report."""
    all_snapshot_results = [
        sr for cr in case_results for sr in cr.snapshots
    ]
    lat_stats = _latency_stats(all_snapshot_results)
    lines = [
        f"# Guard Evaluation Report — {label}",
        "",
        f"- **Run ID:** `{run_id}`",
        f"- **Model:** `{model}`",
    ]
    if prompt_version:
        lines.append(f"- **Prompt:** `{prompt_version}`")
    lines += [
        f"- **Timestamp:** {datetime.now(timezone.utc).isoformat()}",
        f"- **Total cases:** {len(case_results)}",
        f"- **Total snapshots:** {total_snapshots}",
        f"- **Passed snapshots:** {passed_snapshots}",
        f"- **Errors:** {errors}",
        f"- **Snapshot accuracy:** {accuracy:.1%}",
    ]
    if lat_stats["avg_ms"] is not None:
        lines.append(
            f"- **Latency:** avg {lat_stats['avg_ms']:.0f}ms, "
            f"p95 {lat_stats['p95_ms']:.0f}ms"
        )
    lines += ["", "## Per-Snapshot Results", ""]
    lines.append(
        "| Case | After | Expected | Actual | Decision | Signals | "
        "Categories | Alert | Status |"
    )
    lines.append(
        "|------|-------|----------|--------|----------|---------|"
        "------------|-------|--------|"
    )
    for cr in case_results:
        for sr in cr.snapshots:
            expected_str = "|".join(sr.snapshot.acceptable_decisions)
            actual_str = _SHORT.get(sr.actual_decision, "?") if sr.actual_decision else "ERR"
            decision_mark = "✓" if sr.decision_pass else "✗"
            signals_mark = "✓" if sr.signals_pass else "✗"
            categories_mark = "✓" if sr.categories_pass else "✗"
            if sr.alert_pass is None:
                alert_mark = "—"
            else:
                alert_mark = "✓" if sr.alert_pass else "✗"
            if sr.error:
                status = "ERR"
            elif (
                sr.decision_pass
                and sr.signals_pass
                and sr.categories_pass
                and (sr.alert_pass is None or sr.alert_pass)
            ):
                status = "PASS"
            else:
                status = "FAIL"
            lines.append(
                f"| {cr.case.case_id} | {sr.snapshot.after_message_id} | "
                f"{expected_str} | {actual_str} | {decision_mark} | "
                f"{signals_mark} | {categories_mark} | {alert_mark} | {status} |"
            )
    return "\n".join(lines) + "\n"


def save_guard_eval_run(
    label: str,
    model: str,
    case_results: list[GuardCaseResult],
    prompt_version: str = "",
) -> str:
    """Save a Guard eval run to disk and return the ``run_id``.

    Creates ``tests/evaluation/results/<run_id>_guard_<label>/`` with
    ``results.json`` and ``report.md``.
    """
    run_id = _generate_run_id()
    dir_name = f"{run_id}_guard_{label.lower().replace(' ', '_')}"
    run_dir = _RESULTS_DIR / dir_name
    run_dir.mkdir(parents=True, exist_ok=True)

    total_snapshots = sum(len(cr.snapshots) for cr in case_results)
    passed_snapshots = sum(
        1
        for cr in case_results
        for sr in cr.snapshots
        if sr.error is None
        and sr.decision_pass
        and sr.signals_pass
        and sr.categories_pass
        and (sr.alert_pass is None or sr.alert_pass)
    )
    errors = sum(
        1 for cr in case_results for sr in cr.snapshots if sr.error is not None
    )
    accuracy = passed_snapshots / total_snapshots if total_snapshots else 0.0

    json_output = _build_json_output(
        run_id, label, model, case_results, accuracy,
        total_snapshots, passed_snapshots, errors, prompt_version,
    )
    (run_dir / "results.json").write_text(
        json.dumps(json_output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    md_report = _build_markdown_report(
        run_id, label, model, case_results, accuracy,
        total_snapshots, passed_snapshots, errors, prompt_version,
    )
    (run_dir / "report.md").write_text(md_report, encoding="utf-8")

    return run_id
