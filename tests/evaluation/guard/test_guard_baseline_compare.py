"""Direct Baseline B validation against canonical Guard Gold expectations."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from echo_v2.domain.guard_simple import SimpleConversation, SimpleConversationMessage
from echo_v2.services.guard_eval_gold import GoldScenario, load_guard_gold
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
        child_name=str(scenario.context.get("child_name", "the monitored child")),
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


def _expected_parent_should_know(
    scenario: GoldScenario, after_message_id: str, decision: str
) -> bool:
    """Translate each Gold stage into the parent-attention question."""
    early_non_actionable = {
        "gold_parent_01_he_knows_where_she_waits": {"m2", "m3", "m9"},
        "gold_parent_02_he_said_stop_they_kept_going": {"m2", "m4"},
        "gold_parent_03_bring_200_tomorrow": {"m3"},
        "gold_parent_04_please_delete_the_video": {"m2"},
        "gold_parent_05_if_you_trusted_me": {"m2", "m4", "m7"},
        "gold_parent_06_i_dont_want_to_be_alone_tonight": {"m2", "m5", "m7"},
    }
    if after_message_id in early_non_actionable.get(scenario.scenario_id, set()):
        return False
    if scenario.scenario_id == "gold_parent_03_bring_200_tomorrow" and after_message_id == "m3":
        return False
    return decision in {"concerning", "urgent"} or (
        scenario.scenario_id in {
            "gold_parent_03_bring_200_tomorrow",
            "gold_parent_04_please_delete_the_video",
        }
        and after_message_id in {"m6"}
    )


async def _run_validation() -> list[dict[str, Any]]:
    from openai import AsyncOpenAI

    analyzer = SimpleSafetyAnalyzer(
        client=AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", "")),
        model=os.environ.get(
            "GUARD_SIMPLE_LLM_MODEL",
            os.environ.get("LLM_MODEL_NAME", "gpt-4.1"),
        ),
    )
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

    rows: list[dict[str, Any]] = []
    for scenario in scenarios:
        for checkpoint in scenario.checkpoints:
            run = await analyzer.analyze_with_raw(
                _simple_conversation(scenario, checkpoint.prefix_length)
            )
            assessment = run.assessment
            expected = _expected_parent_should_know(
                scenario, checkpoint.after_message_id, checkpoint.decision
            )
            prefix_ids = _prefix_ids(scenario, checkpoint.prefix_length)
            evidence_within_prefix = set(assessment.evidence_message_ids) <= prefix_ids
            passed = (
                assessment.parent_should_know is expected
                and evidence_within_prefix
                and (
                    assessment.parent_should_know is not True
                    or bool(assessment.evidence_message_ids)
                )
            )
            row = {
                "scenario_id": scenario.scenario_id,
                "title": scenario.title,
                "after_message_id": checkpoint.after_message_id,
                "prefix_length": checkpoint.prefix_length,
                "expected_parent_should_know": expected,
                "actual_parent_should_know": assessment.parent_should_know,
                "child_role": assessment.child_role.value,
                "summary": assessment.summary,
                "reason": assessment.reason,
                "evidence_message_ids": list(assessment.evidence_message_ids),
                "evidence_within_prefix": evidence_within_prefix,
                "latency_ms": round((run.latency_ms or 0.0), 1),
                "token_usage": {
                    "prompt_tokens": run.prompt_tokens,
                    "completion_tokens": run.completion_tokens,
                    "total_tokens": run.total_tokens,
                },
                "pass": passed,
            }
            rows.append(row)
            print(
                f"{scenario.scenario_id} @ {checkpoint.after_message_id}: "
                f"expected={expected} actual={assessment.parent_should_know} "
                f"role={assessment.child_role.value} "
                f"evidence={list(assessment.evidence_message_ids)} "
                f"{'PASS' if passed else 'FAIL'}"
            )
    return rows


@pytest.mark.asyncio
async def test_baseline_b_reports_canonical_gold_prefixes() -> None:
    rows = await _run_validation()
    assert rows
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_dir = _RESULTS_DIR / f"{run_id}_guard_baseline_expected"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "results.json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    lines = [
        "# Baseline B canonical Gold validation",
        "",
        "| Scenario | Prefix | Expected | Actual | Role | Evidence | Result |",
        "|---|---|---:|---:|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['scenario_id']} | {row['after_message_id']} | "
            f"{row['expected_parent_should_know']} | "
            f"{row['actual_parent_should_know']} | "
            f"{row['child_role']} | {row['evidence_message_ids']} | "
            f"{'PASS' if row['pass'] else 'FAIL'} |"
        )
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
