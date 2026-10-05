"""Independent Guard queue worker."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timezone

from echo_v2.domain.guard import GuardChatState
from echo_v2.persistence.guard_repositories import (
    GuardAnalysisCommitRepository,
    GuardChatStateRepository,
)
from echo_v2.services.guard_analysis import GuardAnalysisProcessor

_logger = logging.getLogger("echo_v2.services.guard_analysis_worker")


class GuardAnalysisWorker:
    def __init__(
        self,
        *,
        state_repo: GuardChatStateRepository,
        processor: GuardAnalysisProcessor,
        commit_repo: GuardAnalysisCommitRepository,
        poll_interval_seconds: float = 60.0,
        excluded_chat_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._state = state_repo
        self._processor = processor
        self._commit = commit_repo
        self._poll_interval = poll_interval_seconds
        self._excluded = excluded_chat_ids

    async def run_once(self, *, limit: int = 20) -> bool:
        due = await self._state.list_due(datetime.now(timezone.utc), limit=limit)
        touched = False
        for chat in due:
            if chat.chat_id in self._excluded:
                touched = True
                continue
            try:
                await self._process(chat)
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "error processing Guard chat %s/%s",
                    chat.child_user_id,
                    chat.chat_id,
                )
            touched = True
        return touched

    async def run_loop(self) -> None:  # pragma: no cover - infinite lifecycle loop
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("Guard analysis worker loop error")
            await asyncio.sleep(self._poll_interval)

    async def _process(self, chat: GuardChatState) -> str:
        prepared = await self._processor.process(
            chat.child_user_id,
            chat.chat_id,
            chat.activity_version,
        )
        record = replace(
            prepared.record,
            schedule_reason=chat.next_analysis_reason,
            pending_since=chat.pending_since,
            scheduled_for=chat.next_analysis_at,
        )
        status, result_id = await self._commit.commit_if_current(
            child_user_id=chat.child_user_id,
            chat_id=chat.chat_id,
            target_version=chat.activity_version,
            record=record,
        )
        _logger.info(
            "Guard analysis %s for %s/%s version=%d result=%s decision=%s",
            status,
            chat.child_user_id,
            chat.chat_id,
            chat.activity_version,
            result_id,
            prepared.record.decision.value,
        )
        return status
