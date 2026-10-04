"""Build Guard inputs from persisted messages without exposing identities to the LLM."""

from __future__ import annotations

from dataclasses import dataclass

from echo_v2.persistence.chat_repositories import MessageRepository
from echo_v2.services.guard_analyzer import GuardAnalysisInput, GuardMessage

__all__ = ["GuardConversation", "GuardConversationBuilder", "ParticipantDiagnostic"]


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
        )
