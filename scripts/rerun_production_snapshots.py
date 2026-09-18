#!/usr/bin/env python
"""Re-analyze all chats that have results, locally, without modifying the DB.

Pulls conversation snapshots from the DB, runs the v3 analyzer locally,
and compares against the stored v1 decisions.

Usage:
    set -a; source .env; set +a
    export LANGSMITH_TRACING=false LLM_MODEL_NAME=gpt-5.6-luna WFM_PROMPT_VERSION=v3
    .venv/bin/python scripts/rerun_production_snapshots.py
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def main() -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"])

    # 1. Capture current latest decisions + conversation snapshots for all chats
    async with engine.connect() as conn:
        rows = await conn.execute(text("""
            WITH latest AS (
                SELECT DISTINCT ON (chat_id)
                    chat_id, user_id, decision, reason, summary, prompt_version,
                    target_version, conversation_snapshot
                FROM waiting_for_me_results
                ORDER BY chat_id, created_at DESC
            )
            SELECT chat_id, user_id, decision, reason, summary,
                   prompt_version, target_version, conversation_snapshot
            FROM latest
            ORDER BY chat_id
        """))
        old_results = []
        for r in rows:
            snap = r.conversation_snapshot
            messages = []
            if snap:
                raw = snap.get("messages", snap) if isinstance(snap, dict) else snap
                if isinstance(raw, list):
                    for m in raw:
                        if isinstance(m, dict):
                            d = m.get("direction", m.get("d", "?"))
                            t = m.get("text", m.get("t", ""))
                            messages.append((d, t))
            old_results.append({
                "chat_id": r.chat_id,
                "user_id": r.user_id,
                "old_decision": r.decision,
                "old_reason": r.reason,
                "old_summary": r.summary,
                "old_prompt_version": r.prompt_version,
                "old_target_version": r.target_version,
                "old_next_owner": None,
                "old_open_obligation": None,
                "messages": messages,
            })

    await engine.dispose()

    print(f"Captured {len(old_results)} chats with existing results")

    # 2. Run v3 analyzer locally on each snapshot
    from echo_v2.services.chat_analysis_worker import ConversationInput
    from echo_v2.services.waiting_for_me_analyzer import LLMWaitingForMeAnalyzer
    from openai import AsyncOpenAI

    raw_client = AsyncOpenAI()
    analyzer = LLMWaitingForMeAnalyzer(
        client=raw_client,
        model=os.environ.get("LLM_MODEL_NAME", "gpt-4.1"),
        prompt_version=os.environ.get("WFM_PROMPT_VERSION", "v3"),
    )

    now = datetime.now(timezone.utc)
    new_results = {}

    for i, old in enumerate(old_results):
        chat_id = old["chat_id"]
        msgs = old["messages"]
        if not msgs:
            print(f"[{i+1}/{len(old_results)}] {chat_id}: no messages, skip")
            continue

        print(f"[{i+1}/{len(old_results)}] {chat_id} ({len(msgs)} msgs)...", end=" ", flush=True)

        # Build ConversationInput from snapshot
        from datetime import timedelta
        conv = ConversationInput(
            user_id=old["user_id"],
            chat_id=chat_id,
            target_version=old["old_target_version"] or 1,
            messages=[(d, t, now + timedelta(minutes=i)) for i, (d, t) in enumerate(msgs)],
        )

        try:
            result, raw = await analyzer.analyze_with_raw(conv)
            new_results[chat_id] = {
                "new_decision": result.decision.value,
                "new_reason": result.reason,
                "new_summary": result.summary,
                "new_next_owner": result.next_owner.value if result.next_owner else None,
                "new_open_obligation": result.open_obligation,
                "new_confidence": result.confidence,
            }
            print(f"{result.decision.value} (owner={result.next_owner.value if result.next_owner else None})")
        except Exception as e:
            print(f"ERROR: {e}")

    await raw_client.close()

    # 3. Compare old vs new
    print("\n" + "=" * 80)
    print(f"COMPARISON: old ({old_results[0]['old_prompt_version'] or 'None'}) vs new ({os.environ.get('WFM_PROMPT_VERSION', 'v3')})")
    print("=" * 80)

    flips = []
    unchanged_same = 0
    no_new = 0

    for old in old_results:
        chat_id = old["chat_id"]
        old_dec = old["old_decision"]
        new = new_results.get(chat_id)
        if not new:
            print(f"  {chat_id}: NO NEW RESULT (was {old_dec})")
            no_new += 1
            continue

        new_dec = new["new_decision"]
        new_owner = new["new_next_owner"]

        if old_dec == new_dec:
            unchanged_same += 1
            print(f"  {chat_id}: {old_dec} → {new_dec} (unchanged, owner={new_owner})")
        else:
            flips.append({
                "chat_id": chat_id,
                "old": old_dec,
                "new": new_dec,
                "new_owner": new_owner,
                "old_reason": old["old_reason"],
                "new_reason": new["new_reason"],
                "old_summary": old["old_summary"],
                "new_summary": new["new_summary"],
            })
            marker = " *** NWM→WFM (false waiting-list!) ***" if old_dec == "not_waiting_for_me" and new_dec == "waiting_for_me" else ""
            if old_dec == "waiting_for_me" and new_dec == "not_waiting_for_me":
                marker = " *** WFM→NWM (fixed inversion) ***"
            print(f"  {chat_id}: {old_dec} → {new_dec} (owner={new_owner}){marker}")

    nwm_to_wfm = sum(1 for f in flips if f["old"] == "not_waiting_for_me" and f["new"] == "waiting_for_me")
    wfm_to_nwm = sum(1 for f in flips if f["old"] == "waiting_for_me" and f["new"] == "not_waiting_for_me")

    print(f"\nSummary:")
    print(f"  Total chats compared: {len(old_results)}")
    print(f"  Unchanged: {unchanged_same}")
    print(f"  No new result: {no_new}")
    print(f"  Flips: {len(flips)}")
    print(f"  NWM→WFM (false waiting-list): {nwm_to_wfm}")
    print(f"  WFM→NWM (fixed inversions): {wfm_to_nwm}")

    if flips:
        print("\nFlip details:")
        for f in flips:
            print(f"  {f['chat_id']}: {f['old']} → {f['new']} (owner={f['new_owner']})")
            print(f"    old reason: {f['old_reason']}")
            print(f"    new reason: {f['new_reason']}")
            print()

    # Save results
    output_path = Path("tests/evaluation/results/production_rerun.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "prompt_version": os.environ.get("WFM_PROMPT_VERSION", "v3"),
            "model": os.environ.get("LLM_MODEL_NAME", "gpt-4.1"),
            "old_results": [{k: v for k, v in r.items() if k != "messages"} for r in old_results],
            "new_results": new_results,
            "flips": flips,
            "summary": {
                "total": len(old_results),
                "unchanged": unchanged_same,
                "no_new": no_new,
                "flips": len(flips),
                "nwm_to_wfm": nwm_to_wfm,
                "wfm_to_nwm": wfm_to_nwm,
            },
        }, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nResults saved to {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
