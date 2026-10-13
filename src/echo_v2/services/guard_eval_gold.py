"""Typed loader for the human-authored Guard Live Compare Gold set."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from echo_v2.services.guard_analyzer import GuardMessage

_DEFAULT_GOLD_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "static"
    / "guard_demo_scenarios_he.json"
)


@dataclass(frozen=True)
class GoldAnalyzerExpectation:
    required_categories: tuple[str, ...]
    required_signals: tuple[str, ...]
    required_signal_any_of: tuple[str, ...]
    forbidden_categories: tuple[str, ...]
    forbidden_signals: tuple[str, ...]
    required_evidence_message_ids: tuple[str, ...]
    expect_clean: bool


@dataclass(frozen=True)
class GoldLedgerExpectation:
    required_active_categories: tuple[str, ...]
    required_active_signals: tuple[str, ...]
    required_evidence_message_ids: tuple[str, ...]


@dataclass(frozen=True)
class GoldAlertExpectation:
    should_alert: bool
    event: bool
    kind: str
    reason: str


@dataclass(frozen=True)
class GoldCheckpoint:
    ordinal: int
    after_message_id: str
    prefix_length: int
    analyzer: GoldAnalyzerExpectation
    ledger: GoldLedgerExpectation
    decision: str
    alert: GoldAlertExpectation
    alert_window: dict[str, str]


@dataclass(frozen=True)
class GoldScenario:
    scenario_id: str
    family: str
    title: str
    context: dict[str, object]
    alert_window: dict[str, str]
    messages: tuple[GuardMessage, ...]
    checkpoints: tuple[GoldCheckpoint, ...]


@dataclass(frozen=True)
class GuardGoldSet:
    version: str
    label: str
    scenarios: tuple[GoldScenario, ...]

    def scenario(self, scenario_id: str) -> GoldScenario:
        for scenario in self.scenarios:
            if scenario.scenario_id == scenario_id:
                return scenario
        raise KeyError(f"unknown Guard Gold scenario: {scenario_id}")


def load_guard_gold(path: Path = _DEFAULT_GOLD_PATH) -> GuardGoldSet:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("scenarios"), list):
        raise TypeError("Guard Gold fixture must contain a scenarios array")
    source = payload.get("source")
    if not isinstance(source, dict):
        raise TypeError("Guard Gold fixture must contain source metadata")
    version = str(payload.get("version", ""))
    label = str(source.get("label", ""))
    scenarios = tuple(_scenario(raw, index) for index, raw in enumerate(payload["scenarios"]))
    if len({scenario.scenario_id for scenario in scenarios}) != len(scenarios):
        raise ValueError("Guard Gold scenario IDs must be unique")
    return GuardGoldSet(version=version, label=label, scenarios=scenarios)


def _scenario(raw: Any, scenario_index: int) -> GoldScenario:
    if not isinstance(raw, dict):
        raise TypeError(f"Guard Gold scenario {scenario_index} must be an object")
    scenario_id = str(raw["id"])
    messages = tuple(
        GuardMessage(id=str(message["id"]), sender=str(message["sender"]), text=str(message["text"]))
        for message in raw.get("messages", [])
    )
    positions = {message.id: index for index, message in enumerate(messages)}
    if len(positions) != len(messages):
        raise ValueError(f"{scenario_id}: message IDs must be unique")
    alert_window = _string_map(raw.get("alert_window", {}))
    checkpoints: list[GoldCheckpoint] = []
    previous_position = -1
    for ordinal, raw_checkpoint in enumerate(raw.get("snapshots", [])):
        if not isinstance(raw_checkpoint, dict):
            raise TypeError(f"{scenario_id}: checkpoint must be an object")
        after = str(raw_checkpoint["after_message_id"])
        if after not in positions:
            raise ValueError(f"{scenario_id}: unknown checkpoint message {after}")
        position = positions[after]
        if position <= previous_position:
            raise ValueError(f"{scenario_id}: checkpoints must be chronological")
        previous_position = position
        expected = _mapping(raw_checkpoint.get("expected", {}))
        analyzer_raw = _mapping(raw_checkpoint.get("analyzer_expected", expected))
        cumulative = _mapping(raw_checkpoint.get("cumulative", {}))
        ledger_raw = _mapping(raw_checkpoint.get("ledger_expected", {}))
        alert_raw = _mapping(raw_checkpoint.get("alert_expected", {}))
        analyzer = GoldAnalyzerExpectation(
            required_categories=_strings(analyzer_raw.get("required_categories", [])),
            required_signals=_strings(analyzer_raw.get("required_signals", [])),
            required_signal_any_of=_strings(analyzer_raw.get("required_signal_any_of", [])),
            forbidden_categories=_strings(analyzer_raw.get("forbidden_categories", [])),
            forbidden_signals=_strings(analyzer_raw.get("forbidden_signals", [])),
            required_evidence_message_ids=_strings(
                analyzer_raw.get("required_evidence_message_ids", [])
            ),
            expect_clean=bool(analyzer_raw.get("expect_clean", False)),
        )
        ledger = GoldLedgerExpectation(
            required_active_categories=_strings(
                ledger_raw.get("required_active_categories", cumulative.get("categories", []))
            ),
            required_active_signals=_strings(
                ledger_raw.get("required_active_signals", cumulative.get("signals", []))
            ),
            required_evidence_message_ids=_strings(
                ledger_raw.get("required_evidence_message_ids", cumulative.get("evidence_message_ids", []))
            ),
        )
        decision = str(
            raw_checkpoint.get(
                "decision_expected",
                expected.get("expected_decision", raw_checkpoint.get("target_decision", "none")),
            )
        )
        alert = GoldAlertExpectation(
            should_alert=bool(alert_raw.get("should_alert", raw_checkpoint.get("should_alert", False))),
            event=bool(alert_raw.get("event", raw_checkpoint.get("alert_event", False))),
            kind=str(alert_raw.get("kind", raw_checkpoint.get("alert_kind", "none"))),
            reason=str(alert_raw.get("reason", raw_checkpoint.get("alert_reason", ""))),
        )
        checkpoints.append(
            GoldCheckpoint(
                ordinal=ordinal,
                after_message_id=after,
                prefix_length=int(raw_checkpoint.get("prefix_length", position + 1)),
                analyzer=analyzer,
                ledger=ledger,
                decision=decision,
                alert=alert,
                alert_window=alert_window,
            )
        )
    if not checkpoints:
        raise ValueError(f"{scenario_id}: scenario has no checkpoints")
    return GoldScenario(
        scenario_id=scenario_id,
        family=str(raw.get("family", "")),
        title=str(raw.get("title", scenario_id)),
        context=dict(raw.get("context", {})),
        alert_window=alert_window,
        messages=messages,
        checkpoints=tuple(checkpoints),
    )


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("Gold expectation values must be objects")
    return value


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError("Gold expectation lists must be arrays")
    return tuple(str(item) for item in value)


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise TypeError("Gold alert_window must be an object")
    return {str(key): str(item) for key, item in value.items()}


__all__ = [
    "GoldAlertExpectation",
    "GoldAnalyzerExpectation",
    "GoldCheckpoint",
    "GoldLedgerExpectation",
    "GoldScenario",
    "GuardGoldSet",
    "load_guard_gold",
]
