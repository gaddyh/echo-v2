"""Persist evaluation run results as JSON + Markdown.

Each run gets a unique ``run_id`` (timestamp-based) and a directory under
``tests/evaluation/results/`` containing:

- ``results.json`` — machine-readable: run metadata, per-case details
  (input messages, expected, actual, pass/fail, confidence, reason,
  summary, raw LLM response, error).
- ``report.md`` — human-readable: summary table, confusion matrix,
  per-case table, failures detail.

Usage from eval harnesses::

    from tests.evaluation.eval_results import CaseResult, save_eval_run

    case_results = [
        CaseResult(case=case, actual=decision, raw_response=raw, error=err)
        ...
    ]
    run_id = save_eval_run(
        label="SOC Family",
        model="gpt-4.1",
        cases=SANITY_CASES,
        case_results=case_results,
    )
    print(f"Results saved: run_id={run_id}")
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from echo_v2.domain.waiting_for_me import WaitingForMeDecision
from tests.evaluation.waiting_for_me_cases import EvalCase

__all__ = ["CaseResult", "save_eval_run"]

_RESULTS_DIR = Path(__file__).resolve().parent / "results"

_SHORT = {
    WaitingForMeDecision.WAITING_FOR_ME: "WFM",
    WaitingForMeDecision.NOT_WAITING_FOR_ME: "NWM",
    WaitingForMeDecision.UNCERTAIN: "UNC",
}

_LABELS = [
    WaitingForMeDecision.WAITING_FOR_ME,
    WaitingForMeDecision.NOT_WAITING_FOR_ME,
    WaitingForMeDecision.UNCERTAIN,
]


@dataclass
class CaseResult:
    """Result of running a single eval case.

    Attributes:
        case: The original :class:`EvalCase`.
        actual: The LLM's decision, or ``None`` on error.
        raw_response: The raw LLM response text (full JSON string).
        error: Error message if the case errored, else ``None``.
        latency_ms: Time to run the case in milliseconds.
    """

    case: EvalCase
    actual: WaitingForMeDecision | None = None
    raw_response: str = ""
    error: str | None = None
    latency_ms: float | None = None


def _generate_run_id() -> str:
    """Generate a timestamp-based run ID: ``YYYYMMDD_HHMMSS``."""
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _latency_stats(case_results: list[CaseResult]) -> dict:
    """Compute latency statistics in milliseconds."""
    latencies = [cr.latency_ms for cr in case_results if cr.latency_ms is not None]
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


def _build_json_output(
    run_id: str,
    label: str,
    model: str,
    case_results: list[CaseResult],
    accuracy: float,
    correct: int,
    errors: int,
    prompt_version: str = "",
) -> dict:
    """Build the JSON-serializable output dict."""
    return {
        "run_id": run_id,
        "label": label,
        "model": model,
        "prompt_version": prompt_version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total": len(case_results),
            "correct": correct,
            "errors": errors,
            "accuracy": round(accuracy, 4),
            "by_category": _category_breakdown(case_results),
            "latency": _latency_stats(case_results),
        },
        "cases": [_case_to_dict(cr) for cr in case_results],
    }


def _category_breakdown(case_results: list[CaseResult]) -> dict:
    """Build per-category accuracy breakdown."""
    breakdown = {}
    for label in _LABELS:
        total = sum(1 for cr in case_results if cr.case.expected == label)
        correct = sum(
            1 for cr in case_results
            if cr.case.expected == label and cr.actual == cr.case.expected
        )
        key = _SHORT[label]
        breakdown[key] = {
            "total": total,
            "correct": correct,
            "accuracy": round(correct / total, 4) if total else 0.0,
        }
    return breakdown


def _case_to_dict(cr: CaseResult) -> dict:
    """Convert a CaseResult to a JSON-serializable dict."""
    return {
        "id": cr.case.id,
        "description": cr.case.description,
        "family": cr.case.family,
        "source_group": cr.case.source_group,
        "split": cr.case.split,
        "messages": [
            {"direction": d, "text": t} for d, t in cr.case.messages
        ],
        "expected": cr.case.expected.value,
        "actual": cr.actual.value if cr.actual else None,
        "pass": cr.actual == cr.case.expected if cr.actual else False,
        "confidence": None,  # populated below if result available
        "reason": None,
        "summary": None,
        "raw_response": cr.raw_response,
        "error": cr.error,
        "latency_ms": cr.latency_ms,
        "notes": cr.case.notes,
    }


def _extract_parsed_fields(cr: CaseResult) -> dict:
    """Extract confidence/reason/summary from the raw LLM response."""
    if not cr.raw_response:
        return {}
    try:
        raw = cr.raw_response.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1] if "\n" in raw else raw[3:]
            raw = raw.removesuffix("```").strip()
        data = json.loads(raw)
        return {
            "confidence": data.get("confidence"),
            "reason": data.get("reason"),
            "summary": data.get("summary"),
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}


def _build_markdown_report(
    run_id: str,
    label: str,
    model: str,
    case_results: list[CaseResult],
    accuracy: float,
    correct: int,
    errors: int,
    prompt_version: str = "",
) -> str:
    """Build a human-readable Markdown report."""
    total = len(case_results)
    lat_stats = _latency_stats(case_results)
    lines = [
        f"# WaitingForMe Evaluation Report — {label}",
        "",
        f"- **Run ID:** `{run_id}`",
        f"- **Model:** `{model}`",
    ]
    if prompt_version:
        lines.append(f"- **Prompt:** `{prompt_version}`")
    lines += [
        f"- **Timestamp:** {datetime.now(timezone.utc).isoformat()}",
        f"- **Total cases:** {total}",
        f"- **Correct:** {correct}",
        f"- **Errors:** {errors}",
        f"- **Accuracy:** {accuracy:.1%}",
        f"- **Latency:** avg {lat_stats['avg_ms']:.0f}ms, p95 {lat_stats['p95_ms']:.0f}ms"
        if lat_stats["avg_ms"] is not None
        else "- **Latency:** N/A",
        "",
        "## Summary by Category",
        "",
        "| Category | Correct | Total | Accuracy |",
        "|----------|---------|-------|----------|",
    ]

    for label_val in _LABELS:
        cat_total = sum(1 for cr in case_results if cr.case.expected == label_val)
        cat_correct = sum(
            1 for cr in case_results
            if cr.case.expected == label_val and cr.actual == cr.case.expected
        )
        cat_acc = f"{cat_correct / cat_total:.0%}" if cat_total else "—"
        lines.append(
            f"| {_SHORT[label_val]} | {cat_correct} | {cat_total} | {cat_acc} |"
        )

    # Latency summary
    lines += ["", "## Latency Summary", ""]
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    if lat_stats["avg_ms"] is not None:
        lines.append(f"| Average | {lat_stats['avg_ms']:.0f}ms |")
        lines.append(f"| P95 | {lat_stats['p95_ms']:.0f}ms |")
        lines.append(f"| Min | {lat_stats['min_ms']:.0f}ms |")
        lines.append(f"| Max | {lat_stats['max_ms']:.0f}ms |")
    else:
        lines.append("| Average | N/A |")

    # Confusion matrix
    lines += ["", "## Confusion Matrix (rows=expected, cols=actual)", ""]
    header = "| Expected \\ Actual | " + " | ".join(_SHORT[d] for d in _LABELS) + " |"
    sep = "|---" * (len(_LABELS) + 1) + "|"
    lines += [header, sep]
    for expected in _LABELS:
        row = [f"**{_SHORT[expected]}**"]
        for actual in _LABELS:
            count = sum(
                1 for cr in case_results
                if cr.case.expected == expected and cr.actual == actual
            )
            row.append(str(count) if count else "·")
        lines.append("| " + " | ".join(row) + " |")

    # Per-case table
    lines += ["", "## Per-Case Results", ""]
    lines.append("| Status | ID | Msgs | Expected | Actual | Conf | Latency | Description |")
    lines.append("|--------|----|------|----------|--------|------|---------|-------------|")
    for cr in case_results:
        if cr.error:
            status = "ERR"
            actual_str = "ERROR"
            conf = ""
        elif cr.actual == cr.case.expected:
            status = "PASS"
            actual_str = _SHORT.get(cr.actual, "?")
            conf = ""
        else:
            status = "FAIL"
            actual_str = _SHORT.get(cr.actual, "?")
            conf = ""
        # Extract confidence from raw response
        parsed = _extract_parsed_fields(cr)
        if parsed.get("confidence") is not None:
            conf = f"{parsed['confidence']:.2f}"
        latency_str = f"{cr.latency_ms:.0f}ms" if cr.latency_ms is not None else "—"
        lines.append(
            f"| {status} | {cr.case.id} | {len(cr.case.messages)} | "
            f"{_SHORT[cr.case.expected]} | {actual_str} | {conf} | "
            f"{latency_str} | {cr.case.description[:60]} |"
        )

    # Failures detail
    failures = [cr for cr in case_results if cr.actual != cr.case.expected or cr.error]
    if failures:
        lines += ["", "## Failures Detail", ""]
        for cr in failures:
            parsed = _extract_parsed_fields(cr)
            lines += [
                f"### {cr.case.id} — {cr.case.description}",
                "",
                f"- **Expected:** {_SHORT[cr.case.expected]}",
                f"- **Actual:** {_SHORT.get(cr.actual, 'ERROR') if cr.actual else 'ERROR'}",
            ]
            if parsed.get("confidence") is not None:
                lines.append(f"- **Confidence:** {parsed['confidence']:.2f}")
            if parsed.get("reason"):
                lines.append(f"- **LLM Reason:** {parsed['reason']}")
            if parsed.get("summary"):
                lines.append(f"- **LLM Summary:** {parsed['summary']}")
            if cr.error:
                lines.append(f"- **Error:** {cr.error}")
            if cr.latency_ms is not None:
                lines.append(f"- **Latency:** {cr.latency_ms:.0f}ms")
            lines.append(f"- **Notes:** {cr.case.notes}")
            lines += ["", "**Messages:**", ""]
            for direction, text in cr.case.messages:
                label_d = "them" if direction == "inbound" else "me"
                lines.append(f"> **{label_d}:** {text}")
            lines += ["", "**Raw LLM Response:**", "```json"]
            lines.append(cr.raw_response or "(empty)")
            lines.append("```")
            lines.append("")

    if not failures:
        lines += ["", "## Failures Detail", "", "No failures! All cases passed.", ""]

    return "\n".join(lines)


def save_eval_run(
    label: str,
    model: str,
    case_results: list[CaseResult],
    *,
    output_dir: Path | None = None,
    prompt_version: str = "",
) -> str:
    """Save eval results as JSON + Markdown.

    Args:
        label: Human-readable label for the run (e.g. ``"Sanity"``, ``"SOC Family"``).
        model: Model name used (e.g. ``"gpt-4.1"``).
        case_results: List of :class:`CaseResult` for each case.
        output_dir: Base directory for results. Defaults to
            ``tests/evaluation/results/``.
        prompt_version: Prompt version used (e.g. ``"v0"``, ``"v1"``).

    Returns:
        The ``run_id`` (timestamp-based, e.g. ``"20260115_143022"``).
    """
    run_id = _generate_run_id()
    base_dir = output_dir or _RESULTS_DIR

    # Sanitize label for directory name.
    safe_label = label.lower().replace(" ", "_")
    run_dir = base_dir / f"{run_id}_{safe_label}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Compute summary stats.
    correct = sum(
        1 for cr in case_results if cr.actual == cr.case.expected
    )
    errors = sum(1 for cr in case_results if cr.error or cr.actual is None)
    total = len(case_results)
    accuracy = correct / total if total > 0 else 0.0

    # Build and save JSON.
    json_output = _build_json_output(
        run_id, label, model, case_results, accuracy, correct, errors,
        prompt_version=prompt_version,
    )
    # Enrich cases with parsed fields.
    for case_dict, cr in zip(json_output["cases"], case_results):
        parsed = _extract_parsed_fields(cr)
        case_dict.update(parsed)

    json_path = run_dir / "results.json"
    json_path.write_text(
        json.dumps(json_output, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    # Build and save Markdown.
    markdown = _build_markdown_report(
        run_id, label, model, case_results, accuracy, correct, errors,
        prompt_version=prompt_version,
    )
    md_path = run_dir / "report.md"
    md_path.write_text(markdown, encoding="utf-8")

    return run_id
