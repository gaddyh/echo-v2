"""Guard processor: immutable observations plus bounded deterministic state replay."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from echo_v2.domain.guard import GuardAnalysisRecord
from echo_v2.persistence.chat_repositories import MessageRepository
from echo_v2.persistence.guard_repositories import GuardAnalysisRepository
from echo_v2.services.guard_analyzer import GuardAnalysis, GuardAnalyzer
from echo_v2.services.guard_conversation import GuardConversationBuilder
from echo_v2.services.guard_decision_policy import DefaultDecisionPolicy
from echo_v2.services.guard_signal_ledger import GuardSignalLedger
from echo_v2.services.guard_taxonomy import GUARD_TAXONOMY_VERSION

GUARD_SIGNAL_WINDOW_HOURS = 24


@dataclass(frozen=True)
class PreparedGuardAnalysis:
    record: GuardAnalysisRecord
    conversation: object


class GuardAnalysisProcessor:
    def __init__(
        self,
        *,
        message_repo: MessageRepository,
        analyzer: GuardAnalyzer,
        analysis_repo: GuardAnalysisRepository,
        conversation_builder: GuardConversationBuilder | None = None,
        signal_window_hours: float = GUARD_SIGNAL_WINDOW_HOURS,
    ) -> None:
        self._builder = conversation_builder or GuardConversationBuilder(message_repo)
        self._analyzer = analyzer
        self._analyses = analysis_repo
        self._window = timedelta(hours=signal_window_hours)

    async def process(
        self,
        child_user_id: str,
        chat_id: str,
        target_version: int,
    ) -> PreparedGuardAnalysis:
        conversation = await self._builder.build(
            child_user_id=child_user_id,
            chat_id=chat_id,
        )
        since = datetime.now(timezone.utc) - self._window
        prior = await self._analyses.list_for_replay(
            child_user_id=child_user_id,
            chat_id=chat_id,
            since=since,
            before_version=target_version,
        )
        ledger = GuardSignalLedger()
        for record in prior:
            ledger.update(
                GuardAnalysis(
                    signals=record.signals,
                    categories=record.categories,
                    evidence_message_ids=record.evidence_message_ids,
                    confidence=record.confidence,
                    reason=record.reason,
                    model=record.model,
                    prompt_version=record.prompt_version,
                    analyzer_version=record.analyzer_version,
                    taxonomy_version=record.taxonomy_version,
                )
            )
        current = await self._analyzer.analyze(conversation.input)
        state = ledger.update(current)
        cumulative_evidence_ids = tuple(
            dict.fromkeys(
                message_id
                for evidence_ids in state.evidence_by_signal.values()
                for message_id in evidence_ids
            )
        )
        decision = DefaultDecisionPolicy().decide(
            signals=state.active_signals,
            categories=state.active_categories,
            signal_state=state,
        )
        record = GuardAnalysisRecord(
            child_user_id=child_user_id,
            connection_id=conversation.connection_id,
            chat_id=chat_id,
            target_version=target_version,
            signals=current.signals,
            categories=current.categories,
            confidence=current.confidence,
            reason=current.reason,
            evidence_message_ids=cumulative_evidence_ids,
            decision=decision,
            model=current.model,
            prompt_version=current.prompt_version,
            analyzer_version=current.analyzer_version,
            taxonomy_version=current.taxonomy_version or GUARD_TAXONOMY_VERSION,
            diagnostics={
                "participant_roles": [
                    {
                        "canonical_id": item.canonical_id,
                        "role": item.role,
                        "display_name": item.display_name,
                    }
                    for item in conversation.participant_diagnostics
                ],
                "cumulative_signals": list(state.active_signals),
                "cumulative_categories": list(state.active_categories),
            },
        )
        return PreparedGuardAnalysis(record=record, conversation=conversation)
