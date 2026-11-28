"""Tests for Baseline B daily windows and prior-concern context."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.domain.chat import Message
from echo_v2.domain.guard_exposure import ExposureAssessment, ExposureCategory
from echo_v2.domain.guard_simple import ChildRole, SafetyAssessment
from echo_v2.domain.media_enrichment import EnrichmentStatus, MessageEnrichment
from echo_v2.persistence.chat_repositories import InMemoryMessageRepository
from echo_v2.persistence.guard_simple_repositories import (
    InMemoryGuardSimpleAssessmentRepository,
)
from echo_v2.ports.whatsapp import MessageDirection
from echo_v2.services.guard_conversation import SimpleGuardConversationBuilder
from echo_v2.services.guard_simple_processor import GuardSimpleProcessor
from echo_v2.services.media_enrichment import EnrichedMessage

pytestmark = pytest.mark.asyncio
BASE = datetime(2026, 10, 16, 8, tzinfo=timezone.utc)


class FakeAnalyzer:
    def __init__(self, parent_should_know: bool | None = False) -> None:
        self.parent_should_know = parent_should_know
        self.conversations = []

    async def analyze(self, conversation):
        self.conversations.append(conversation)
        return SafetyAssessment(
            parent_should_know=self.parent_should_know,
            child_role=ChildRole.TARGET if self.parent_should_know else ChildRole.PARTICIPANT,
            summary="assessment",
            evidence_message_ids=(conversation.messages[0].id,) if conversation.messages else (),
            reason="reason",
            model="test",
            prompt_version="prompt",
            analyzer_version="analyzer",
        )


class FakeExposureAnalyzer:
    def __init__(self) -> None:
        self.conversations = []

    async def analyze(self, conversation):
        self.conversations.append(conversation)
        return ExposureAssessment(
            significant_exposure=True,
            category=ExposureCategory.BULLYING,
            summary="Another participant is repeatedly targeted.",
            evidence_message_ids=(conversation.messages[0].id,) if conversation.messages else (),
            reason="Repeated targeted humiliation.",
            model="test",
            prompt_version="prompt",
            analyzer_version="analyzer",
        )


class FailingExposureAnalyzer:
    async def analyze(self, conversation):
        raise RuntimeError("temporary exposure failure")


class PendingMediaEnrichment:
    async def enrich(self, messages):
        return [
            EnrichedMessage(
                message=item,
                enrichment=MessageEnrichment(
                    message_id=item.id,
                    enrichment_version="test-media",
                    status=EnrichmentStatus.PENDING,
                    next_retry_at=item.timestamp + timedelta(minutes=5) if item.timestamp else None,
                ),
                effective_text=item.text or "",
            )
            for item in messages
        ]


class FakeTimezoneResolver:
    def __init__(self, value: str | None) -> None:
        self.value = value

    async def get_timezone(self, child_user_id: str) -> str | None:
        return self.value


def message(
    message_id: str,
    at: datetime,
    *,
    direction: MessageDirection = MessageDirection.INBOUND,
    sender_id: str | None = "sender",
    chat_id: str = "group@g.us",
) -> Message:
    return Message(
        id=message_id,
        user_id="child",
        connection_id="connection",
        chat_id=chat_id,
        provider_message_id=f"provider-{message_id}",
        direction=direction,
        sender_id=sender_id,
        timestamp=at,
        text=message_id,
    )


async def test_builder_uses_local_calendar_day_and_newest_cap() -> None:
    messages = InMemoryMessageRepository()
    for index in range(101):
        await messages.save(message(f"m{index}", BASE - timedelta(minutes=index)))
    await messages.save(message("yesterday", BASE - timedelta(days=1)))

    built = await SimpleGuardConversationBuilder(messages, max_messages=100).build(
        child_user_id="child",
        child_name="Shay",
        connection_id="connection",
        chat_id="group@g.us",
        timezone_name="Asia/Jerusalem",
        as_of=BASE,
    )

    assert len(built.messages) == 100
    assert built.truncated
    assert built.omitted_message_count == 1
    assert built.messages[0].id == "m99"
    assert built.messages[-1].id == "m0"
    assert built.messages[-1].id != "yesterday"


async def test_invalid_timezone_falls_back_to_pilot_timezone() -> None:
    messages = InMemoryMessageRepository()
    await messages.save(message("m1", BASE))

    built = await SimpleGuardConversationBuilder(messages).build(
        child_user_id="child",
        child_name="Shay",
        connection_id="connection",
        chat_id="chat@c.us",
        timezone_name="not/a-zone",
        as_of=BASE,
    )

    assert built.timezone == "Asia/Jerusalem"


async def test_incomplete_media_without_text_is_unknown() -> None:
    messages = InMemoryMessageRepository()
    media = replace(message("media", BASE, sender_id="classmate"), text=None)
    await messages.save(media)
    builder = SimpleGuardConversationBuilder(
        messages,
        enrichment_service=PendingMediaEnrichment(),
    )
    analyzer = FakeAnalyzer(True)
    processor = GuardSimpleProcessor(
        message_repo=messages,
        analyzer=analyzer,
        assessment_repo=InMemoryGuardSimpleAssessmentRepository(),
        conversation_builder=builder,
    )

    result = await processor.process(
        child_user_id="child",
        child_name="Shay",
        connection_id="connection",
        chat_id="group@g.us",
        as_of=BASE,
    )

    assert result.parent_should_know is None
    assert result.reason == "incomplete_media_evidence"
    assert result.diagnostics["llm_skipped"] is True
    assert result.diagnostics["semantic_uncertainty"] is False
    assert analyzer.conversations == []


async def test_exposure_uses_same_conversation_without_prior_concern() -> None:
    messages = InMemoryMessageRepository()
    await messages.save(message("m1", BASE, sender_id="classmate"))
    assessments = InMemoryGuardSimpleAssessmentRepository()
    await assessments.save(SafetyAssessment(
        parent_should_know=True,
        child_role=ChildRole.TARGET,
        summary="Prior child concern",
        evidence_message_ids=("old",),
        reason="prior",
        child_user_id="child",
        connection_id="connection",
        chat_id="group@g.us",
        created_at=BASE - timedelta(days=1),
    ))
    exposure = FakeExposureAnalyzer()
    processor = GuardSimpleProcessor(
        message_repo=messages,
        analyzer=FakeAnalyzer(False),
        exposure_analyzer=exposure,
        assessment_repo=assessments,
    )

    child_result, exposure_result = await processor.process_with_exposure(
        child_user_id="child", child_name="Shay", connection_id="connection",
        chat_id="group@g.us", as_of=BASE,
    )

    assert child_result.parent_should_know is False
    assert exposure_result is not None and exposure_result.significant_exposure is True
    assert exposure.conversations[0].prior_concern is None
    assert exposure.conversations[0] is not None


async def test_exposure_failure_becomes_unknown_without_affecting_child() -> None:
    messages = InMemoryMessageRepository()
    await messages.save(message("m1", BASE, sender_id="classmate"))
    processor = GuardSimpleProcessor(
        message_repo=messages,
        analyzer=FakeAnalyzer(False),
        exposure_analyzer=FailingExposureAnalyzer(),
        assessment_repo=InMemoryGuardSimpleAssessmentRepository(),
    )
    child_result, exposure_result = await processor.process_with_exposure(
        child_user_id="child", child_name="Shay", connection_id="connection",
        chat_id="group@g.us", as_of=BASE,
    )
    assert child_result.parent_should_know is False
    assert exposure_result is not None
    assert exposure_result.significant_exposure is None
    assert exposure_result.reason == "exposure_analyzer_failed"


async def test_private_chat_does_not_run_exposure_analyzer() -> None:
    exposure = FakeExposureAnalyzer()
    processor = GuardSimpleProcessor(
        message_repo=InMemoryMessageRepository(),
        analyzer=FakeAnalyzer(False),
        exposure_analyzer=exposure,
        assessment_repo=InMemoryGuardSimpleAssessmentRepository(),
    )
    _, exposure_result = await processor.process_with_exposure(
        child_user_id="child", child_name="Shay", connection_id="connection",
        chat_id="private@c.us", as_of=BASE,
    )
    assert exposure_result is None
    assert exposure.conversations == []


async def test_group_can_concern_child_without_child_authored_message() -> None:
    messages = InMemoryMessageRepository()
    await messages.save(message("m1", BASE, sender_id="classmate"))

    built = await SimpleGuardConversationBuilder(messages).build(
        child_user_id="child",
        child_name="Shay",
        connection_id="connection",
        chat_id="group@g.us",
        timezone_name="Asia/Jerusalem",
        as_of=BASE + timedelta(minutes=1),
    )

    assert built.identity_verified
    assert built.messages[0].sender == "other"


async def test_missing_sender_attribution_fails_closed() -> None:
    messages = InMemoryMessageRepository()
    await messages.save(message("m1", BASE, sender_id=None))
    analyzer = FakeAnalyzer(True)
    assessments = InMemoryGuardSimpleAssessmentRepository()
    processor = GuardSimpleProcessor(
        message_repo=messages,
        analyzer=analyzer,
        assessment_repo=assessments,
        timezone_resolver=FakeTimezoneResolver("Asia/Jerusalem"),
    )

    result = await processor.process(
        child_user_id="child",
        child_name="Shay",
        connection_id="connection",
        chat_id="group@g.us",
        as_of=BASE + timedelta(minutes=1),
    )

    assert result.parent_should_know is None
    assert result.child_role is ChildRole.UNKNOWN
    assert not result.identity_verified
    assert result.input_message_ids == ("m1",)
    assert result.input_content_hash
    assert assessments.assessments == []


async def test_prior_actionable_assessment_is_context_not_forced_decision() -> None:
    messages = InMemoryMessageRepository()
    await messages.save(message("m1", BASE, direction=MessageDirection.OUTBOUND, sender_id=None))
    assessments = InMemoryGuardSimpleAssessmentRepository()
    await assessments.save(
        SafetyAssessment(
            parent_should_know=True,
            child_role=ChildRole.TARGET,
            summary="Yesterday's concern",
            evidence_message_ids=("old",),
            reason="prior",
            child_user_id="child",
            connection_id="connection",
            chat_id="group@g.us",
            created_at=BASE - timedelta(days=1),
        )
    )
    analyzer = FakeAnalyzer(False)
    processor = GuardSimpleProcessor(
        message_repo=messages,
        analyzer=analyzer,
        assessment_repo=assessments,
        timezone_resolver=FakeTimezoneResolver("Asia/Jerusalem"),
    )

    result = await processor.process(
        child_user_id="child",
        child_name="Shay",
        connection_id="connection",
        chat_id="group@g.us",
        as_of=BASE + timedelta(minutes=1),
    )

    assert result.parent_should_know is False
    assert analyzer.conversations[0].prior_concern is not None
    assert analyzer.conversations[0].prior_concern.evidence_message_ids == ("old",)


async def test_actionable_prior_concern_takes_precedence_over_uncertainty() -> None:
    messages = InMemoryMessageRepository()
    await messages.save(message("m1", BASE, direction=MessageDirection.OUTBOUND, sender_id=None))
    assessments = InMemoryGuardSimpleAssessmentRepository()
    uncertain = SafetyAssessment(
        parent_should_know=None,
        child_role=ChildRole.PARTICIPANT,
        summary="Ambiguous object exchange",
        evidence_message_ids=("ambiguous-object",),
        reason="A realistic dangerous interpretation remains unresolved.",
        child_user_id="child",
        connection_id="connection",
        chat_id="group@g.us",
        created_at=BASE - timedelta(hours=22),
        diagnostics={"semantic_uncertainty": True},
    )
    await assessments.save(uncertain)
    await assessments.save(
        replace(
            uncertain,
            parent_should_know=True,
            child_role=ChildRole.TARGET,
            summary="Actionable threat",
            evidence_message_ids=("threat",),
            created_at=BASE - timedelta(hours=21),
            first_detected_at=BASE - timedelta(hours=21),
            last_evidence_at=BASE - timedelta(hours=21),
            diagnostics={},
        )
    )
    analyzer = FakeAnalyzer(False)
    processor = GuardSimpleProcessor(
        message_repo=messages,
        analyzer=analyzer,
        assessment_repo=assessments,
    )

    await processor.process(
        child_user_id="child",
        connection_id="connection",
        chat_id="group@g.us",
        child_name="Shay",
        as_of=BASE,
    )

    prior = analyzer.conversations[0].prior_concern
    assert prior is not None
    assert prior.parent_should_know is True
    assert prior.evidence_message_ids == ("threat",)


async def test_previous_day_semantic_uncertainty_is_carried_as_context() -> None:
    messages = InMemoryMessageRepository()
    await messages.save(message("m1", BASE, direction=MessageDirection.OUTBOUND, sender_id=None))
    assessments = InMemoryGuardSimpleAssessmentRepository()
    await assessments.save(
        SafetyAssessment(
            parent_should_know=None,
            child_role=ChildRole.PARTICIPANT,
            summary="Ambiguous object exchange",
            evidence_message_ids=("ambiguous-object",),
            reason="A realistic dangerous interpretation remains unresolved.",
            child_user_id="child",
            connection_id="connection",
            chat_id="group@g.us",
            created_at=BASE - timedelta(days=1),
            diagnostics={"semantic_uncertainty": True},
        )
    )
    analyzer = FakeAnalyzer(True)
    processor = GuardSimpleProcessor(
        message_repo=messages,
        analyzer=analyzer,
        assessment_repo=assessments,
    )

    result = await processor.process(
        child_user_id="child",
        connection_id="connection",
        chat_id="group@g.us",
        child_name="Shay",
        as_of=BASE,
    )

    prior = analyzer.conversations[0].prior_concern
    assert prior is not None
    assert prior.parent_should_know is None
    assert prior.evidence_message_ids == ("ambiguous-object",)
    assert result.first_detected_at == BASE


async def test_uncertain_context_does_not_cross_more_than_one_day_boundary() -> None:
    messages = InMemoryMessageRepository()
    await messages.save(message("m1", BASE, direction=MessageDirection.OUTBOUND, sender_id=None))
    assessments = InMemoryGuardSimpleAssessmentRepository()
    await assessments.save(
        SafetyAssessment(
            parent_should_know=None,
            child_role=ChildRole.PARTICIPANT,
            summary="Ambiguous object exchange",
            evidence_message_ids=("ambiguous-object",),
            reason="A realistic dangerous interpretation remains unresolved.",
            child_user_id="child",
            connection_id="connection",
            chat_id="group@g.us",
            created_at=BASE - timedelta(days=3),
            diagnostics={"semantic_uncertainty": True},
        )
    )
    analyzer = FakeAnalyzer(False)
    processor = GuardSimpleProcessor(
        message_repo=messages,
        analyzer=analyzer,
        assessment_repo=assessments,
    )

    await processor.process(
        child_user_id="child",
        connection_id="connection",
        chat_id="group@g.us",
        child_name="Shay",
        as_of=BASE,
    )

    assert analyzer.conversations[0].prior_concern is None


async def test_uncertain_context_uses_immediate_previous_local_day_boundary() -> None:
    messages = InMemoryMessageRepository()
    await messages.save(message("m1", BASE, direction=MessageDirection.OUTBOUND, sender_id=None))
    assessments = InMemoryGuardSimpleAssessmentRepository()
    await assessments.save(
        SafetyAssessment(
            parent_should_know=None,
            child_role=ChildRole.PARTICIPANT,
            summary="Ambiguous object exchange",
            evidence_message_ids=("ambiguous-object",),
            reason="A realistic dangerous interpretation remains unresolved.",
            child_user_id="child",
            connection_id="connection",
            chat_id="group@g.us",
            created_at=datetime(2026, 10, 14, 20, 59, tzinfo=timezone.utc),
            diagnostics={"semantic_uncertainty": True},
        )
    )
    await assessments.save(
        SafetyAssessment(
            parent_should_know=None,
            child_role=ChildRole.PARTICIPANT,
            summary="Ambiguous object exchange",
            evidence_message_ids=("ambiguous-object-newer",),
            reason="A realistic dangerous interpretation remains unresolved.",
            child_user_id="child",
            connection_id="connection",
            chat_id="group@g.us",
            created_at=datetime(2026, 10, 14, 21, 0, tzinfo=timezone.utc),
            diagnostics={"semantic_uncertainty": True},
        )
    )
    analyzer = FakeAnalyzer(False)
    processor = GuardSimpleProcessor(
        message_repo=messages,
        analyzer=analyzer,
        assessment_repo=assessments,
    )

    await processor.process(
        child_user_id="child",
        connection_id="connection",
        chat_id="group@g.us",
        child_name="Shay",
        as_of=BASE,
    )

    prior = analyzer.conversations[0].prior_concern
    assert prior is not None
    assert prior.evidence_message_ids == ("ambiguous-object-newer",)


async def test_definitive_assessment_supersedes_previous_day_uncertainty() -> None:
    messages = InMemoryMessageRepository()
    await messages.save(message("m1", BASE, direction=MessageDirection.OUTBOUND, sender_id=None))
    assessments = InMemoryGuardSimpleAssessmentRepository()
    uncertain = SafetyAssessment(
        parent_should_know=None,
        child_role=ChildRole.PARTICIPANT,
        summary="Ambiguous object exchange",
        evidence_message_ids=("ambiguous-object",),
        reason="A realistic dangerous interpretation remains unresolved.",
        child_user_id="child",
        connection_id="connection",
        chat_id="group@g.us",
        created_at=BASE - timedelta(days=1),
        diagnostics={"semantic_uncertainty": True},
    )
    await assessments.save(uncertain)
    await assessments.save(
        replace(
            uncertain,
            parent_should_know=False,
            created_at=BASE - timedelta(hours=20),
            diagnostics={},
        )
    )
    analyzer = FakeAnalyzer(False)
    processor = GuardSimpleProcessor(
        message_repo=messages,
        analyzer=analyzer,
        assessment_repo=assessments,
    )

    await processor.process(
        child_user_id="child",
        connection_id="connection",
        chat_id="group@g.us",
        child_name="Shay",
        as_of=BASE,
    )

    assert analyzer.conversations[0].prior_concern is None


async def test_older_definitive_assessment_does_not_supersede_newer_uncertainty() -> None:
    messages = InMemoryMessageRepository()
    await messages.save(message("m1", BASE, direction=MessageDirection.OUTBOUND, sender_id=None))
    assessments = InMemoryGuardSimpleAssessmentRepository()
    definitive = SafetyAssessment(
        parent_should_know=False,
        child_role=ChildRole.PARTICIPANT,
        summary="Earlier resolved conversation",
        evidence_message_ids=("resolved",),
        reason="No supported concern in the earlier evidence.",
        child_user_id="child",
        connection_id="connection",
        chat_id="group@g.us",
        created_at=BASE - timedelta(hours=22),
    )
    await assessments.save(definitive)
    await assessments.save(
        replace(
            definitive,
            parent_should_know=None,
            summary="Ambiguous object exchange",
            evidence_message_ids=("ambiguous-object",),
            reason="A realistic dangerous interpretation remains unresolved.",
            created_at=BASE - timedelta(hours=20),
            diagnostics={"semantic_uncertainty": True},
        )
    )
    analyzer = FakeAnalyzer(False)
    processor = GuardSimpleProcessor(
        message_repo=messages,
        analyzer=analyzer,
        assessment_repo=assessments,
    )

    await processor.process(
        child_user_id="child",
        connection_id="connection",
        chat_id="group@g.us",
        child_name="Shay",
        as_of=BASE,
    )

    prior = analyzer.conversations[0].prior_concern
    assert prior is not None
    assert prior.evidence_message_ids == ("ambiguous-object",)


async def test_operational_null_does_not_resolve_or_extend_uncertainty_chain() -> None:
    messages = InMemoryMessageRepository()
    await messages.save(message("m1", BASE, direction=MessageDirection.OUTBOUND, sender_id=None))
    assessments = InMemoryGuardSimpleAssessmentRepository()
    uncertain = SafetyAssessment(
        parent_should_know=None,
        child_role=ChildRole.PARTICIPANT,
        summary="Ambiguous object exchange",
        evidence_message_ids=("ambiguous-object",),
        reason="A realistic dangerous interpretation remains unresolved.",
        child_user_id="child",
        connection_id="connection",
        chat_id="group@g.us",
        created_at=BASE - timedelta(hours=20),
        diagnostics={"semantic_uncertainty": True},
    )
    await assessments.save(uncertain)
    await assessments.save(
        replace(
            uncertain,
            evidence_message_ids=(),
            created_at=BASE - timedelta(hours=19),
            diagnostics={"semantic_uncertainty": False},
        )
    )
    analyzer = FakeAnalyzer(False)
    processor = GuardSimpleProcessor(
        message_repo=messages,
        analyzer=analyzer,
        assessment_repo=assessments,
    )

    await processor.process(
        child_user_id="child",
        connection_id="connection",
        chat_id="group@g.us",
        child_name="Shay",
        as_of=BASE,
    )

    prior = analyzer.conversations[0].prior_concern
    assert prior is not None
    assert prior.evidence_message_ids == ("ambiguous-object",)


async def test_resolved_positive_assessment_supersedes_previous_day_uncertainty() -> None:
    messages = InMemoryMessageRepository()
    await messages.save(message("m1", BASE, direction=MessageDirection.OUTBOUND, sender_id=None))
    assessments = InMemoryGuardSimpleAssessmentRepository()
    uncertain = SafetyAssessment(
        parent_should_know=None,
        child_role=ChildRole.PARTICIPANT,
        summary="Ambiguous object exchange",
        evidence_message_ids=("ambiguous-object",),
        reason="A realistic dangerous interpretation remains unresolved.",
        child_user_id="child",
        connection_id="connection",
        chat_id="group@g.us",
        created_at=BASE - timedelta(days=1),
        diagnostics={"semantic_uncertainty": True},
    )
    await assessments.save(uncertain)
    await assessments.save(
        replace(
            uncertain,
            parent_should_know=True,
            created_at=BASE - timedelta(hours=20),
            resolved_at=BASE - timedelta(hours=19),
            diagnostics={},
        )
    )
    analyzer = FakeAnalyzer(False)
    processor = GuardSimpleProcessor(
        message_repo=messages,
        analyzer=analyzer,
        assessment_repo=assessments,
    )

    await processor.process(
        child_user_id="child",
        connection_id="connection",
        chat_id="group@g.us",
        child_name="Shay",
        as_of=BASE,
    )

    assert analyzer.conversations[0].prior_concern is None


async def test_same_day_parent_concern_is_not_reused_as_recursive_context() -> None:
    messages = InMemoryMessageRepository()
    await messages.save(message("m1", BASE, direction=MessageDirection.OUTBOUND, sender_id=None))
    assessments = InMemoryGuardSimpleAssessmentRepository()
    await assessments.save(
        SafetyAssessment(
            parent_should_know=True,
            child_role=ChildRole.TARGET,
            summary="same-day concern",
            evidence_message_ids=("old",),
            reason="prior",
            child_user_id="child",
            connection_id="connection",
            chat_id="group@g.us",
            created_at=BASE - timedelta(hours=1),
            first_detected_at=BASE - timedelta(hours=2),
            last_evidence_at=BASE - timedelta(hours=1),
        )
    )
    analyzer = FakeAnalyzer(False)
    processor = GuardSimpleProcessor(
        message_repo=messages,
        analyzer=analyzer,
        assessment_repo=assessments,
    )

    result = await processor.process(
        child_user_id="child",
        connection_id="connection",
        chat_id="group@g.us",
        child_name="Shay",
        as_of=BASE,
    )

    assert result.parent_should_know is False
    assert analyzer.conversations[0].prior_concern is None


async def test_same_day_uncertain_context_remains_in_followup_window() -> None:
    messages = InMemoryMessageRepository()
    await messages.save(message("ambiguous-object", BASE - timedelta(hours=2)))
    await messages.save(message("resolving-followup", BASE, direction=MessageDirection.OUTBOUND, sender_id=None))
    assessments = InMemoryGuardSimpleAssessmentRepository()
    await assessments.save(
        SafetyAssessment(
            parent_should_know=None,
            child_role=ChildRole.UNKNOWN,
            summary="Ambiguous object exchange",
            evidence_message_ids=("ambiguous-object",),
            reason="Insufficient evidence",
            child_user_id="child",
            connection_id="connection",
            chat_id="group@g.us",
            created_at=BASE - timedelta(hours=1),
        )
    )
    analyzer = FakeAnalyzer(False)
    processor = GuardSimpleProcessor(
        message_repo=messages,
        analyzer=analyzer,
        assessment_repo=assessments,
    )

    await processor.process(
        child_user_id="child",
        connection_id="connection",
        chat_id="group@g.us",
        child_name="Shay",
        as_of=BASE,
    )

    conversation = analyzer.conversations[0]
    assert conversation.prior_concern is None
    assert [message.id for message in conversation.messages] == [
        "ambiguous-object",
        "resolving-followup",
    ]


async def test_llm_uncertain_result_is_marked_as_semantic_uncertainty() -> None:
    messages = InMemoryMessageRepository()
    await messages.save(message("m1", BASE))
    processor = GuardSimpleProcessor(
        message_repo=messages,
        analyzer=FakeAnalyzer(None),
        assessment_repo=InMemoryGuardSimpleAssessmentRepository(),
    )

    result = await processor.process(
        child_user_id="child", connection_id="connection", chat_id="group@g.us",
        child_name="Shay", as_of=BASE,
    )

    assert result.parent_should_know is None
    assert result.diagnostics["semantic_uncertainty"] is True


async def test_missing_child_name_fails_closed() -> None:
    messages = InMemoryMessageRepository()
    await messages.save(message("m1", BASE))
    processor = GuardSimpleProcessor(
        message_repo=messages,
        analyzer=FakeAnalyzer(True),
        assessment_repo=InMemoryGuardSimpleAssessmentRepository(),
    )

    result = await processor.process(
        child_user_id="child", connection_id="connection", chat_id="group@g.us",
        child_name="", as_of=BASE,
    )

    assert result.parent_should_know is None
    assert not result.identity_verified
    assert result.diagnostics["semantic_uncertainty"] is False


async def test_empty_day_is_unknown_and_does_not_create_false_result() -> None:
    messages = InMemoryMessageRepository()
    analyzer = FakeAnalyzer(False)
    assessments = InMemoryGuardSimpleAssessmentRepository()
    processor = GuardSimpleProcessor(
        message_repo=messages,
        analyzer=analyzer,
        assessment_repo=assessments,
    )

    result = await processor.process(
        child_user_id="child",
        child_name="Shay",
        connection_id="connection",
        chat_id="chat@c.us",
        as_of=BASE,
    )

    assert result.parent_should_know is None
    assert result.diagnostics["semantic_uncertainty"] is False


async def test_processor_rejects_non_positive_message_cap() -> None:
    with pytest.raises(ValueError, match="max_messages"):
        GuardSimpleProcessor(
            message_repo=InMemoryMessageRepository(),
            analyzer=FakeAnalyzer(),
            assessment_repo=InMemoryGuardSimpleAssessmentRepository(),
            max_messages=0,
        )


async def test_invalid_configured_timezone_uses_fallback() -> None:
    messages = InMemoryMessageRepository()
    await messages.save(message("m1", BASE))
    analyzer = FakeAnalyzer()
    processor = GuardSimpleProcessor(
        message_repo=messages,
        analyzer=analyzer,
        assessment_repo=InMemoryGuardSimpleAssessmentRepository(),
        timezone_resolver=FakeTimezoneResolver("not/a-timezone"),
    )

    result = await processor.process(
        child_user_id="child",
        child_name="Shay",
        connection_id="connection",
        chat_id="group@g.us",
        as_of=BASE + timedelta(minutes=1),
    )

    assert result.timezone == "Asia/Jerusalem"


async def test_in_memory_history_can_list_and_resolve_assessments() -> None:
    repository = InMemoryGuardSimpleAssessmentRepository()
    assessment = SafetyAssessment(
        parent_should_know=True,
        child_role=ChildRole.TARGET,
        summary="concern",
        evidence_message_ids=("m1",),
        reason="reason",
        child_user_id="child",
        chat_id="chat@c.us",
        created_at=BASE,
    )
    assessment_id = await repository.save(assessment)

    assert len(await repository.list_for_chat(child_user_id="child", chat_id="chat@c.us")) == 1
    assert await repository.resolve(assessment_id="missing", resolved_by="tester") is False
    assert await repository.resolve(assessment_id=assessment_id, resolved_by="tester")
    assert await repository.latest_unresolved(child_user_id="child", chat_id="chat@c.us") is None
