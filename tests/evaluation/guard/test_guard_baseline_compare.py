"""Compare Baseline B with the existing Guard path on the same Gold prefixes."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from echo_v2.domain.guard_simple import SimpleConversation, SimpleConversationMessage
from echo_v2.services.guard_alert_policy import (
    AlertKind,
    ChildContext,
    ConversationContext,
    DefaultAlertPolicy,
)
from echo_v2.services.guard_analyzer import (
    AnalysisError,
    GuardAnalysisInput,
    LLMGuardAnalyzer,
)
from echo_v2.services.guard_decision_policy import DefaultDecisionPolicy
from echo_v2.services.guard_eval_gold import GoldScenario, load_guard_gold
from echo_v2.services.guard_signal_ledger import GuardSignalLedger
from echo_v2.services.guard_simple_analyzer import SimpleSafetyAnalyzer

pytestmark = [
    pytest.mark.eval_guard,
    pytest.mark.skipif(
        not os.environ.get("OPENAI_API_KEY"),
        reason="OPENAI_API_KEY not set — eval requires real API access",
    ),
]

_RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


def _simple_conversation(scenario: GoldScenario, prefix_length: int) -> SimpleConversation:
    messages = tuple(
        SimpleConversationMessage(
            id=message.id,
            sender=message.sender,
            text=message.text,
        )
        for message in scenario.messages[:prefix_length]
    )
    return SimpleConversation(
        child_user_id="gold-child",
        chat_id=scenario.scenario_id,
        chat_type=(
            "group" if scenario.context.get("conversation_type") == "group" else "private"
        ),
        connection_id="gold-connection",
        timezone="Asia/Jerusalem",
        window_start=NOW.replace(hour=0),
        window_end=NOW,
        monitored_child_role="child",
        messages=messages,
    )


def _prefix_ids(scenario: GoldScenario, prefix_length: int) -> set[str]:
    return {message.id for message in scenario.messages[:prefix_length]}


async def _run_comparison() -> list[dict[str, Any]]:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
    legacy = LLMGuardAnalyzer(
        client=client,
        model=os.environ.get("GUARD_LLM_MODEL", os.environ.get("LLM_MODEL_NAME", "gpt-4.1")),
    )
    simple = SimpleSafetyAnalyzer(
        client=client,
        model=os.environ.get(
            "GUARD_SIMPLE_LLM_MODEL",
            os.environ.get("GUARD_LLM_MODEL", os.environ.get("LLM_MODEL_NAME", "gpt-4.1")),
        ),
    )
    rows: list[dict[str, Any]] = []
    gold = load_guard_gold()
    requested_ids = {
        value.strip()
        for value in os.environ.get("GUARD_COMPARE_CASE_IDS", "").split(",")
        if value.strip()
    }
    scenarios = tuple(
        scenario
        for scenario in gold.scenarios
        if not requested_ids or scenario.scenario_id in requested_ids
    )
    if requested_ids and len(scenarios) != len(requested_ids):
        known = {scenario.scenario_id for scenario in gold.scenarios}
        raise ValueError(f"unknown GUARD_COMPARE_CASE_IDS: {sorted(requested_ids - known)}")

    for scenario in scenarios:
        ledger = GuardSignalLedger()
        prior_alert_kind = AlertKind.NONE
        for checkpoint in scenario.checkpoints:
            legacy_conversation = GuardAnalysisInput(
                child_id="gold-child",
                chat_id=scenario.scenario_id,
                messages=scenario.messages[: checkpoint.prefix_length],
                context=dict(scenario.context),
            )
            legacy_started = time.perf_counter()
            legacy_error: str | None = None
            try:
                legacy_analysis, _legacy_raw = await legacy.analyze_with_raw(legacy_conversation)
            except AnalysisError as exc:
                legacy_analysis = None
                legacy_error = str(exc)
            legacy_latency_ms = (time.perf_counter() - legacy_started) * 1000
            legacy_state = ledger.update(legacy_analysis) if legacy_analysis is not None else ledger.state()
            legacy_decision = (
                DefaultDecisionPolicy().decide(
                    signals=legacy_analysis.signals,
                    categories=legacy_analysis.categories,
                    signal_state=legacy_state,
                )
                if legacy_analysis is not None
                else None
            )
            known_contact = scenario.context.get("contact_is_known")
            legacy_alert = (
                DefaultAlertPolicy().evaluate(
                    analysis=legacy_analysis,
                    child_context=ChildContext(
                        known_contact=known_contact if isinstance(known_contact, bool) else None
                    ),
                    conversation_context=ConversationContext(prior_alert_kind=prior_alert_kind),
                    signal_state=legacy_state,
                )
                if legacy_analysis is not None
                else None
            )
            if legacy_alert is not None and legacy_alert.event:
                prior_alert_kind = legacy_alert.kind

            simple_started = time.perf_counter()
            simple_run = await simple.analyze_with_raw(
                _simple_conversation(scenario, checkpoint.prefix_length)
            )
            simple_latency_ms = (time.perf_counter() - simple_started) * 1000
            simple_assessment = simple_run.assessment
            prefix_ids = _prefix_ids(scenario, checkpoint.prefix_length)
            row = {
                "scenario_id": scenario.scenario_id,
                "title": scenario.title,
                "after_message_id": checkpoint.after_message_id,
                "prefix_length": checkpoint.prefix_length,
                "gold": {
                    "legacy_decision": checkpoint.decision,
                    "legacy_alert_event": checkpoint.alert.event,
                    "legacy_alert_kind": checkpoint.alert.kind,
                },
                "legacy": {
                    "signals": list(legacy_analysis.signals) if legacy_analysis is not None else [],
                    "categories": list(legacy_analysis.categories) if legacy_analysis is not None else [],
                    "active_signals": list(legacy_state.active_signals),
                    "active_categories": list(legacy_state.active_categories),
                    "evidence_message_ids": (
                        list(legacy_analysis.evidence_message_ids)
                        if legacy_analysis is not None
                        else []
                    ),
                    "decision": legacy_decision.value if legacy_decision is not None else None,
                    "alert_event": legacy_alert.event if legacy_alert is not None else False,
                    "alert_kind": legacy_alert.kind.value if legacy_alert is not None else None,
                    "alert_reason": legacy_alert.reason if legacy_alert is not None else None,
                    "error": legacy_error,
                    "latency_ms": round(legacy_latency_ms, 1),
                    "token_usage": None,
                },
                "baseline_b": {
                    "status": simple_assessment.status.value,
                    "child_role": simple_assessment.child_role.value,
                    "summary": simple_assessment.summary,
                    "reason": simple_assessment.reason,
                    "evidence_message_ids": list(simple_assessment.evidence_message_ids),
                    "evidence_within_prefix": set(simple_assessment.evidence_message_ids) <= prefix_ids,
                    "latency_ms": round(simple_latency_ms, 1),
                    "token_usage": {
                        "prompt_tokens": simple_run.prompt_tokens,
                        "completion_tokens": simple_run.completion_tokens,
                        "total_tokens": simple_run.total_tokens,
                    },
                },
            }
            rows.append(row)
            print(
                f"{scenario.scenario_id} @ {checkpoint.after_message_id}: "
                f"legacy={legacy_decision.value if legacy_decision is not None else 'error'}/"
                f"{legacy_alert.kind.value if legacy_alert is not None else 'none'} "
                f"baseline_b={simple_assessment.status.value}/{simple_assessment.child_role.value} "
                f"legacy_evidence={list(legacy_analysis.evidence_message_ids) if legacy_analysis is not None else []} "
                f"baseline_b_evidence={list(simple_assessment.evidence_message_ids)}"
            )
    return rows


@pytest.mark.asyncio
async def test_compare_baseline_b_with_existing_path_on_shared_gold_prefixes() -> None:
    rows = await _run_comparison()
    assert len(rows) > 0
    assert all(row["baseline_b"]["evidence_within_prefix"] for row in rows)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_dir = _RESULTS_DIR / f"{run_id}_guard_baseline_compare"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "comparison.json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    summary = [
        "# Baseline A / Baseline B Gold prefix comparison",
        "",
        "| Scenario | Prefix | Legacy | Baseline B | Legacy evidence | Baseline B evidence |",
        "|---|---:|---|---|---|---|",
    ]
    for row in rows:
        summary.append(
            f"| {row['scenario_id']} | {row['after_message_id']} | "
            f"{row['legacy']['decision']}/{row['legacy']['alert_kind']} | "
            f"{row['baseline_b']['status']}/{row['baseline_b']['child_role']} | "
            f"{row['legacy']['evidence_message_ids']} | "
            f"{row['baseline_b']['evidence_message_ids']} |"
        )
    (output_dir / "comparison.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
