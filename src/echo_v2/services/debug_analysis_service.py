"""DebugAnalysisService — list all analyzed chats + LLM results for a user.

Used by the debug analysis web view (``GET /api/debug/analysis``). Combines
:class:`ChatStateRepository.list_all_for_user` with
:class:`WaitingForMeResultRepository.list_all_for_user` to produce a
per-chat view of every analysis run, including the LLM decision, reason,
summary, model, and conversation snapshot.

This is an operator/debug tool — not exposed to end users.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from echo_v2.persistence.chat_repositories import (
    ChatStateRepository,
    WaitingForMeResultEntry,
    WaitingForMeResultRepository,
)
from echo_v2.services.waiting_list_token_service import WaitingListTokenService

__all__ = [
    "DebugAnalysisResponse",
    "DebugAnalysisService",
    "DebugChatEntry",
    "DebugResultEntry",
]

_logger = logging.getLogger("echo_v2.services.debug_analysis")


@dataclass(frozen=True)
class DebugResultEntry:
    """One analysis result row in the debug view."""

    id: str
    created_at: str
    target_version: int
    decision: str
    next_owner: str | None
    open_obligation: str | None
    confidence: float | None
    reason: str | None
    summary: str | None
    model: str | None
    prompt_version: str | None
    analyzer_version: str | None
    conversation_snapshot: dict[str, Any] | None


@dataclass(frozen=True)
class DebugChatEntry:
    """One chat with all its analysis results in the debug view."""

    chat_id: str
    chat_name: str | None
    last_message_at: str
    last_direction: str
    activity_version: int
    results: list[DebugResultEntry] = field(default_factory=list)


@dataclass(frozen=True)
class DebugAnalysisResponse:
    """Top-level response for the debug analysis endpoint."""

    user_id: str
    chats: list[DebugChatEntry]
    total_results: int


class DebugAnalysisService:
    """List all analyzed chats + LLM results for a user.

    Args:
        token_service: The :class:`WaitingListTokenService` for session
            validation.
        chat_state_repo: The :class:`ChatStateRepository` for listing chats.
        result_repo: The :class:`WaitingForMeResultRepository` for listing
            analysis results.
        excluded_chat_ids: Chat IDs to hide from the debug view (e.g. the
            Echo bot's own service chat). Defaults to empty.
    """

    def __init__(
        self,
        *,
        token_service: WaitingListTokenService,
        chat_state_repo: ChatStateRepository,
        result_repo: WaitingForMeResultRepository,
        excluded_chat_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._token_service = token_service
        self._chat_state_repo = chat_state_repo
        self._result_repo = result_repo
        self._excluded_chat_ids = excluded_chat_ids

    async def list_analysis(
        self,
        *,
        session_id: str,
    ) -> DebugAnalysisResponse | None:
        """List all analyzed chats + results for the session's user.

        Returns ``None`` if the session is invalid/expired.
        """
        resolved = await self._token_service.resolve_session(session_id)
        if resolved is None:
            return None

        user_id = resolved.user_id

        chats = await self._chat_state_repo.list_all_for_user(user_id=user_id)
        results = await self._result_repo.list_all_for_user(user_id=user_id)

        # Exclude service chats (e.g. the Echo bot's own chat) from the
        # debug view — they are not analyzed and would only add noise.
        if self._excluded_chat_ids:
            chats = [c for c in chats if c.chat_id not in self._excluded_chat_ids]
            results = [r for r in results if r.chat_id not in self._excluded_chat_ids]

        # Group results by chat_id.
        results_by_chat: dict[str, list[WaitingForMeResultEntry]] = {}
        for entry in results:
            results_by_chat.setdefault(entry.chat_id, []).append(entry)

        chat_entries: list[DebugChatEntry] = []
        total_results = 0
        for chat in chats:
            chat_results = results_by_chat.get(chat.chat_id, [])
            total_results += len(chat_results)
            chat_entries.append(
                DebugChatEntry(
                    chat_id=chat.chat_id,
                    chat_name=chat.chat_name,
                    last_message_at=chat.last_message_at.isoformat() if chat.last_message_at else "",
                    last_direction=chat.last_direction.value,
                    activity_version=chat.activity_version,
                    results=[
                        DebugResultEntry(
                            id=r.id,
                            created_at=r.created_at.isoformat() if r.created_at else "",
                            target_version=r.result.target_version,
                            decision=r.result.decision.value,
                            next_owner=r.result.next_owner.value if r.result.next_owner else None,
                            open_obligation=r.result.open_obligation,
                            confidence=r.result.confidence,
                            reason=r.result.reason,
                            summary=r.result.summary,
                            model=r.result.model,
                            prompt_version=r.result.prompt_version,
                            analyzer_version=r.result.analyzer_version,
                            conversation_snapshot=r.result.conversation_snapshot,
                        )
                        for r in chat_results
                    ],
                )
            )

        # Include chats that have results but no chat state row (edge case).
        known_chat_ids = {c.chat_id for c in chats}
        for cid, chat_results in results_by_chat.items():
            if cid in known_chat_ids:
                continue
            total_results += len(chat_results)
            chat_entries.append(
                DebugChatEntry(
                    chat_id=cid,
                    chat_name=None,
                    last_message_at="",
                    last_direction="",
                    activity_version=0,
                    results=[
                        DebugResultEntry(
                            id=r.id,
                            created_at=r.created_at.isoformat() if r.created_at else "",
                            target_version=r.result.target_version,
                            decision=r.result.decision.value,
                            next_owner=r.result.next_owner.value if r.result.next_owner else None,
                            open_obligation=r.result.open_obligation,
                            confidence=r.result.confidence,
                            reason=r.result.reason,
                            summary=r.result.summary,
                            model=r.result.model,
                            prompt_version=r.result.prompt_version,
                            analyzer_version=r.result.analyzer_version,
                            conversation_snapshot=r.result.conversation_snapshot,
                        )
                        for r in chat_results
                    ],
                )
            )

        return DebugAnalysisResponse(
            user_id=user_id,
            chats=chat_entries,
            total_results=total_results,
        )
