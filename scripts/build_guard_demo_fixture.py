"""Freeze selected Guard eval results into a deterministic demo fixture."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from echo_v2.services.guard_alert_policy import (
    ChildContext,
    ConversationContext,
    DefaultAlertPolicy,
)
from echo_v2.services.guard_analyzer import GuardAnalysis
from echo_v2.services.guard_decision_policy import DefaultDecisionPolicy
from echo_v2.services.guard_signal_ledger import GuardSignalLedger

DEFAULT_OUTPUT = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "echo_v2"
    / "app"
    / "static"
    / "guard_demo_scenarios.json"
)

SCENARIO_COPY: dict[str, dict[str, str]] = {
    "unknown_contact_escalation_001": {
        "title": "A stranger becomes a pattern",
        "subtitle": "Echo Guard stays quiet until ordinary messages form a concerning story.",
        "moment": "Danger emerges one signal at a time.",
    },
    "suspicious_contact_002_known_activity_coordinator_negative": {
        "title": "The same words can be safe",
        "subtitle": "A known school coordinator discusses time and location without triggering an alert.",
        "moment": "Context matters more than keywords.",
    },
    "teasing_vs_bullying_002_positive": {
        "title": "When a group piles on",
        "subtitle": "One comment becomes repeated targeting, then exclusion by several people.",
        "moment": "Guard understands group dynamics, not just harsh words.",
    },
    "distress_001_hopelessness_and_help_request_positive": {
        "title": "When the child asks for help",
        "subtitle": "The monitored child’s own words move from distress toward an explicit need for support.",
        "moment": "Not every urgent pattern comes from another person.",
    },
    "child_sexual_exploitation_002_intimate_image_sextortion": {
        "title": "Pressure becomes blackmail",
        "subtitle": "A request, a refusal, coercion, and a threat to share build to an urgent pattern.",
        "moment": "The final snapshot is the climax: notify the parent now.",
    },
}

SCENARIO_ORDER = tuple(SCENARIO_COPY)


def _prefix(messages: list[dict[str, str]], after_message_id: str) -> list[dict[str, str]]:
    for index, message in enumerate(messages):
        if message["id"] == after_message_id:
            return messages[: index + 1]
    raise ValueError(f"snapshot references unknown message {after_message_id!r}")


def _analysis(snapshot: dict[str, Any]) -> GuardAnalysis:
    actual = snapshot["actual"]
    return GuardAnalysis(
        signals=tuple(actual["signals"]),
        categories=tuple(actual["categories"]),
        evidence_message_ids=tuple(actual["evidence_message_ids"]),
        confidence=float(actual["confidence"]),
        reason=str(actual["reason"]),
    )


def _cumulative_evidence(state: Any) -> list[str]:
    return list(
        dict.fromkeys(
            message_id
            for evidence_ids in state.evidence_by_signal.values()
            for message_id in evidence_ids
        )
    )


def _build_snapshot(
    *,
    case: dict[str, Any],
    source_snapshot: dict[str, Any],
    ledger: GuardSignalLedger,
) -> dict[str, Any]:
    analysis = _analysis(source_snapshot)
    previous_state = ledger.state()
    state = ledger.update(analysis)
    prefix = _prefix(case["messages"], source_snapshot["after_message_id"])
    prefix_ids = {message["id"] for message in prefix}
    raw_evidence = list(analysis.evidence_message_ids)
    cumulative_evidence = _cumulative_evidence(state)
    if not set(raw_evidence) <= prefix_ids or not set(cumulative_evidence) <= prefix_ids:
        raise ValueError(
            f"{case['case_id']} @ {source_snapshot['after_message_id']}: evidence is outside prefix"
        )

    cumulative_signals = list(state.active_signals)
    current_signals = list(analysis.signals)
    new_signals = [signal for signal in current_signals if signal not in previous_state.active_signals]
    remembered_signals = [signal for signal in cumulative_signals if signal not in new_signals]
    cumulative_categories = list(state.active_categories)
    decision = DefaultDecisionPolicy().decide(
        signals=state.active_signals,
        categories=state.active_categories,
        signal_state=state,
    ).value
    should_alert = DefaultAlertPolicy().should_alert(
        analysis=analysis,
        child_context=ChildContext(
            known_contact=case.get("context", {}).get("contact_is_known")
            if isinstance(case.get("context", {}).get("contact_is_known"), bool)
            else None
        ),
        conversation_context=ConversationContext(),
        signal_state=state,
    )
    expected = source_snapshot.get("expected", {})
    return {
        "after_message_id": source_snapshot["after_message_id"],
        "prefix_length": len(prefix),
        "new_message_id": source_snapshot["after_message_id"],
        "evaluation_pass": bool(source_snapshot.get("pass", {}).get("analyzer", False)),
        "expected": expected,
        "raw": {
            "signals": current_signals,
            "categories": list(analysis.categories),
            "evidence_message_ids": raw_evidence,
            "confidence": analysis.confidence,
            "reason": analysis.reason,
            "decision": DefaultDecisionPolicy().decide(
                signals=analysis.signals,
                categories=analysis.categories,
            ).value,
        },
        "cumulative": {
            "signals": cumulative_signals,
            "categories": cumulative_categories,
            "evidence_message_ids": cumulative_evidence,
        },
        "new_signals": new_signals,
        "remembered_signals": remembered_signals,
        "decision": decision,
        "should_alert": should_alert,
        "alert_label": "Notify parent" if should_alert else "Not yet",
    }


def build_fixture(payload: dict[str, Any]) -> dict[str, Any]:
    cases_by_id = {case["case_id"]: case for case in payload.get("cases", [])}
    missing = [case_id for case_id in SCENARIO_ORDER if case_id not in cases_by_id]
    if missing:
        raise ValueError(f"source run is missing selected cases: {', '.join(missing)}")

    scenarios: list[dict[str, Any]] = []
    for case_id in SCENARIO_ORDER:
        case = cases_by_id[case_id]
        copy = SCENARIO_COPY[case_id]
        ledger = GuardSignalLedger()
        source_snapshots = case.get("snapshots", [])
        snapshots = [
            _build_snapshot(case=case, source_snapshot=snapshot, ledger=ledger)
            for snapshot in source_snapshots
        ]
        if not snapshots:
            raise ValueError(f"selected case has no snapshots: {case_id}")
        scenarios.append(
            {
                "id": case_id,
                "family": case["family"],
                "title": copy["title"],
                "subtitle": copy["subtitle"],
                "moment": copy["moment"],
                "source": case.get("source", ""),
                "notes": case.get("notes", ""),
                "context": case.get("context", {}),
                "messages": case["messages"],
                "snapshots": snapshots,
            }
        )

    return {
        "version": 1,
        "kind": "echo_guard_demo_replay",
        "source": {
            "run_id": payload.get("run_id", ""),
            "label": payload.get("label", ""),
            "model": payload.get("model", ""),
            "prompt_version": payload.get("prompt_version", ""),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "scenarios": scenarios,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="Guard eval results.json")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    fixture = build_fixture(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(fixture['scenarios'])} scenarios to {args.output}")


if __name__ == "__main__":
    main()
