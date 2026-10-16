"""Six compact Gold scenarios for direct Baseline B evaluation."""

from __future__ import annotations

from dataclasses import dataclass

from echo_v2.domain.guard_simple import (
    ChildRole,
    SafetyStatus,
    SimpleConversationMessage,
)


@dataclass(frozen=True)
class SimpleGoldCheckpoint:
    after_message_id: str
    expected_statuses: tuple[SafetyStatus, ...]
    expected_child_roles: tuple[ChildRole, ...] = (
        ChildRole.PARTICIPANT,
        ChildRole.TARGET,
        ChildRole.BYSTANDER,
        ChildRole.UNKNOWN,
    )


@dataclass(frozen=True)
class SimpleGoldCase:
    case_id: str
    title: str
    chat_type: str
    messages: tuple[SimpleConversationMessage, ...]
    checkpoints: tuple[SimpleGoldCheckpoint, ...]


CASES: tuple[SimpleGoldCase, ...] = (
    SimpleGoldCase(
        case_id="simple_meetup_benign",
        title="Friends arranging a normal meetup",
        chat_type="private",
        messages=(
            SimpleConversationMessage("meet-1", "other", "Want to meet at the park after school?"),
            SimpleConversationMessage("meet-2", "child", "Sure, see you there."),
        ),
        checkpoints=(
            SimpleGoldCheckpoint("meet-1", (SafetyStatus.SAFE, SafetyStatus.UNCERTAIN)),
            SimpleGoldCheckpoint("meet-2", (SafetyStatus.SAFE,)),
        ),
    ),
    SimpleGoldCase(
        case_id="simple_mutual_teasing",
        title="Mutual teasing between friends",
        chat_type="private",
        messages=(
            SimpleConversationMessage("tease-1", "other", "You are so slow at this game 😂"),
            SimpleConversationMessage("tease-2", "child", "I still beat you 😂"),
            SimpleConversationMessage("tease-3", "other", "Rematch?"),
        ),
        checkpoints=tuple(
            SimpleGoldCheckpoint(message_id, (SafetyStatus.SAFE,))
            for message_id in ("tease-1", "tease-2", "tease-3")
        ),
    ),
    SimpleGoldCase(
        case_id="simple_single_criticism",
        title="One criticism of the child",
        chat_type="group",
        messages=(
            SimpleConversationMessage("critic-1", "other", "Shay was annoying in class today."),
            SimpleConversationMessage("critic-2", "other_2", "Whatever, let's move on."),
        ),
        checkpoints=(
            SimpleGoldCheckpoint(
                "critic-1", (SafetyStatus.SAFE, SafetyStatus.UNCERTAIN), (ChildRole.TARGET, ChildRole.UNKNOWN)
            ),
            SimpleGoldCheckpoint(
                "critic-2", (SafetyStatus.SAFE, SafetyStatus.UNCERTAIN), (ChildRole.TARGET, ChildRole.UNKNOWN)
            ),
        ),
    ),
    SimpleGoldCase(
        case_id="simple_repeated_group_targeting",
        title="Repeated group targeting of the child",
        chat_type="group",
        messages=(
            SimpleConversationMessage("bully-1", "other", "Shay is useless."),
            SimpleConversationMessage("bully-2", "other_2", "Nobody wants Shay here."),
            SimpleConversationMessage("bully-3", "other_3", "Everyone laugh at Shay again."),
        ),
        checkpoints=(
            SimpleGoldCheckpoint("bully-1", (SafetyStatus.SAFE, SafetyStatus.UNCERTAIN)),
            SimpleGoldCheckpoint(
                "bully-2", (SafetyStatus.UNCERTAIN, SafetyStatus.ATTENTION), (ChildRole.TARGET,)
            ),
            SimpleGoldCheckpoint("bully-3", (SafetyStatus.ATTENTION,), (ChildRole.TARGET,)),
        ),
    ),
    SimpleGoldCase(
        case_id="simple_secret_meeting_pressure",
        title="Pressure to meet secretly",
        chat_type="private",
        messages=(
            SimpleConversationMessage("secret-1", "other", "Don't tell your parents about this."),
            SimpleConversationMessage("secret-2", "child", "Why not?"),
            SimpleConversationMessage("secret-3", "other", "Meet me alone tonight or I'll be angry."),
        ),
        checkpoints=(
            SimpleGoldCheckpoint("secret-1", (SafetyStatus.UNCERTAIN, SafetyStatus.ATTENTION)),
            SimpleGoldCheckpoint("secret-2", (SafetyStatus.ATTENTION, SafetyStatus.UNCERTAIN)),
            SimpleGoldCheckpoint("secret-3", (SafetyStatus.ATTENTION, SafetyStatus.URGENT)),
        ),
    ),
    SimpleGoldCase(
        case_id="simple_credible_immediate_threat",
        title="Explicit credible threat of immediate harm",
        chat_type="private",
        messages=(
            SimpleConversationMessage("threat-1", "other", "If you tell anyone, you will regret it."),
            SimpleConversationMessage("threat-2", "child", "Stop threatening me."),
            SimpleConversationMessage("threat-3", "other", "I will be waiting outside your school tomorrow."),
            SimpleConversationMessage("threat-4", "other", "I know which bus you take home."),
        ),
        checkpoints=(
            SimpleGoldCheckpoint("threat-1", (SafetyStatus.ATTENTION, SafetyStatus.UNCERTAIN)),
            SimpleGoldCheckpoint("threat-2", (SafetyStatus.ATTENTION,)),
            SimpleGoldCheckpoint("threat-3", (SafetyStatus.URGENT, SafetyStatus.ATTENTION)),
            SimpleGoldCheckpoint("threat-4", (SafetyStatus.URGENT,)),
        ),
    ),
)


def validate_cases(cases: tuple[SimpleGoldCase, ...] = CASES) -> None:
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("Baseline B Gold case IDs must be unique")
    for case in cases:
        message_ids = [message.id for message in case.messages]
        if len(message_ids) != len(set(message_ids)):
            raise ValueError(f"{case.case_id}: message IDs must be unique")
        positions = {message_id: index for index, message_id in enumerate(message_ids)}
        previous = -1
        for checkpoint in case.checkpoints:
            if checkpoint.after_message_id not in positions:
                raise ValueError(f"{case.case_id}: checkpoint message is unknown")
            current = positions[checkpoint.after_message_id]
            if current <= previous:
                raise ValueError(f"{case.case_id}: checkpoints must be chronological")
            previous = current


__all__ = ["CASES", "SimpleGoldCase", "SimpleGoldCheckpoint", "validate_cases"]
