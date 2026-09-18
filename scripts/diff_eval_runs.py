#!/usr/bin/env python3
"""Aggregate 3-vs-3 eval runs and report per-case stability categories.

Compares baseline (e.g. v1) vs candidate (e.g. v3) eval runs, where each
version has been run multiple times (typically 3) to smooth out
reasoning-model variance. For each case, counts how many baseline runs
passed and how many candidate runs passed, then classifies the case:

    FIXED            baseline <= 1 and candidate >= 2
    REGRESSED        baseline >= 2 and candidate <= 1
    UNCHANGED_CORRECT baseline >= 2 and candidate >= 2
    UNCHANGED_WRONG  baseline <= 1 and candidate <= 1
    UNSTABLE         anything in between (e.g. 1→2, 2→1)

The most important regression metric is NWM→WFM: a case that was
NOT_WAITING_FOR_ME in baseline but flipped to WAITING_FOR_ME in candidate
represents a new false waiting-list item.

Usage:
    python scripts/diff_eval_runs.py \\
        --baseline tests/evaluation/results/run1_v1 tests/evaluation/results/run2_v1 tests/evaluation/results/run3_v1 \\
        --candidate tests/evaluation/results/run1_v3 tests/evaluation/results/run2_v3 tests/evaluation/results/run3_v3

Each argument is a path to a results directory (containing results.json)
or a direct path to a results.json file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import NamedTuple

# --- decision short labels -------------------------------------------------
_SHORT = {
    "waiting_for_me": "WFM",
    "not_waiting_for_me": "NWM",
    "uncertain": "UNC",
}


class RunSummary(NamedTuple):
    run_id: str
    model: str
    prompt_version: str
    label: str
    total: int
    correct: int
    errors: int
    accuracy: float
    by_category: dict
    cases: dict[str, dict]  # case_id -> case dict


def _load_run(path: Path) -> RunSummary:
    """Load a results.json into a RunSummary."""
    if path.is_dir():
        path = path / "results.json"
    if not path.exists():
        raise FileNotFoundError(f"results.json not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = {c["id"]: c for c in data.get("cases", [])}
    summary = data.get("summary", {})
    return RunSummary(
        run_id=data.get("run_id", path.parent.name),
        model=data.get("model", "?"),
        prompt_version=data.get("prompt_version", "?"),
        label=data.get("label", "?"),
        total=summary.get("total", len(cases)),
        correct=summary.get("correct", 0),
        errors=summary.get("errors", 0),
        accuracy=summary.get("accuracy", 0.0),
        by_category=summary.get("by_category", {}),
        cases=cases,
    )


def _case_pass(case: dict) -> bool:
    """A case passes if it has no error and actual == expected."""
    if case.get("error"):
        return False
    return case.get("pass", False) or case.get("actual") == case.get("expected")


def _aggregate(runs: list[RunSummary]) -> dict[str, int]:
    """For each case id, count how many runs passed it (0..len(runs))."""
    pass_counts: dict[str, int] = {}
    for run in runs:
        for cid, case in run.cases.items():
            pass_counts[cid] = pass_counts.get(cid, 0) + (1 if _case_pass(case) else 0)
    return pass_counts


def _classify(baseline_passes: int, candidate_passes: int) -> str:
    """Stability category for a single case across the run sets."""
    if baseline_passes <= 1 and candidate_passes >= 2:
        return "FIXED"
    if baseline_passes >= 2 and candidate_passes <= 1:
        return "REGRESSED"
    if baseline_passes >= 2 and candidate_passes >= 2:
        return "UNCHANGED_CORRECT"
    if baseline_passes <= 1 and candidate_passes <= 1:
        return "UNCHANGED_WRONG"
    return "UNSTABLE"


def _flip_direction(
    baseline_runs: list[RunSummary], candidate_runs: list[RunSummary], cid: str
) -> str | None:
    """Detect a NWM→WFM or WFM→NWM flip between baseline and candidate.

    Returns the most common actual decision in each set and reports the
    flip direction if they differ meaningfully. Used to surface
    NWM→WFM regressions (new false waiting-list items).
    """
    from collections import Counter

    baseline_actuals = Counter()
    candidate_actuals = Counter()
    for run in baseline_runs:
        if cid in run.cases:
            a = run.cases[cid].get("actual")
            if a:
                baseline_actuals[a] += 1
    for run in candidate_runs:
        if cid in run.cases:
            a = run.cases[cid].get("actual")
            if a:
                candidate_actuals[a] += 1
    if not baseline_actuals or not candidate_actuals:
        return None
    b = baseline_actuals.most_common(1)[0][0]
    c = candidate_actuals.most_common(1)[0][0]
    if b == c:
        return None
    return f"{_SHORT.get(b, b)}→{_SHORT.get(c, c)}"


def _overall_metrics(runs: list[RunSummary]) -> dict:
    """Aggregate overall accuracy / WFM recall / NWM recall / UNC accuracy
    across a set of runs (averaged)."""
    if not runs:
        return {}
    # Per-run confusion: count by expected/actual.
    total_wfm_recall = []
    total_nwm_recall = []
    total_unc_acc = []
    accs = []
    for run in runs:
        wfm_total = wfm_correct = 0
        nwm_total = nwm_correct = 0
        unc_total = unc_correct = 0
        for case in run.cases.values():
            exp = case.get("expected")
            act = case.get("actual")
            if exp == "waiting_for_me":
                wfm_total += 1
                if act == "waiting_for_me":
                    wfm_correct += 1
            elif exp == "not_waiting_for_me":
                nwm_total += 1
                if act == "not_waiting_for_me":
                    nwm_correct += 1
            elif exp == "uncertain":
                unc_total += 1
                if act == "uncertain":
                    unc_correct += 1
        if wfm_total:
            total_wfm_recall.append(wfm_correct / wfm_total)
        if nwm_total:
            total_nwm_recall.append(nwm_correct / nwm_total)
        if unc_total:
            total_unc_acc.append(unc_correct / unc_total)
        accs.append(run.accuracy)

    def avg(xs: list[float]) -> float:
        return round(sum(xs) / len(xs), 4) if xs else 0.0

    return {
        "accuracy_avg": avg(accs),
        "wfm_recall_avg": avg(total_wfm_recall),
        "nwm_recall_avg": avg(total_nwm_recall),
        "unc_accuracy_avg": avg(total_unc_acc),
    }


def _count_flips(
    baseline_runs: list[RunSummary], candidate_runs: list[RunSummary]
) -> dict[str, int]:
    """Count WFM→NWM and NWM→WFM flips (most-common actual per case)."""
    all_cids = set()
    for run in baseline_runs:
        all_cids.update(run.cases)
    for run in candidate_runs:
        all_cids.update(run.cases)
    wfm_to_nwm = 0
    nwm_to_wfm = 0
    for cid in all_cids:
        flip = _flip_direction(baseline_runs, candidate_runs, cid)
        if flip == "WFM→NWM":
            wfm_to_nwm += 1
        elif flip == "NWM→WFM":
            nwm_to_wfm += 1
    return {"wfm_to_nwm": wfm_to_nwm, "nwm_to_wfm": nwm_to_wfm}


def _invalid_json_count(runs: list[RunSummary]) -> int:
    """Count cases with errors (invalid JSON / API errors) across all runs."""
    return sum(
        1
        for run in runs
        for case in run.cases.values()
        if case.get("error")
    )


def _avg_latency(runs: list[RunSummary]) -> float:
    """Average per-case latency across all runs."""
    latencies = [
        case.get("latency_ms")
        for run in runs
        for case in run.cases.values()
        if case.get("latency_ms") is not None
    ]
    if not latencies:
        return 0.0
    return round(sum(latencies) / len(latencies), 1)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate 3-vs-3 eval runs and report per-case stability."
    )
    parser.add_argument(
        "--baseline",
        nargs="+",
        required=True,
        help="Paths to baseline run directories (or results.json files).",
    )
    parser.add_argument(
        "--candidate",
        nargs="+",
        required=True,
        help="Paths to candidate run directories (or results.json files).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a markdown report.",
    )
    args = parser.parse_args()

    baseline_runs = [_load_run(Path(p)) for p in args.baseline]
    candidate_runs = [_load_run(Path(p)) for p in args.candidate]

    if len(baseline_runs) != len(candidate_runs):
        print(
            f"WARNING: baseline has {len(baseline_runs)} runs but candidate "
            f"has {len(candidate_runs)}; proceeding anyway.",
            file=sys.stderr,
        )

    baseline_pass = _aggregate(baseline_runs)
    candidate_pass = _aggregate(candidate_runs)

    all_cids = sorted(set(baseline_pass) | set(candidate_pass))

    # Per-case classification.
    rows: list[dict] = []
    category_counts: dict[str, int] = {}
    nwm_to_wfm_cases: list[str] = []
    for cid in all_cids:
        b = baseline_pass.get(cid, 0)
        c = candidate_pass.get(cid, 0)
        category = _classify(b, c)
        category_counts[category] = category_counts.get(category, 0) + 1
        flip = _flip_direction(baseline_runs, candidate_runs, cid)
        if flip == "NWM→WFM":
            nwm_to_wfm_cases.append(cid)
        # Pull expected + description from the first run that has it.
        desc = ""
        expected = ""
        for run in baseline_runs + candidate_runs:
            if cid in run.cases:
                desc = run.cases[cid].get("description", "")
                expected = run.cases[cid].get("expected", "")
                break
        rows.append(
            {
                "case_id": cid,
                "description": desc,
                "expected": expected,
                "baseline_passes": b,
                "candidate_passes": c,
                "category": category,
                "flip": flip,
            }
        )

    baseline_metrics = _overall_metrics(baseline_runs)
    candidate_metrics = _overall_metrics(candidate_runs)
    flips = _count_flips(baseline_runs, candidate_runs)

    report = {
        "baseline": {
            "runs": [r.run_id for r in baseline_runs],
            "model": baseline_runs[0].model if baseline_runs else "?",
            "prompt_version": baseline_runs[0].prompt_version if baseline_runs else "?",
            "metrics": baseline_metrics,
            "invalid_json_errors": _invalid_json_count(baseline_runs),
            "avg_latency_ms": _avg_latency(baseline_runs),
        },
        "candidate": {
            "runs": [r.run_id for r in candidate_runs],
            "model": candidate_runs[0].model if candidate_runs else "?",
            "prompt_version": candidate_runs[0].prompt_version if candidate_runs else "?",
            "metrics": candidate_metrics,
            "invalid_json_errors": _invalid_json_count(candidate_runs),
            "avg_latency_ms": _avg_latency(candidate_runs),
        },
        "flips": flips,
        "nwm_to_wfm_cases": nwm_to_wfm_cases,
        "category_counts": category_counts,
        "cases": rows,
    }

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    # --- Markdown report -----------------------------------------------------
    b_pv = baseline_runs[0].prompt_version if baseline_runs else "?"
    c_pv = candidate_runs[0].prompt_version if candidate_runs else "?"
    b_model = baseline_runs[0].model if baseline_runs else "?"
    c_model = candidate_runs[0].model if candidate_runs else "?"

    print(f"# Eval Diff: {b_pv} → {c_pv}\n")
    print(f"- Baseline runs: {len(baseline_runs)} (model={b_model}, prompt={b_pv})")
    print(f"- Candidate runs: {len(candidate_runs)} (model={c_model}, prompt={c_pv})")
    print()

    print("## Overall Metrics\n")
    print("| Metric | Baseline | Candidate | Delta |")
    print("|---|---|---|---|")
    for key in ["accuracy_avg", "wfm_recall_avg", "nwm_recall_avg", "unc_accuracy_avg"]:
        b = baseline_metrics.get(key, 0.0)
        c = candidate_metrics.get(key, 0.0)
        delta = round(c - b, 4)
        sign = "+" if delta >= 0 else ""
        print(f"| {key} | {b} | {c} | {sign}{delta} |")
    print(
        f"| invalid_json_errors | {report['baseline']['invalid_json_errors']} "
        f"| {report['candidate']['invalid_json_errors']} | "
        f"{report['candidate']['invalid_json_errors'] - report['baseline']['invalid_json_errors']:+d} |"
    )
    print(
        f"| avg_latency_ms | {report['baseline']['avg_latency_ms']} "
        f"| {report['candidate']['avg_latency_ms']} | "
        f"{round(report['candidate']['avg_latency_ms'] - report['baseline']['avg_latency_ms'], 1):+} |"
    )
    print()

    print("## Flips\n")
    print(f"- WFM→NWM: {flips['wfm_to_nwm']}")
    print(f"- **NWM→WFM (new false waiting-list items): {flips['nwm_to_wfm']}**")
    if nwm_to_wfm_cases:
        print(f"  - Cases: {', '.join(nwm_to_wfm_cases)}")
    print()

    print("## Category Counts\n")
    for cat in ["FIXED", "REGRESSED", "UNCHANGED_CORRECT", "UNCHANGED_WRONG", "UNSTABLE"]:
        print(f"- {cat}: {category_counts.get(cat, 0)}")
    print()

    print("## Per-Case Detail\n")
    print("| Case | Baseline | Candidate | Category | Flip | Expected | Description |")
    print("|---|---|---|---|---|---|---|")
    # Sort: REGRESSED first, then FIXED, then UNSTABLE, then the rest.
    cat_order = {
        "REGRESSED": 0,
        "FIXED": 1,
        "UNSTABLE": 2,
        "UNCHANGED_WRONG": 3,
        "UNCHANGED_CORRECT": 4,
    }
    rows_sorted = sorted(
        rows,
        key=lambda r: (cat_order.get(r["category"], 9), r["case_id"]),
    )
    for r in rows_sorted:
        print(
            f"| {r['case_id']} | {r['baseline_passes']}/{len(baseline_runs)} "
            f"| {r['candidate_passes']}/{len(candidate_runs)} "
            f"| {r['category']} | {r['flip'] or ''} "
            f"| {_SHORT.get(r['expected'], r['expected'])} | {r['description'][:60]} |"
        )

    # --- Regression gate -----------------------------------------------------
    print("\n## Regression Gate\n")
    gate_pass = True
    if flips["nwm_to_wfm"] > 0:
        gate_pass = False
        print(
            f"FAIL: {flips['nwm_to_wfm']} stable NWM→WFM regression(s) detected: "
            f"{', '.join(nwm_to_wfm_cases)}"
        )
    if category_counts.get("REGRESSED", 0) > 0:
        gate_pass = False
        print(
            f"FAIL: {category_counts['REGRESSED']} case(s) regressed (baseline ≥2 passes → candidate ≤1)."
        )
    if gate_pass:
        print("PASS: no stable NWM→WFM regressions and no stable regressions.")
    return 0 if gate_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
