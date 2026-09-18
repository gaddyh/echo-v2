"""Quick standalone runner for the 6 updated sanity cases on v4."""

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

from echo_v2.domain.waiting_for_me import WaitingForMeDecision
from echo_v2.services.chat_analysis_worker import ConversationInput
from echo_v2.services.waiting_for_me_analyzer import LLMWaitingForMeAnalyzer
from echo_v2.services.waiting_for_me_prompts import get_prompt
from tests.evaluation.waiting_for_me_cases import SANITY_CASES

_SHORT = {
    WaitingForMeDecision.WAITING_FOR_ME: "WFM",
    WaitingForMeDecision.NOT_WAITING_FOR_ME: "NWM",
    WaitingForMeDecision.UNCERTAIN: "UNC",
}

TARGET_IDS = {"wfm-02", "wfm-05", "unc-01", "unc-02", "unc-03", "unc-04"}


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
    version = os.environ.get("WFM_PROMPT_VERSION", "v4")
    analyzer = LLMWaitingForMeAnalyzer(
        client=client, model=model, prompt_version=version
    )
    print(f"model={model} prompt={version}\n")

    cases = [c for c in SANITY_CASES if c.id in TARGET_IDS]
    for case in cases:
        conv = _build_conversation(case)
        t0 = time.perf_counter()
        try:
            result, raw = await analyzer.analyze_with_raw(conv)
            latency = (time.perf_counter() - t0) * 1000
            ok = "PASS" if result.decision == case.expected else "FAIL"
            print(f"{'='*72}")
            print(f"{ok}  {case.id}  expected={_SHORT[case.expected]}  actual={_SHORT.get(result.decision, '?')}  ({latency:.0f}ms)")
            print(f"desc: {case.description}")
            print(f"reason: {result.reason}")
            print(f"summary: {result.summary}")
            print(f"next_owner: {getattr(result, 'next_owner', None)}  confidence: {result.confidence}")
            print("messages:")
            for d, t in case.messages:
                print(f"  [{d}] {t}")
            print()
        except Exception as exc:
            latency = (time.perf_counter() - t0) * 1000
            print(f"ERROR  {case.id}  ({latency:.0f}ms): {exc}\n")


if __name__ == "__main__":
    asyncio.run(main())
