"""ChatAnalysisWorker — single-worker poller that processes due chats.

The worker polls :meth:`ChatStateRepository.list_due` once a minute,
processes each due chat, and commits the result atomically via
:meth:`AnalysisCommitRepository.commit_if_current`.

Safety properties:

* **Atomic version-checked commit**: The processor runs the LLM and
  returns a :class:`PreparedAnalysis` without persisting. The
  :class:`AnalysisCommitRepository` then locks the chat row, checks
  ``activity_version``, and only commits (insert result + update active +
  mark processed) if the version is still current. If a new message
  arrived during processing, nothing is written.

* **Worker crash = reprocess**: No lease or claim_token. If the worker
  crashes mid-processing, the chat still has ``next_analysis_at`` in
  the past and ``activity_version > last_processed_version``. After
  restart, the worker picks it up again. At-least-once processing;
  result committed at most once (atomic version check).

* **Single worker only**: No ``FOR UPDATE SKIP LOCKED`` on
  ``list_due`` — it is a plain SELECT. For POC with one worker, this
  is fine. The atomic commit handles races if a message arrives during
  processing.

The worker is **not started in production** by default
(``CHAT_ANALYSIS_ENABLED=false``). It is constructed and tested but
not wired into the lifespan loop until a real
:class:`AnalysisProcessor` exists.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol, runtime_checkable

from langsmith import traceable

from echo_v2.domain.chat import ChatState
from echo_v2.domain.waiting_for_me import (
    PreparedAnalysis,
    WaitingForMeDecision,
    WaitingForMeResult,
)
from echo_v2.persistence.chat_repositories import (
    AnalysisCommitRepository,
    ChatStateRepository,
    MessageRepository,
)
from echo_v2.services.waiting_for_me_analyzer import WaitingForMeAnalyzer

__all__ = [
    "AnalysisProcessor",
    "ChatAnalysisProcessor",
    "ChatAnalysisWorker",
    "ConversationInput",
    "RecordingAnalysisProcessor",
]

_logger = logging.getLogger("echo_v2.services.chat_analysis_worker")


def safe_process_chat_inputs(inputs: dict) -> dict:
    """Sanitize _process_chat inputs for LangSmith trace metadata.

    Removes ``self`` and the full ``ChatState``. Keeps only safe
    correlation fields: hashed IDs, version.
    """
    from echo_v2.observability.privacy import correlation_id

    chat = inputs.get("chat")
    if chat is None:
        return {}
    return {
        "user_id_hash": correlation_id(chat.user_id),
        "chat_id_hash": correlation_id(chat.chat_id),
        "target_version": chat.activity_version,
    }


def safe_process_chat_output(output: str) -> dict:
    """Sanitize _process_chat output for LangSmith trace metadata."""
    return {"status": output}


@dataclass(frozen=True)
class ConversationInput:
    """Stable, serializable representation of a chat slice for analysis.

    Built from the messages loaded by :meth:`MessageRepository.list_for_analysis`.
    Contains only the fields needed for the LLM prompt — no IDs, no
    connection metadata.
    """

    user_id: str
    chat_id: str
    target_version: int
    messages: list[tuple[str, str, datetime]]  # (direction, text, timestamp)


@runtime_checkable
class AnalysisProcessor(Protocol):
    """Hook for chat analysis.

    The default :class:`RecordingAnalysisProcessor` records calls without
    doing real analysis. :class:`ChatAnalysisProcessor` loads messages,
    builds a :class:`ConversationInput`, and calls a
    :class:`WaitingForMeAnalyzer` to produce a :class:`PreparedAnalysis`.

    The processor does NOT persist — it returns the analysis result and
    conversation snapshot. The :class:`ChatAnalysisWorker` commits the
    result atomically via :class:`AnalysisCommitRepository`.
    """

    async def process(
        self,
        user_id: str,
        chat_id: str,
        target_version: int,
    ) -> PreparedAnalysis: ...


class RecordingAnalysisProcessor:
    """Test/development processor that records calls without real analysis.

    Records every call as a ``(user_id, chat_id, target_version)`` tuple.
    Returns a dummy :class:`PreparedAnalysis` with an ``UNCERTAIN`` decision
    — safe to use in tests and as a placeholder in production wiring (though
    the worker is not started by default).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def process(
        self,
        user_id: str,
        chat_id: str,
        target_version: int,
    ) -> PreparedAnalysis:
        self.calls.append((user_id, chat_id, target_version))
        return PreparedAnalysis(
            result=WaitingForMeResult(
                decision=WaitingForMeDecision.UNCERTAIN,
                confidence=1.0,
                reason="Recording processor — no real analysis.",
                target_version=target_version,
            ),
            conversation_snapshot={},
        )


class ChatAnalysisProcessor:
    """Loads messages, builds conversation input, runs analysis.

    Stage 4: message loading + LLM analysis. The processor loads messages
    via :meth:`MessageRepository.list_for_analysis`, builds a
    :class:`ConversationInput`, passes it to a :class:`WaitingForMeAnalyzer`,
    and returns a :class:`PreparedAnalysis` containing the result and a
    conversation snapshot for training/feedback.

    The processor does NOT persist. The :class:`ChatAnalysisWorker`
    commits the result atomically via :class:`AnalysisCommitRepository`,
    which handles:

    - ``WAITING_FOR_ME`` → upsert active (preserve ``waiting_since`` and
      ``notified_at`` if a row already exists).
    - ``NOT_WAITING_FOR_ME`` / ``UNCERTAIN`` → delete active if exists.

    Args:
        message_repo: The :class:`MessageRepository` for
            :meth:`list_for_analysis`.
        analyzer: The :class:`WaitingForMeAnalyzer` that produces the
            :class:`WaitingForMeResult`.
        context_messages: Number of messages before the last outbound
            to include for context. Default 5.
        max_no_outbound: If no outbound exists, load this many recent
            messages. Default 20.
    """

    def __init__(
        self,
        message_repo: MessageRepository,
        analyzer: WaitingForMeAnalyzer,
        *,
        context_messages: int = 5,
        max_no_outbound: int = 20,
    ) -> None:
        self._messages = message_repo
        self._analyzer = analyzer
        self._context_messages = context_messages
        self._max_no_outbound = max_no_outbound

    async def process(
        self,
        user_id: str,
        chat_id: str,
        target_version: int,
    ) -> PreparedAnalysis:
        messages = await self._messages.list_for_analysis(
            user_id=user_id,
            chat_id=chat_id,
            context_messages=self._context_messages,
            max_no_outbound=self._max_no_outbound,
        )
        conversation = ConversationInput(
            user_id=user_id,
            chat_id=chat_id,
            target_version=target_version,
            messages=[
                (m.direction.value, m.text or "", m.timestamp)
                for m in messages
            ],
        )
        result = await self._analyzer.analyze(conversation)
        _logger.info(
            "analysis for chat %s/%s (version %d): %s (confidence=%s, reason=%s)",
            user_id,
            chat_id,
            target_version,
            result.decision.value,
            result.confidence,
            result.reason,
        )

        # Build a conversation snapshot for training/feedback.
        # This is the exact conversation the model saw — not messages
        # loaded later when feedback arrives.
        conversation_snapshot = {
            "messages": [
                {
                    "direction": m.direction.value,
                    "text": m.text or "",
                    "timestamp": m.timestamp.isoformat() if m.timestamp else None,
                }
                for m in messages
            ],
            "target_version": target_version,
        }

        return PreparedAnalysis(
            result=result,
            conversation_snapshot=conversation_snapshot,
        )


class ChatAnalysisWorker:
    """Single-worker poller that processes due chats.

    Args:
        chat_state_repo: The repository for ``list_due`` and ``get``.
        processor: The :class:`AnalysisProcessor` that analyzes each chat
            and returns a :class:`PreparedAnalysis` (no persistence).
        commit_repo: The :class:`AnalysisCommitRepository` that atomically
            commits the analysis if the chat version is still current.
        poll_interval_seconds: How often ``run_loop`` polls for due chats.
            Default 60 seconds.
    """

    def __init__(
        self,
        chat_state_repo: ChatStateRepository,
        processor: AnalysisProcessor,
        commit_repo: AnalysisCommitRepository,
        *,
        poll_interval_seconds: float = 60.0,
    ) -> None:
        self._chat_state_repo = chat_state_repo
        self._processor = processor
        self._commit_repo = commit_repo
        self._poll_interval = poll_interval_seconds

    async def run_once(self, *, limit: int = 20) -> bool:
        """Process all due chats once.

        Returns ``True`` if at least one chat was processed.
        """
        now = datetime.now(timezone.utc)
        due_chats = await self._chat_state_repo.list_due(now, limit=limit)
        for chat in due_chats:
            try:
                await self._process_chat(chat)
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "error processing chat %s/%s, continuing",
                    chat.user_id,
                    chat.chat_id,
                )
        return len(due_chats) > 0

    async def run_loop(self) -> None:
        """Poll for due chats until cancelled.

        On each iteration, calls ``run_once``. If nothing was due, sleeps
        for ``poll_interval_seconds``. Handles ``CancelledError`` cleanly.
        """
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                _logger.info("chat analysis worker loop cancelled")
                raise
            except Exception:
                # An unexpected error in the loop should not kill the
                # worker — log and continue after a pause.
                _logger.exception("chat analysis worker loop error, continuing")
            try:
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                _logger.info("chat analysis worker loop cancelled during sleep")
                raise

    @traceable(
        name="wfm.analysis",
        process_inputs=safe_process_chat_inputs,
        process_outputs=safe_process_chat_output,
    )
    async def _process_chat(
        self,
        chat: ChatState,
    ) -> Literal["committed", "stale", "missing"]:
        """Process a single due chat with atomic version-checked commit.

        1. Call the processor (outside any transaction — no lock held).
           The processor runs the LLM and returns a
           :class:`PreparedAnalysis` without persisting.
        2. Call :meth:`AnalysisCommitRepository.commit_if_current`, which
           locks the chat row, checks the version, and commits the result
           + active state + mark_processed in one transaction.

        Returns ``"committed"`` if the result was persisted, ``"stale"``
        if the version changed during processing (nothing written), or
        ``"missing"`` if the chat disappeared during processing.
        """
        version_being_processed = chat.activity_version

        # Process (outside any transaction — no lock held)
        prepared = await self._processor.process(
            chat.user_id,
            chat.chat_id,
            version_being_processed,
        )

        # Atomic commit: lock, version check, insert, upsert, mark processed.
        outcome = await self._commit_repo.commit_if_current(
            user_id=chat.user_id,
            chat_id=chat.chat_id,
            target_version=version_being_processed,
            analysis=prepared,
        )

        if outcome.status == "committed":
            _logger.info(
                "committed analysis for chat %s/%s (version %d, result_id=%s)",
                chat.user_id,
                chat.chat_id,
                version_being_processed,
                outcome.result_id,
            )
        elif outcome.status == "stale":
            _logger.info(
                "chat %s/%s version changed during processing (%d), "
                "discarding result",
                chat.user_id,
                chat.chat_id,
                version_being_processed,
            )
        else:  # missing
            _logger.warning(
                "chat %s/%s disappeared during processing, discarding result",
                chat.user_id,
                chat.chat_id,
            )

        return outcome.status
