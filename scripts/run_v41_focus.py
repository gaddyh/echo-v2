"""Focused runner for the 8 v4.1 target cases. Judges on exact 4-way labels."""

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

from echo_v2.domain.waiting_for_me import WaitingForMeDecision
from echo_v2.services.chat_analysis_worker import ConversationInput
from echo_v2.services.waiting_for_me_analyzer import LLMWaitingForMeAnalyzer
from tests.evaluation.waiting_for_me_cases import SANITY_CASES
from tests.evaluation.waiting_for_me_soc_cases import SOC_ALL_CASES
from tests.evaluation.waiting_for_me_soc2508_cases import SOC2508_ALL_CASES

_SHORT = {
    WaitingForMeDecision.WAITING_FOR_ME: "WFM",
    WaitingForMeDecision.NOT_WAITING_FOR_ME: "NWM",
    WaitingForMeDecision.UNCERTAIN: "UNC",
}

# v4 actual → v4.1 target (from the user's spec)
_V4_ACTUAL = {
    "socf-57": "UNC",
    "socf-83": "WFM",
    "socf-85": "NWM",
    "socf-87": "UNC",
    "socf-65": "WFM",
    "socr-14": "NWM",
    "socr-15": "UNC",
    "socr-16": "NWM",
}

FOCUS_IDS = set(_V4_ACTUAL)


def _build_conversation(case):
    base = datetime(2026, 9, 5, 10, 0, 0, tzinfo=timezone.utc)
    return ConversationInput(
        user_id="eval-user",
        chat_id="eval-chat@c.us",
        target_version=1,
        messages=[
            (direction, text, base + timedelta(minutes=i))
            for i, (direction, text) in enumerate(case.messages)
        ],
    )


async def main():
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    model = os.environ.get("LLM_MODEL_NAME", "gpt-4.1")
    version = os.environ.get("WFM_PROMPT_VERSION", "v4.1")
    analyzer = LLMWaitingForMeAnalyzer(
        client=client, model=model, prompt_version=version
    )
    print(f"model={model} prompt={version}\n")

    all_cases = {c.id: c for c in SANITY_CASES + SOC_ALL_CASES + SOC2508_ALL_CASES}
    cases = [all_cases[cid] for cid in sorted(FOCUS_IDS)]

    correct = 0
    for case in cases:
        conv = _build_conversation(case)
        t0 = time.perf_counter()
        try:
            result, raw = await analyzer.analyze_with_raw(conv)
            latency = (time.perf_counter() - t0) * 1000
            actual = result.decision
            expected = case.expected
            ok = actual == expected
            if ok:
                correct += 1
            v4_was = _V4_ACTUAL[case.id]
            fixed = "FIXED" if ok and v4_was != _SHORT[expected] else ("OK" if ok else "STILL FAIL")
            print(f"{'='*72}")
            print(f"{'PASS' if ok else 'FAIL'}  {case.id}  {fixed}")
            print(f"  v4 was: {v4_was}  →  v4.1 expected: {_SHORT[expected]}  →  v4.1 actual: {_SHORT.get(actual, '?')}  ({latency:.0f}ms)")
            print(f"  desc: {case.description}")
            print(f"  reason: {result.reason}")
            print(f"  next_owner: {getattr(result, 'next_owner', None)}  confidence: {result.confidence}")
            print(f"  messages:")
            for d, t in case.messages:
                print(f"    [{d}] {t[:100]}")
            print()
        except Exception as exc:
            latency = (time.perf_counter() - t0) * 1000
            print(f"ERROR  {case.id}  ({latency:.0f}ms): {exc}\n")

    print(f"{'='*72}")
    print(f"RESULT: {correct}/{len(cases)} exact 4-way match")


if __name__ == "__main__":
    asyncio.run(main())
