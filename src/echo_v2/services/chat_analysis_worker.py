"""ChatAnalysisWorker — single-worker poller that processes due chats.

The worker polls :meth:`ChatStateRepository.list_due` once a minute,
processes each due chat, and commits the result only if
``activity_version`` hasn't changed during processing (conditional
``mark_processed``).

Safety properties:

* **Version check after processing**: The worker reads
  ``activity_version`` before processing, processes the chat, then
  re-reads. If the version changed (new message arrived during
  processing), the result is discarded. The chat stays due and gets
  reprocessed on the next poll.

* **Worker crash = reprocess**: No lease or claim_token. If the worker
  crashes mid-processing, the chat still has ``next_analysis_at`` in
  the past and ``activity_version > last_processed_version``. After
  restart, the worker picks it up again. At-least-once processing;
  result committed at most once (version check).

* **Single worker only**: No ``FOR UPDATE SKIP LOCKED`` — the
  ``list_due`` query is a plain SELECT. For POC with one worker, this
  is fine. When a real processor with side effects is added, we'll
  need idempotency (e.g., key based on version).

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
from typing import Protocol, runtime_checkable

from echo_v2.domain.chat import ChatState
from echo_v2.persistence.chat_repositories import (
    ChatStateRepository,
    MessageRepository,
    WaitingForMeResultRepository,
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
    :class:`WaitingForMeAnalyzer` to produce a :class:`WaitingForMeResult`.
    """

    async def process(
        self,
        user_id: str,
        chat_id: str,
        target_version: int,
    ) -> None: ...


class RecordingAnalysisProcessor:
    """Test/development processor that records calls without real analysis.

    Records every call as a ``(user_id, chat_id, target_version)`` tuple.
    Does not modify any state — safe to use in tests and as a placeholder
    in production wiring (though the worker is not started by default).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def process(
        self,
        user_id: str,
        chat_id: str,
        target_version: int,
    ) -> None:
        self.calls.append((user_id, chat_id, target_version))


class ChatAnalysisProcessor:
    """Loads messages, builds conversation input, runs analysis, stores result.

    Stage 3: message loading + LLM analysis + result storage. The processor
    loads messages via :meth:`MessageRepository.list_for_analysis`, builds a
    :class:`ConversationInput`, passes it to a :class:`WaitingForMeAnalyzer`,
    and stores the :class:`WaitingForMeResult` via
    :class:`WaitingForMeResultRepository`.

    Args:
        message_repo: The :class:`MessageRepository` for
            :meth:`list_for_analysis`.
        analyzer: The :class:`WaitingForMeAnalyzer` that produces the
            :class:`WaitingForMeResult`.
        result_repo: The :class:`WaitingForMeResultRepository` that stores
            the result. If ``None``, results are logged but not persisted.
        context_messages: Number of messages before the last outbound
            to include for context. Default 5.
        max_no_outbound: If no outbound exists, load this many recent
            messages. Default 20.
    """

    def __init__(
        self,
        message_repo: MessageRepository,
        analyzer: WaitingForMeAnalyzer,
        result_repo: WaitingForMeResultRepository | None = None,
        *,
        context_messages: int = 5,
        max_no_outbound: int = 20,
    ) -> None:
        self._messages = message_repo
        self._analyzer = analyzer
        self._result_repo = result_repo
        self._context_messages = context_messages
        self._max_no_outbound = max_no_outbound

    async def process(
        self,
        user_id: str,
        chat_id: str,
        target_version: int,
    ) -> None:
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
        if self._result_repo is not None:
            await self._result_repo.save(
                user_id=user_id,
                chat_id=chat_id,
                result=result,
            )
            _logger.info(
                "stored result for chat %s/%s (version %d)",
                user_id,
                chat_id,
                target_version,
            )


class ChatAnalysisWorker:
    """Single-worker poller that processes due chats.

    Args:
        chat_state_repo: The repository for ``list_due``, ``get``, and
            ``mark_processed``.
        processor: The :class:`AnalysisProcessor` that analyzes each chat.
        poll_interval_seconds: How often ``run_loop`` polls for due chats.
            Default 60 seconds.
    """

    def __init__(
        self,
        chat_state_repo: ChatStateRepository,
        processor: AnalysisProcessor,
        *,
        poll_interval_seconds: float = 60.0,
    ) -> None:
        self._chat_state_repo = chat_state_repo
        self._processor = processor
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

    async def _process_chat(self, chat: ChatState) -> None:
        """Process a single due chat with version-check safety.

        1. Record ``activity_version`` before processing.
        2. Call the processor (outside any transaction — no lock held).
        3. Re-read the chat. If ``activity_version`` is unchanged,
           ``mark_processed`` (conditional UPDATE). If it changed,
           discard the result — the chat is still due and will be
           reprocessed on the next poll.
        """
        version_being_processed = chat.activity_version

        # Process (outside any transaction — no lock held)
        await self._processor.process(
            chat.user_id,
            chat.chat_id,
            version_being_processed,
        )

        # Version check: only accept result if version is still current
        fresh_chat = await self._chat_state_repo.get(chat.user_id, chat.chat_id)
        if fresh_chat is None:
            _logger.warning(
                "chat %s/%s disappeared during processing, discarding result",
                chat.user_id,
                chat.chat_id,
            )
            return

        if fresh_chat.activity_version == version_being_processed:
            # Version unchanged — accept result, mark processed
            updated = await self._chat_state_repo.mark_processed(
                chat.user_id,
                chat.chat_id,
                version_being_processed,
            )
            if not updated:
                # Race: version changed between our get and mark_processed.
                # The chat will be reprocessed on the next poll.
                _logger.info(
                    "chat %s/%s version changed during mark_processed, "
                    "result discarded",
                    chat.user_id,
                    chat.chat_id,
                )
        else:
            # New message arrived during processing — discard result.
            # The chat is still due (next_analysis_at set by the new message)
            # and will be reprocessed on the next poll.
            _logger.info(
                "chat %s/%s version changed during processing (%d -> %d), "
                "discarding result",
                chat.user_id,
                chat.chat_id,
                version_being_processed,
                fresh_chat.activity_version,
            )
