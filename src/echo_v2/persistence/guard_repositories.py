"""Repository protocols and in-memory implementations for Guard."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from echo_v2.domain.guard import (
    GuardAnalysisRecord,
    GuardChatState,
    GuardianChildLink,
    GuardianChildLinkStatus,
)
from echo_v2.domain.guard_alert import (
    GuardAlert,
    GuardAlertEvaluation,
    GuardThresholdState,
)
from echo_v2.services.guard_taxonomy import GuardDecision

__all__ = [
    "GuardAnalysisCommitRepository",
    "GuardAnalysisRepository",
    "GuardChatStateRepository",
    "GuardianChildLinkRepository",
    "InMemoryGuardAnalysisRepository",
    "InMemoryGuardChatStateRepository",
    "InMemoryGuardianChildLinkRepository",
]


@runtime_checkable
class GuardianChildLinkRepository(Protocol):
    async def get_active_for_child(self, child_user_id: str) -> GuardianChildLink | None: ...

    async def list_active_for_child(self, child_user_id: str) -> list[GuardianChildLink]: ...

    async def get_active_for_pair(
        self, *, guardian_user_id: str, child_user_id: str
    ) -> GuardianChildLink | None: ...

    async def list_active_for_guardian(self, guardian_user_id: str) -> list[GuardianChildLink]: ...

    async def list_all_active(self) -> list[GuardianChildLink]: ...

    async def get_pending_for_guardian(
        self, guardian_user_id: str
    ) -> list[GuardianChildLink]: ...

    async def get_pending_for_pair(
        self, *, guardian_user_id: str, child_user_id: str
    ) -> GuardianChildLink | None: ...

    async def get_pending_for_child(
        self, child_user_id: str
    ) -> GuardianChildLink | None: ...

    async def upsert_pending(
        self, *, guardian_user_id: str, child_user_id: str, now: datetime
    ) -> GuardianChildLink: ...

    async def authorize_guardian(
        self, *, guardian_user_id: str, child_user_id: str, now: datetime
    ) -> GuardianChildLink: ...

    async def activate(
        self, *, guardian_user_id: str, child_user_id: str, now: datetime
    ) -> GuardianChildLink: ...

    async def upsert_active(
        self, *, guardian_user_id: str, child_user_id: str, now: datetime
    ) -> GuardianChildLink: ...


@runtime_checkable
class GuardChatStateRepository(Protocol):
    async def get(self, child_user_id: str, chat_id: str) -> GuardChatState | None: ...

    async def upsert_on_message(
        self,
        *,
        child_user_id: str,
        chat_id: str,
        observed_at: datetime,
        next_analysis_at: datetime,
        pending_since: datetime,
        next_analysis_reason: str | None = None,
        chat_name: str | None = None,
        connection_id: str | None = None,
        is_group: bool = False,
    ) -> GuardChatState: ...

    async def list_due(self, now: datetime, *, limit: int = 20) -> list[GuardChatState]: ...

    async def list_for_children(self, child_user_ids: list[str]) -> list[GuardChatState]: ...

    async def mark_processed(
        self,
        child_user_id: str,
        chat_id: str,
        target_version: int,
        *,
        decision: GuardDecision,
        analyzed_at: datetime,
        parent_should_know: bool | None = None,
    ) -> bool: ...

    async def defer_analysis(
        self,
        child_user_id: str,
        chat_id: str,
        target_version: int,
        *,
        retry_at: datetime,
        reason: str,
        analyzed_at: datetime,
    ) -> bool: ...


@runtime_checkable
class GuardAnalysisCommitRepository(Protocol):
    async def commit_if_current(
        self,
        *,
        child_user_id: str,
        chat_id: str,
        target_version: int,
        record: GuardAnalysisRecord,
    ) -> tuple[str, str | None]: ...


@runtime_checkable
class GuardAnalysisRepository(Protocol):
    async def save(self, record: GuardAnalysisRecord) -> str: ...

    async def list_needing_alert_evaluation(
        self, *, since: datetime, limit: int = 20
    ) -> list[GuardAnalysisRecord]: ...

    async def list_for_replay(
        self,
        *,
        child_user_id: str,
        chat_id: str,
        since: datetime,
        before_version: int,
    ) -> list[GuardAnalysisRecord]: ...

    async def list_for_children(
        self, *, child_user_ids: list[str], limit: int = 500
    ) -> list[GuardAnalysisRecord]: ...

    async def list_for_chat(
        self, *, child_user_id: str, chat_id: str, limit: int = 500
    ) -> list[GuardAnalysisRecord]: ...


@runtime_checkable
class GuardAlertRepository(Protocol):
    async def get_threshold_state(
        self, *, child_user_id: str, chat_id: str, threshold_family: str
    ) -> GuardThresholdState: ...

    async def list_threshold_states(
        self, *, child_user_id: str, chat_id: str
    ) -> list[GuardThresholdState]: ...

    async def save_threshold_state(self, state: GuardThresholdState) -> None: ...

    async def get_evaluation(self, analysis_result_id: str) -> GuardAlertEvaluation | None: ...

    async def save_evaluation(self, evaluation: GuardAlertEvaluation) -> bool: ...

    async def get_by_event_key(self, logical_event_key: str) -> GuardAlert | None: ...

    async def get_by_session_id(
        self, *, session_id: str, guardian_user_id: str
    ) -> GuardAlert | None: ...

    async def save_alert(self, alert: GuardAlert) -> bool: ...

    async def update_alert_status(self, alert_id: str, status: str) -> None: ...

    async def list_evaluations_for_chat(
        self, *, child_user_id: str, chat_id: str, limit: int = 500
    ) -> list[GuardAlertEvaluation]: ...

    async def list_alerts_for_chat(
        self, *, child_user_id: str, chat_id: str, limit: int = 100
    ) -> list[GuardAlert]: ...


class InMemoryGuardianChildLinkRepository:
    def __init__(self) -> None:
        self._links: dict[tuple[str, str], GuardianChildLink] = {}

    async def get_active_for_child(self, child_user_id: str) -> GuardianChildLink | None:
        links = await self.list_active_for_child(child_user_id)
        return links[0] if links else None

    async def list_active_for_child(self, child_user_id: str) -> list[GuardianChildLink]:
        return [
            link for link in self._links.values()
            if link.child_user_id == child_user_id and link.is_guard_enabled
        ]

    async def get_active_for_pair(
        self, *, guardian_user_id: str, child_user_id: str
    ) -> GuardianChildLink | None:
        link = self._links.get((guardian_user_id, child_user_id))
        return (
            link if link is not None and link.is_guard_enabled
            and link.guardian_authorized_at is not None else None
        )

    async def list_active_for_guardian(self, guardian_user_id: str) -> list[GuardianChildLink]:
        return [
            link for link in self._links.values()
            if link.guardian_user_id == guardian_user_id and link.is_guard_enabled
        ]

    async def list_all_active(self) -> list[GuardianChildLink]:
        return [link for link in self._links.values() if link.is_guard_enabled]

    async def get_pending_for_guardian(
        self, guardian_user_id: str
    ) -> list[GuardianChildLink]:
        return [
            link for link in self._links.values()
            if link.guardian_user_id == guardian_user_id
            and link.status is GuardianChildLinkStatus.PENDING
        ]

    async def get_pending_for_pair(
        self, *, guardian_user_id: str, child_user_id: str
    ) -> GuardianChildLink | None:
        link = self._links.get((guardian_user_id, child_user_id))
        return link if link is not None and link.status is GuardianChildLinkStatus.PENDING else None

    async def get_pending_for_child(
        self, child_user_id: str
    ) -> GuardianChildLink | None:
        for link in self._links.values():
            if link.child_user_id == child_user_id and link.status is GuardianChildLinkStatus.PENDING:
                return link
        return None

    def _upsert(
        self,
        *,
        guardian_user_id: str,
        child_user_id: str,
        now: datetime,
        status: GuardianChildLinkStatus,
        guardian_authorized_at: datetime | None = None,
        safety_enabled_at: datetime | None = None,
    ) -> GuardianChildLink:
        key = (guardian_user_id, child_user_id)
        existing = self._links.get(key)
        link = GuardianChildLink(
            id=existing.id if existing else f"link-{len(self._links) + 1}",
            guardian_user_id=guardian_user_id,
            child_user_id=child_user_id,
            status=status,
            guardian_authorized_at=(
                guardian_authorized_at
                if guardian_authorized_at is not None
                else existing.guardian_authorized_at if existing else None
            ),
            safety_enabled_at=safety_enabled_at,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        self._links[key] = link
        return link

    async def upsert_pending(
        self, *, guardian_user_id: str, child_user_id: str, now: datetime
    ) -> GuardianChildLink:
        existing = self._links.get((guardian_user_id, child_user_id))
        if existing is not None and existing.status is GuardianChildLinkStatus.ACTIVE:
            return existing
        return self._upsert(
            guardian_user_id=guardian_user_id,
            child_user_id=child_user_id,
            now=now,
            status=GuardianChildLinkStatus.PENDING,
        )

    async def authorize_guardian(
        self, *, guardian_user_id: str, child_user_id: str, now: datetime
    ) -> GuardianChildLink:
        return self._upsert(
            guardian_user_id=guardian_user_id,
            child_user_id=child_user_id,
            now=now,
            status=GuardianChildLinkStatus.PENDING,
            guardian_authorized_at=now,
        )

    async def activate(
        self, *, guardian_user_id: str, child_user_id: str, now: datetime
    ) -> GuardianChildLink:
        existing = self._links.get((guardian_user_id, child_user_id))
        if existing is None or existing.guardian_authorized_at is None:
            raise ValueError("guardian authorization is required before activation")
        return self._upsert(
            guardian_user_id=guardian_user_id,
            child_user_id=child_user_id,
            now=now,
            status=GuardianChildLinkStatus.ACTIVE,
            safety_enabled_at=now,
        )

    async def upsert_active(
        self, *, guardian_user_id: str, child_user_id: str, now: datetime
    ) -> GuardianChildLink:
        key = (guardian_user_id, child_user_id)
        existing = self._links.get(key)
        link = GuardianChildLink(
            id=existing.id if existing else f"link-{len(self._links) + 1}",
            guardian_user_id=guardian_user_id,
            child_user_id=child_user_id,
            status=GuardianChildLinkStatus.ACTIVE,
            guardian_authorized_at=existing.guardian_authorized_at if existing else now,
            safety_enabled_at=now,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        self._links[key] = link
        return link


class InMemoryGuardChatStateRepository:
    def __init__(self) -> None:
        self._states: dict[tuple[str, str], GuardChatState] = {}

    async def get(self, child_user_id: str, chat_id: str) -> GuardChatState | None:
        return self._states.get((child_user_id, chat_id))

    async def upsert_on_message(
        self,
        *,
        child_user_id: str,
        chat_id: str,
        observed_at: datetime,
        next_analysis_at: datetime,
        pending_since: datetime,
        next_analysis_reason: str | None = None,
        chat_name: str | None = None,
        connection_id: str | None = None,
        is_group: bool = False,
    ) -> GuardChatState:
        key = (child_user_id, chat_id)
        existing = self._states.get(key)
        if existing is None:
            state = GuardChatState(
                child_user_id=child_user_id,
                chat_id=chat_id,
                activity_version=1,
                last_message_at=observed_at,
                pending_since=pending_since,
                next_analysis_at=next_analysis_at,
                next_analysis_reason=next_analysis_reason,
                chat_name=chat_name,
                connection_id=connection_id,
                is_group=is_group,
            )
        elif observed_at >= existing.last_message_at:
            state = replace(
                existing,
                activity_version=existing.activity_version + 1,
                last_message_at=observed_at,
                pending_since=existing.pending_since or pending_since,
                next_analysis_at=min(existing.next_analysis_at or next_analysis_at, next_analysis_at),
                next_analysis_reason=existing.next_analysis_reason or next_analysis_reason,
                chat_name=chat_name or existing.chat_name,
                connection_id=connection_id or existing.connection_id,
                is_group=is_group,
                updated_at=datetime.now(timezone.utc),
            )
        else:
            state = existing
        self._states[key] = state
        return state

    async def list_due(self, now: datetime, *, limit: int = 20) -> list[GuardChatState]:
        due = [
            state for state in self._states.values()
            if state.next_analysis_at is not None
            and state.next_analysis_at <= now
            and state.activity_version > state.last_analyzed_version
        ]
        due.sort(key=lambda state: state.next_analysis_at or now)
        return due[:limit]

    async def list_for_children(self, child_user_ids: list[str]) -> list[GuardChatState]:
        allowed = set(child_user_ids)
        return [state for state in self._states.values() if state.child_user_id in allowed]

    async def mark_processed(
        self,
        child_user_id: str,
        chat_id: str,
        target_version: int,
        *,
        decision: GuardDecision,
        analyzed_at: datetime,
        parent_should_know: bool | None = None,
    ) -> bool:
        key = (child_user_id, chat_id)
        state = self._states.get(key)
        if state is None or state.activity_version != target_version:
            return False
        self._states[key] = replace(
            state,
            last_analyzed_version=target_version,
            last_decision=decision,
            last_parent_should_know=parent_should_know,
            last_analysis_at=analyzed_at,
            pending_since=None,
            next_analysis_at=None,
            next_analysis_reason=None,
            updated_at=analyzed_at,
        )
        return True

    async def defer_analysis(  # pragma: no cover - exercised by queue integration tests
        self, child_user_id: str, chat_id: str, target_version: int, *,
        retry_at: datetime, reason: str, analyzed_at: datetime,
    ) -> bool:
        key = (child_user_id, chat_id)
        state = self._states.get(key)
        if state is None or state.activity_version != target_version:
            return False
        self._states[key] = replace(
            state, next_analysis_at=retry_at, next_analysis_reason=reason,
            last_analysis_at=analyzed_at, updated_at=analyzed_at,
        )
        return True


class InMemoryGuardAnalysisCommitRepository:
    def __init__(
        self,
        state_repo: InMemoryGuardChatStateRepository,
        analysis_repo: InMemoryGuardAnalysisRepository,
    ) -> None:
        self._state = state_repo
        self._analyses = analysis_repo

    async def commit_if_current(
        self,
        *,
        child_user_id: str,
        chat_id: str,
        target_version: int,
        record: GuardAnalysisRecord,
    ) -> tuple[str, str | None]:
        state = await self._state.get(child_user_id, chat_id)
        if state is None:
            return "missing", None
        if state.activity_version != target_version:
            return "stale", None
        try:
            result_id = await self._analyses.save(record)
        except ValueError:
            result_id = record.id or ""
        committed = await self._state.mark_processed(
            child_user_id,
            chat_id,
            target_version,
            decision=record.decision,
            analyzed_at=datetime.now(timezone.utc),
        )
        if not committed:
            return "stale", None
        return "committed", result_id


class InMemoryGuardAnalysisRepository:
    def __init__(self) -> None:
        self.records: list[GuardAnalysisRecord] = []

    async def save(self, record: GuardAnalysisRecord) -> str:
        if any(
            existing.child_user_id == record.child_user_id
            and existing.chat_id == record.chat_id
            and existing.target_version == record.target_version
            and existing.analyzer_version == record.analyzer_version
            for existing in self.records
        ):
            raise ValueError("duplicate Guard analysis result")
        record_id = record.id or f"guard-result-{len(self.records) + 1}"
        self.records.append(
            replace(
                record,
                id=record_id,
                created_at=record.created_at or datetime.now(timezone.utc),
            )
        )
        return record_id

    async def list_needing_alert_evaluation(
        self, *, since: datetime, limit: int = 20
    ) -> list[GuardAnalysisRecord]:
        return [
            record
            for record in sorted(
                self.records,
                key=lambda item: item.created_at or datetime.min.replace(tzinfo=timezone.utc),
            )
            if record.created_at is not None and record.created_at >= since
        ][:limit]

    async def list_for_replay(
        self,
        *,
        child_user_id: str,
        chat_id: str,
        since: datetime,
        before_version: int,
    ) -> list[GuardAnalysisRecord]:
        return sorted(
            [
                record for record in self.records
                if record.child_user_id == child_user_id
                and record.chat_id == chat_id
                and record.target_version < before_version
                and record.created_at is not None
                and record.created_at >= since
            ],
            key=lambda record: (record.target_version, record.created_at or since),
        )

    async def list_for_children(
        self, *, child_user_ids: list[str], limit: int = 500
    ) -> list[GuardAnalysisRecord]:
        allowed = set(child_user_ids)
        rows = [record for record in self.records if record.child_user_id in allowed]
        rows.sort(key=lambda record: record.created_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return rows[:limit]

    async def list_for_chat(
        self, *, child_user_id: str, chat_id: str, limit: int = 500
    ) -> list[GuardAnalysisRecord]:
        rows = [
            record for record in self.records
            if record.child_user_id == child_user_id and record.chat_id == chat_id
        ]
        rows.sort(key=lambda record: record.created_at or datetime.min.replace(tzinfo=timezone.utc))
        return rows[-limit:]
