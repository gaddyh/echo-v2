"""Isolated cumulative Guard pipeline for the Live Compare debugger."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, cast

from echo_v2.services.guard_alert_policy import (
    AlertKind,
    ChildContext,
    ConversationContext,
    DefaultAlertPolicy,
)
from echo_v2.services.guard_analyzer import (
    GuardAnalysis,
    GuardAnalysisInput,
    GuardAnalyzer,
)
from echo_v2.services.guard_decision_policy import DefaultDecisionPolicy
from echo_v2.services.guard_eval_gold import GoldCheckpoint, GoldScenario
from echo_v2.services.guard_schedule_policy import (
    GuardConversationState,
    GuardSchedulePolicy,
    GuardSchedulingContext,
)
from echo_v2.services.guard_signal_ledger import GuardSignalLedger, GuardSignalState
from echo_v2.services.guard_taxonomy import GuardDecision


class _AnalyzerWithRaw(Protocol):
    async def analyze_with_raw(
        self, conversation: GuardAnalysisInput
    ) -> tuple[GuardAnalysis, str]: ...


@dataclass(frozen=True)
class LiveSnapshotExecution:
    ordinal: int
    after_message_id: str
    expected: dict[str, Any]
    actual: dict[str, Any]
    diff: dict[str, Any]
    has_difference: bool
    failure: bool


class GuardLiveCompareRunner:
    """Run the production Guard policies without production persistence or sends."""

    def __init__(
        self,
        *,
        analyzer: GuardAnalyzer,
        model: str,
        prompt_version: str,
        analyzer_version: str,
        taxonomy_version: str,
        schedule_policy: GuardSchedulePolicy | None = None,
    ) -> None:
        self._analyzer = analyzer
        self.model = model
        self.prompt_version = prompt_version
        self.analyzer_version = analyzer_version
        self.taxonomy_version = taxonomy_version
        self._schedule = schedule_policy or GuardSchedulePolicy()

    async def run(
        self,
        scenario: GoldScenario,
        on_snapshot: Callable[[LiveSnapshotExecution], Awaitable[None]] | None = None,
    ) -> tuple[LiveSnapshotExecution, ...]:
        ledger = GuardSignalLedger()
        prior_alert_kind = AlertKind.NONE
        previous_schedule = None
        executions: list[LiveSnapshotExecution] = []
        positions = {message.id: index for index, message in enumerate(scenario.messages)}
        for checkpoint in scenario.checkpoints:
            prefix = scenario.messages[: checkpoint.prefix_length]
            conversation = GuardAnalysisInput(
                child_id="live-compare-child",
                chat_id=scenario.scenario_id,
                messages=prefix,
                context=dict(scenario.context),
            )
            analysis, raw_response = await self._analyze(conversation)
            state = ledger.update(analysis)
            decision = DefaultDecisionPolicy().decide(
                signals=state.active_signals,
                categories=state.active_categories,
                signal_state=state,
            )
            known_contact = scenario.context.get("contact_is_known")
            alert = DefaultAlertPolicy().evaluate(
                analysis=analysis,
                child_context=ChildContext(
                    known_contact=known_contact if isinstance(known_contact, bool) else None
                ),
                conversation_context=ConversationContext(prior_alert_kind=prior_alert_kind),
                signal_state=state,
            )
            schedule = self._simulate_schedule(
                checkpoint=checkpoint,
                decision=decision,
                state=state,
                previous_schedule=previous_schedule,
            )
            previous_schedule = schedule
            expected = _expected_dict(checkpoint)
            actual = _actual_dict(
                conversation=conversation,
                analysis=analysis,
                raw_response=raw_response,
                state=state,
                decision=decision,
                alert=alert,
                schedule=schedule,
            )
            diff = _compare_checkpoint(
                checkpoint=checkpoint,
                positions=positions,
                actual=actual,
            )
            has_difference = bool(diff["difference"])
            failure = bool(diff["failure"])
            execution = LiveSnapshotExecution(
                ordinal=checkpoint.ordinal,
                after_message_id=checkpoint.after_message_id,
                expected=expected,
                actual=actual,
                diff=diff,
                has_difference=has_difference,
                failure=failure,
            )
            executions.append(execution)
            if on_snapshot is not None:
                await on_snapshot(execution)
            if alert.event:
                prior_alert_kind = alert.kind
        return tuple(executions)

    async def _analyze(
        self, conversation: GuardAnalysisInput
    ) -> tuple[GuardAnalysis, str]:
        candidate = getattr(self._analyzer, "analyze_with_raw", None)
        if callable(candidate):
            analyzer = cast(_AnalyzerWithRaw, self._analyzer)
            return await analyzer.analyze_with_raw(conversation)
        return await self._analyzer.analyze(conversation), ""

    def _simulate_schedule(
        self,
        *,
        checkpoint: GoldCheckpoint,
        decision: GuardDecision,
        state: GuardSignalState,
        previous_schedule: Any,
    ) -> dict[str, Any]:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        new_message_at = base + timedelta(minutes=checkpoint.ordinal)
        schedule = self._schedule.on_message(
            GuardSchedulingContext(
                state=GuardConversationState(
                    decision=decision,
                    active_signals=state.active_signals,
                    active_categories=state.active_categories,
                ),
                is_group=False,
                pending_since=None,
                last_analysis_at=None,
                next_analysis_at=(
                    datetime.fromisoformat(previous_schedule["analyze_at"])
                    if previous_schedule
                    else None
                ),
                new_message_at=new_message_at,
            )
        )
        return {
            "mode": "ordinal_simulation",
            "ordinal": checkpoint.ordinal,
            "new_message_at": new_message_at.isoformat(),
            "analyze_at": schedule.analyze_at.isoformat(),
            "reason": schedule.reason,
            "timing_claim": "checkpoint ordering only; not wall-clock latency",
        }


def _expected_dict(checkpoint: GoldCheckpoint) -> dict[str, Any]:
    return {
        "analyzer": {
            "required_categories": list(checkpoint.analyzer.required_categories),
            "required_signals": list(checkpoint.analyzer.required_signals),
            "required_signal_any_of": list(checkpoint.analyzer.required_signal_any_of),
            "forbidden_categories": list(checkpoint.analyzer.forbidden_categories),
            "forbidden_signals": list(checkpoint.analyzer.forbidden_signals),
            "required_evidence_message_ids": list(
                checkpoint.analyzer.required_evidence_message_ids
            ),
            "expect_clean": checkpoint.analyzer.expect_clean,
        },
        "ledger": {
            "required_active_categories": list(checkpoint.ledger.required_active_categories),
            "required_active_signals": list(checkpoint.ledger.required_active_signals),
            "required_evidence_message_ids": list(
                checkpoint.ledger.required_evidence_message_ids
            ),
        },
        "decision": checkpoint.decision,
        "alert": {
            "should_alert": checkpoint.alert.should_alert,
            "event": checkpoint.alert.event,
            "kind": checkpoint.alert.kind,
            "reason": checkpoint.alert.reason,
        },
        "timing": checkpoint.alert_window,
    }


def _actual_dict(
    *,
    conversation: GuardAnalysisInput,
    analysis: GuardAnalysis,
    raw_response: str,
    state: GuardSignalState,
    decision: GuardDecision,
    alert: Any,
    schedule: dict[str, Any],
) -> dict[str, Any]:
    return {
        "analyzer_input": {
            "child_id": conversation.child_id,
            "chat_id": conversation.chat_id,
            "context": conversation.context,
            "messages": [
                {"id": message.id, "sender": message.sender, "text": message.text}
                for message in conversation.messages
            ],
        },
        "analyzer": {
            "signals": list(analysis.signals),
            "categories": list(analysis.categories),
            "evidence_message_ids": list(analysis.evidence_message_ids),
            "confidence": analysis.confidence,
            "reason": analysis.reason,
            "model": analysis.model,
            "prompt_version": analysis.prompt_version,
            "analyzer_version": analysis.analyzer_version,
            "taxonomy_version": analysis.taxonomy_version,
            "raw_response": raw_response,
        },
        "ledger": {
            "active_signals": list(state.active_signals),
            "active_categories": list(state.active_categories),
            "evidence_by_signal": {
                signal: list(ids) for signal, ids in state.evidence_by_signal.items()
            },
        },
        "decision": decision.value,
        "alert": {
            "should_alert": alert.should_alert,
            "event": alert.event,
            "kind": alert.kind.value,
            "reason": alert.reason,
        },
        "schedule": schedule,
    }


def _compare_checkpoint(
    *,
    checkpoint: GoldCheckpoint,
    positions: dict[str, int],
    actual: dict[str, Any],
) -> dict[str, Any]:
    analyzer = actual["analyzer"]
    ledger = actual["ledger"]
    alert = actual["alert"]
    analyzer_diff = {
        "missing_categories": _missing(checkpoint.analyzer.required_categories, analyzer["categories"]),
        "missing_signals": _missing(checkpoint.analyzer.required_signals, analyzer["signals"]),
        "missing_any_of": (
            list(checkpoint.analyzer.required_signal_any_of)
            if checkpoint.analyzer.required_signal_any_of
            and not set(checkpoint.analyzer.required_signal_any_of) & set(analyzer["signals"])
            else []
        ),
        "forbidden_categories": _present(checkpoint.analyzer.forbidden_categories, analyzer["categories"]),
        "forbidden_signals": _present(checkpoint.analyzer.forbidden_signals, analyzer["signals"]),
        "missing_evidence": _missing(
            checkpoint.analyzer.required_evidence_message_ids,
            analyzer["evidence_message_ids"],
        ),
        "expected_clean_but_not_clean": checkpoint.analyzer.expect_clean
        and bool(analyzer["signals"] or analyzer["categories"] or analyzer["evidence_message_ids"]),
    }
    ledger_evidence = {
        message_id
        for ids in ledger["evidence_by_signal"].values()
        for message_id in ids
    }
    ledger_diff = {
        "missing_active_categories": _missing(
            checkpoint.ledger.required_active_categories, ledger["active_categories"]
        ),
        "missing_active_signals": _missing(
            checkpoint.ledger.required_active_signals, ledger["active_signals"]
        ),
        "missing_evidence": _missing(
            checkpoint.ledger.required_evidence_message_ids, ledger_evidence
        ),
    }
    decision_diff = {"expected": checkpoint.decision, "actual": actual["decision"]}
    decision_mismatch = actual["decision"] != checkpoint.decision
    alert_diff = {
        "should_alert": {
            "expected": checkpoint.alert.should_alert,
            "actual": alert["should_alert"],
        },
        "event": {"expected": checkpoint.alert.event, "actual": alert["event"]},
        "kind": {"expected": checkpoint.alert.kind, "actual": alert["kind"]},
    }
    alert_mismatch = (
        checkpoint.alert.should_alert != alert["should_alert"]
        or checkpoint.alert.event != alert["event"]
        or checkpoint.alert.kind != alert["kind"]
    )
    timing = _timing_result(checkpoint, positions, actual)
    analyzer_has_difference = any(
        value if isinstance(value, bool) else bool(value) for value in analyzer_diff.values()
    )
    ledger_has_difference = any(bool(value) for value in ledger_diff.values())
    difference = {
        "analyzer": analyzer_diff,
        "ledger": ledger_diff,
        "decision": decision_diff,
        "alert": alert_diff,
        "timing": timing,
    }
    return {
        "difference": analyzer_has_difference or ledger_has_difference or decision_mismatch or alert_mismatch or not timing["pass"],
        "failure": decision_mismatch or alert_mismatch or not timing["pass"],
        "layers": difference,
        "failure_reasons": _failure_reasons(
            decision_mismatch=decision_mismatch,
            alert_mismatch=alert_mismatch,
            timing_pass=timing["pass"],
        ),
    }


def _timing_result(
    checkpoint: GoldCheckpoint, positions: dict[str, int], actual: dict[str, Any]
) -> dict[str, Any]:
    actual_event = bool(actual["alert"]["event"])
    actual_position = positions[checkpoint.after_message_id]
    earliest = positions.get(checkpoint.alert_window.get("earliest_allowed_after", ""))
    latest = positions.get(checkpoint.alert_window.get("latest_allowed_after", ""))
    too_early = actual_event and earliest is not None and actual_position < earliest
    too_late = actual_event and latest is not None and actual_position > latest
    return {
        "pass": not too_early and not too_late,
        "actual_event": actual_event,
        "actual_checkpoint_ordinal": checkpoint.ordinal,
        "earliest_allowed_ordinal": earliest,
        "latest_allowed_ordinal": latest,
        "timing_semantics": "ordinal_only; no wall_clock_measurement",
        "too_early": too_early,
        "too_late": too_late,
    }


def _failure_reasons(*, decision_mismatch: bool, alert_mismatch: bool, timing_pass: bool) -> list[str]:
    reasons: list[str] = []
    if decision_mismatch:
        reasons.append("decision_mismatch")
    if alert_mismatch:
        reasons.append("alert_mismatch")
    if not timing_pass:
        reasons.append("alert_outside_gold_window")
    return reasons


def _missing(expected: tuple[str, ...] | list[str], actual: Any) -> list[str]:
    actual_set = set(actual)
    return [value for value in expected if value not in actual_set]


def _present(expected: tuple[str, ...] | list[str], actual: Any) -> list[str]:
    actual_set = set(actual)
    return [value for value in expected if value in actual_set]


__all__ = ["GuardLiveCompareRunner", "LiveSnapshotExecution"]
