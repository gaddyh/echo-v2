"""Build Guard inputs from persisted messages without exposing identities to the LLM."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from echo_v2.domain.guard_simple import (
    PriorConcern,
    SimpleConversation,
    SimpleConversationMessage,
)
from echo_v2.persistence.chat_repositories import MessageRepository
from echo_v2.services.guard_analyzer import GuardAnalysisInput, GuardMessage

__all__ = [
    "GuardConversation",
    "GuardConversationBuilder",
    "ParticipantDiagnostic",
    "SimpleGuardConversationBuilder",
]


@dataclass(frozen=True)
class ParticipantDiagnostic:
    canonical_id: str
    role: str
    display_name: str | None = None


@dataclass(frozen=True)
class GuardConversation:
    input: GuardAnalysisInput
    participant_diagnostics: tuple[ParticipantDiagnostic, ...] = ()
    connection_id: str = ""
    latest_message_at: datetime | None = None


class GuardConversationBuilder:
    def __init__(self, message_repo: MessageRepository, *, max_messages: int = 100) -> None:
        self._messages = message_repo
        self._max_messages = max_messages

    async def build(
        self,
        *,
        child_user_id: str,
        chat_id: str,
        expected_connection_id: str | None = None,
    ) -> GuardConversation:
        messages = await self._messages.list_recent_for_chat(
            user_id=child_user_id,
            chat_id=chat_id,
            limit=self._max_messages,
        )
        connections = {message.connection_id for message in messages}
        if expected_connection_id is not None:
            connections.add(expected_connection_id)
        if len(connections) != 1:
            raise ValueError(
                f"Guard chat {child_user_id}/{chat_id} has inconsistent connections: "
                f"{sorted(connections)}"
            )
        connection_id = next(iter(connections))

        participant_ids = sorted(
            {
                message.sender_id
                for message in messages
                if message.direction.value == "inbound" and message.sender_id
            }
        )
        role_by_sender = {
            sender_id: ("other" if index == 0 else f"other_{index + 1}")
            for index, sender_id in enumerate(participant_ids)
        }
        unknown_index = 0
        diagnostics: list[ParticipantDiagnostic] = []
        guard_messages: list[GuardMessage] = []
        for message in messages:
            if message.direction.value == "outbound":
                role = "child"
            elif message.sender_id:
                role = role_by_sender[message.sender_id]
            else:
                unknown_index += 1
                role = f"unknown_participant_{unknown_index}"
            if message.sender_id:
                diagnostics.append(
                    ParticipantDiagnostic(message.sender_id, role, message.sender_name)
                )
            guard_messages.append(
                GuardMessage(
                    id=message.id,
                    sender=role,
                    text=message.text or "",
                )
            )

        return GuardConversation(
            input=GuardAnalysisInput(
                child_id=child_user_id,
                chat_id=chat_id,
                messages=tuple(guard_messages),
            ),
            participant_diagnostics=tuple(diagnostics),
            connection_id=connection_id,
            latest_message_at=max((message.timestamp for message in messages), default=None),
        )


class SimpleGuardConversationBuilder:
    """Build a child-aware, bounded calendar-day Baseline B context."""

    def __init__(self, message_repo: MessageRepository, *, max_messages: int = 100) -> None:
        if max_messages < 1:
            raise ValueError("max_messages must be positive")
        self._messages = message_repo
        self._max_messages = max_messages

    async def build(
        self,
        *,
        child_user_id: str,
        connection_id: str,
        chat_id: str,
        timezone_name: str,
        as_of: datetime,
        prior_concern: PriorConcern | None = None,
    ) -> SimpleConversation:
        zone = _zone_or_fallback(timezone_name)
        current = _aware_utc(as_of).astimezone(zone)
        local_start = datetime.combine(current.date(), time.min, tzinfo=zone)
        window_start = local_start.astimezone(timezone.utc)
        window_end = _aware_utc(as_of)
        window = await self._messages.list_for_chat_window(
            user_id=child_user_id,
            chat_id=chat_id,
            since=window_start,
            until=window_end + timedelta(microseconds=1),
            limit=self._max_messages,
        )
        messages = list(window.messages)
        connections = set(window.connection_ids)
        identity_reasons: list[str] = []
        if any(connection != connection_id for connection in connections):
            identity_reasons.append("message connection does not match monitored connection")
        if window.unknown_sender_count:
            identity_reasons.append("inbound sender attribution is missing")

        participant_ids = sorted(
            {
                message.sender_id
                for message in messages
                if message.direction.value == "inbound" and message.sender_id
            }
        )
        role_by_sender = {
            sender_id: ("other" if index == 0 else f"other_{index + 1}")
            for index, sender_id in enumerate(participant_ids)
        }
        unknown_index = 0
        simple_messages: list[SimpleConversationMessage] = []
        for message in messages:
            if message.direction.value == "outbound":
                role = "child"
            elif message.sender_id:
                role = role_by_sender[message.sender_id]
            else:
                unknown_index += 1
                role = f"unknown_participant_{unknown_index}"
                identity_reasons.append("inbound sender attribution is missing")
            simple_messages.append(
                SimpleConversationMessage(
                    id=message.id,
                    sender=role,
                    text=message.text or "",
                )
            )

        return SimpleConversation(
            child_user_id=child_user_id,
            chat_id=chat_id,
            chat_type="group" if chat_id.endswith("@g.us") else "private",
            connection_id=connection_id,
            timezone=zone.key,
            window_start=window_start,
            window_end=window_end,
            monitored_child_role="child",
            messages=tuple(simple_messages),
            truncated=window.truncated,
            omitted_message_count=window.omitted_message_count,
            identity_verified=not identity_reasons,
            identity_reason="; ".join(dict.fromkeys(identity_reasons)) or None,
            prior_concern=prior_concern,
        )


def _aware_utc(value: datetime) -> datetime:
    return (value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)).astimezone(
        timezone.utc
    )


def _zone_or_fallback(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Asia/Jerusalem")
