"""API-backed evaluation for the Exposure Guard product-value Gold set."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from echo_v2.domain.guard_simple import SimpleConversation
from echo_v2.services.guard_exposure_analyzer import ExposureAnalyzer
from echo_v2.services.guard_simple_analyzer import SimpleSafetyAnalyzer
from tests.evaluation.guard.guard_exposure_cases import (
    CASES,
    ExposureGoldCase,
    validate_cases,
)

pytestmark = [
    pytest.mark.eval_guard,
    pytest.mark.skipif(
        not os.environ.get("OPENAI_API_KEY"),
        reason="OPENAI_API_KEY not set — eval requires real API access",
    ),
]

NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)


def _model(env_var: str) -> str:
    return os.environ.get(
        env_var,
        os.environ.get(
            "GUARD_SIMPLE_LLM_MODEL",
            os.environ.get("LLM_MODEL_NAME", "gpt-4.1"),
        ),
    )


def _conversation(case: ExposureGoldCase) -> SimpleConversation:
    return SimpleConversation(
        child_user_id="gold-child",
        child_name="Shay",
        chat_id=case.chat_id or case.case_id,
        chat_type="group",
        connection_id="gold-connection",
        timezone="Asia/Jerusalem",
        window_start=NOW.replace(hour=0),
        window_end=NOW,
        monitored_child_role="child",
        messages=case.messages,
    )


@pytest.fixture
def analyzers() -> tuple[SimpleSafetyAnalyzer, ExposureAnalyzer]:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
    return (
        SimpleSafetyAnalyzer(client=client, model=_model("GUARD_SIMPLE_LLM_MODEL")),
        ExposureAnalyzer(client=client, model=_model("GUARD_EXPOSURE_LLM_MODEL")),
    )


async def test_exposure_gold_cases(
    analyzers: tuple[SimpleSafetyAnalyzer, ExposureAnalyzer],
) -> None:
    validate_cases()
    child_analyzer, exposure_analyzer = analyzers
    failures: list[str] = []
    for case in CASES:
        conversation = _conversation(case)
        child_assessment = None
        if case.child_expected is not None:
            child_assessment = await child_analyzer.analyze(conversation)
        exposure_run = await exposure_analyzer.analyze_with_raw(conversation)
        exposure = exposure_run.assessment
        print(
            f"{case.case_id}: child_expected={case.child_expected} "
            f"child_actual={child_assessment.parent_should_know if child_assessment else 'skipped'} / "
            f"exposure_expected={case.exposure_expected} "
            f"exposure_actual={exposure.significant_exposure} "
            f"category={exposure.category.value if exposure.category else None} "
            f"evidence={list(exposure.evidence_message_ids)} / "
            f"summary={exposure.summary} / reason={exposure.reason}"
        )
        if (
            child_assessment is not None
            and child_assessment.parent_should_know is not case.child_expected
        ):
            failures.append(
                f"{case.case_id}: child guard got "
                f"{child_assessment.parent_should_know}, expected {case.child_expected}"
            )
        if (
            case.exposure_expected is not None
            and exposure.significant_exposure is not case.exposure_expected
        ):
            failures.append(
                f"{case.case_id}: exposure got "
                f"{exposure.significant_exposure}, expected {case.exposure_expected}"
            )
        if not set(case.expected_evidence_message_ids) <= set(exposure.evidence_message_ids):
            failures.append(
                f"{case.case_id}: exposure evidence "
                f"{list(exposure.evidence_message_ids)} missing required "
                f"{list(case.expected_evidence_message_ids)}"
            )
    if failures:
        pytest.fail("\n".join(failures))
