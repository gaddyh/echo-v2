"""Tests for isolated cumulative Guard Live Compare execution."""

from __future__ import annotations

from dataclasses import replace

from echo_v2.services.guard_alert_policy import AlertKind
from echo_v2.services.guard_analyzer import (
    GuardAnalysis,
    GuardAnalysisInput,
    GuardMessage,
)
from echo_v2.services.guard_eval_gold import (
    GoldAlertExpectation,
    GoldAnalyzerExpectation,
    GoldCheckpoint,
    GoldLedgerExpectation,
    GoldScenario,
)
from echo_v2.services.guard_live_compare import GuardLiveCompareRunner
from echo_v2.services.guard_taxonomy import GUARD_TAXONOMY_VERSION


class FakeAnalyzer:
    def __init__(self, analyses: list[GuardAnalysis]) -> None:
        self._analyses = iter(analyses)
        self.inputs: list[GuardAnalysisInput] = []

    async def analyze(self, conversation: GuardAnalysisInput) -> GuardAnalysis:
        self.inputs.append(conversation)
        return next(self._analyses)


def _scenario() -> GoldScenario:
    messages = (
        GuardMessage("m1", "other", "I saw you outside."),
        GuardMessage("m2", "other", "Do you wait there alone?"),
    )
    return GoldScenario(
        scenario_id="fake",
        family="suspicious_contact",
        title="Fake",
        context={"contact_is_known": False},
        alert_window={"earliest_allowed_after": "m2", "latest_allowed_after": "m2"},
        messages=messages,
        checkpoints=(
            GoldCheckpoint(
                ordinal=0,
                after_message_id="m1",
                prefix_length=1,
                analyzer=GoldAnalyzerExpectation(
                    required_categories=("suspicious_contact",),
                    required_signals=("offline_knowledge",),
                    required_signal_any_of=(),
                    forbidden_categories=(),
                    forbidden_signals=(),
                    required_evidence_message_ids=("m1",),
                    expect_clean=False,
                ),
                ledger=GoldLedgerExpectation(
                    required_active_categories=("suspicious_contact",),
                    required_active_signals=("offline_knowledge",),
                    required_evidence_message_ids=("m1",),
                ),
                decision="concerning",
                alert=GoldAlertExpectation(False, False, "none", ""),
                alert_window={"earliest_allowed_after": "m2", "latest_allowed_after": "m2"},
            ),
            GoldCheckpoint(
                ordinal=1,
                after_message_id="m2",
                prefix_length=2,
                analyzer=GoldAnalyzerExpectation(
                    required_categories=("suspicious_contact",),
                    required_signals=("routine_probing",),
                    required_signal_any_of=(),
                    forbidden_categories=(),
                    forbidden_signals=(),
                    required_evidence_message_ids=("m2",),
                    expect_clean=False,
                ),
                ledger=GoldLedgerExpectation(
                    required_active_categories=("suspicious_contact",),
                    required_active_signals=("offline_knowledge", "routine_probing"),
                    required_evidence_message_ids=("m1", "m2"),
                ),
                decision="concerning",
                alert=GoldAlertExpectation(False, False, "none", ""),
                alert_window={"earliest_allowed_after": "m2", "latest_allowed_after": "m2"},
            ),
        ),
    )


async def test_runner_uses_full_prefix_and_accumulates_ledger() -> None:
    analyzer = FakeAnalyzer(
        [
            GuardAnalysis(
                categories=("suspicious_contact",),
                signals=("offline_knowledge",),
                evidence_message_ids=("m1",),
                model="test-model",
                prompt_version="test-prompt",
                analyzer_version="test-analyzer",
                taxonomy_version=GUARD_TAXONOMY_VERSION,
            ),
            GuardAnalysis(
                categories=("suspicious_contact",),
                signals=("routine_probing",),
                evidence_message_ids=("m2",),
                model="test-model",
                prompt_version="test-prompt",
                analyzer_version="test-analyzer",
                taxonomy_version=GUARD_TAXONOMY_VERSION,
            ),
        ]
    )
    runner = GuardLiveCompareRunner(
        analyzer=analyzer,
        model="test-model",
        prompt_version="test-prompt",
        analyzer_version="test-analyzer",
        taxonomy_version=GUARD_TAXONOMY_VERSION,
    )

    results = await runner.run(_scenario())

    assert [len(item.messages) for item in analyzer.inputs] == [1, 2]
    assert results[1].actual["ledger"]["active_signals"] == [
        "offline_knowledge",
        "routine_probing",
    ]
    assert results[1].actual["decision"] == "concerning"
    assert results[1].actual["alert"]["kind"] == AlertKind.NONE.value
    assert results[1].failure is False


async def test_analyzer_difference_can_be_non_failure() -> None:
    analyzer = FakeAnalyzer(
        [
            GuardAnalysis(
                categories=("suspicious_contact",),
                signals=("offline_knowledge",),
                evidence_message_ids=("m1",),
            ),
            GuardAnalysis(
                categories=("suspicious_contact",),
                signals=("offline_knowledge",),
                evidence_message_ids=("m2",),
            ),
        ]
    )
    runner = GuardLiveCompareRunner(
        analyzer=analyzer,
        model="test-model",
        prompt_version="test-prompt",
        analyzer_version="test-analyzer",
        taxonomy_version=GUARD_TAXONOMY_VERSION,
    )
    scenario = _scenario()
    scenario = replace(
        scenario,
        checkpoints=(
            scenario.checkpoints[0],
            replace(
                scenario.checkpoints[1],
                analyzer=replace(
                    scenario.checkpoints[1].analyzer,
                    required_signals=("meeting_request",),
                ),
                ledger=GoldLedgerExpectation(
                    required_active_categories=("suspicious_contact",),
                    required_active_signals=("offline_knowledge",),
                    required_evidence_message_ids=("m1",),
                ),
                decision="concerning",
                alert=GoldAlertExpectation(False, False, "none", ""),
            ),
        ),
    )

    results = await runner.run(scenario)

    assert results[0].has_difference is False
    assert results[1].has_difference is True
    assert results[1].failure is False
    assert results[1].diff["layers"]["analyzer"]["missing_signals"] == ["meeting_request"]
