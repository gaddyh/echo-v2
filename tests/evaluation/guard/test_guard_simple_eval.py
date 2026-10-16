"""API-backed, prefix-by-prefix evaluation for Baseline B."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from echo_v2.domain.guard_simple import SimpleConversation
from echo_v2.services.guard_simple_analyzer import SimpleSafetyAnalyzer
from tests.evaluation.guard.guard_simple_cases import (
    CASES,
    SimpleGoldCase,
    validate_cases,
)

pytestmark = [
    pytest.mark.eval_guard,
    pytest.mark.skipif(
        not os.environ.get("OPENAI_API_KEY"),
        reason="OPENAI_API_KEY not set — eval requires real API access",
    ),
]

NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


def _prefix(case: SimpleGoldCase, after_message_id: str) -> tuple:
    messages = []
    for message in case.messages:
        messages.append(message)
        if message.id == after_message_id:
            break
    return tuple(messages)


@pytest.fixture
def analyzer() -> SimpleSafetyAnalyzer:
    from openai import AsyncOpenAI

    return SimpleSafetyAnalyzer(
        client=AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", "")),
        model=os.environ.get("GUARD_SIMPLE_LLM_MODEL", os.environ.get("LLM_MODEL_NAME", "gpt-4.1")),
    )


async def test_baseline_b_gold_prefixes(analyzer: SimpleSafetyAnalyzer) -> None:
    validate_cases()
    failures: list[str] = []
    for case in CASES:
        for checkpoint in case.checkpoints:
            conversation = SimpleConversation(
                child_user_id="gold-child",
                child_name="Shay",
                chat_id=case.case_id,
                chat_type=case.chat_type,
                connection_id="gold-connection",
                timezone="Asia/Jerusalem",
                window_start=NOW.replace(hour=0),
                window_end=NOW,
                monitored_child_role="child",
                messages=_prefix(case, checkpoint.after_message_id),
            )
            run = await analyzer.analyze_with_raw(conversation)
            assessment = run.assessment
            print(
                f"{case.case_id} @ {checkpoint.after_message_id}: "
                f"{assessment.parent_should_know} / {assessment.child_role.value} / "
                f"evidence={list(assessment.evidence_message_ids)} / "
                f"summary={assessment.summary} / reason={assessment.reason}"
            )
            prefix_ids = {message.id for message in conversation.messages}
            if assessment.parent_should_know not in checkpoint.expected_parent_should_know:
                failures.append(
                    f"{case.case_id}@{checkpoint.after_message_id}: "
                    f"got {assessment.parent_should_know}, expected "
                    f"{list(checkpoint.expected_parent_should_know)}"
                )
            if assessment.child_role not in checkpoint.expected_child_roles:
                failures.append(
                    f"{case.case_id}@{checkpoint.after_message_id}: "
                    f"unexpected child role {assessment.child_role.value}"
                )
            if not set(assessment.evidence_message_ids) <= prefix_ids:
                failures.append(
                    f"{case.case_id}@{checkpoint.after_message_id}: evidence cites a message outside the prefix"
                )
            if assessment.parent_should_know is True and not assessment.evidence_message_ids:
                failures.append(
                    f"{case.case_id}@{checkpoint.after_message_id}: true result has no evidence"
                )
    if failures:
        pytest.fail("\n".join(failures))
