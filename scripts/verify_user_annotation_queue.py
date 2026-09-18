"""End-to-end verification: enqueue a user false-positive to the annotation queue.

Loads a real WaitingForMeResult from the DB, creates a wfm.user_false_positive
trace, and enqueues it to USER_ANNOTATION_QUEUE_ID. Then lists the queue to
confirm the item landed.

Requires LANGSMITH_TRACING=true (set below automatically) and
USER_ANNOTATION_QUEUE_ID in .env.

Usage:
    .venv/bin/python scripts/verify_user_annotation_queue.py [result_id]

If result_id is omitted, picks the most recent result with a conversation_snapshot
that has at least 3 messages with non-empty text.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Ensure src/ is on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Load .env BEFORE setting env vars and before any echo_v2 imports,
# so tracing_client (created at import time) picks up LANGSMITH_API_KEY.
from dotenv import load_dotenv

load_dotenv()

# Enable tracing and force inputs/outputs visible (override .env's
# LANGSMITH_HIDE_INPUTS=true which would strip the conversation text).
os.environ["LANGSMITH_TRACING"] = "true"
os.environ["LANGSMITH_HIDE_INPUTS"] = "false"
os.environ["LANGSMITH_HIDE_OUTPUTS"] = "false"

from langsmith import Client
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from echo_v2.observability.tracing import tracing_client
from echo_v2.services.feedback_service import WaitingForMeActionService


async def _pick_result_id(db_url: str) -> tuple[str, str, str]:
    """Find the most recent result with >=3 messages that have non-empty text.

    Returns (result_id, user_id, chat_id).
    """
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        # Fetch recent results and filter in Python for message text quality.
        result = await conn.execute(
            text(
                "SELECT id, user_id, chat_id, conversation_snapshot "
                "FROM waiting_for_me_results "
                "WHERE conversation_snapshot IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 50"
            )
        )
        rows = result.fetchall()
    await engine.dispose()

    for row in rows:
        rid, uid, cid, snapshot = row
        if snapshot is None:
            continue
        if isinstance(snapshot, str):
            try:
                snapshot = json.loads(snapshot)
            except (json.JSONDecodeError, TypeError):
                continue
        messages = snapshot.get("messages", []) if isinstance(snapshot, dict) else []
        with_text = [m for m in messages if m.get("text")]
        if len(with_text) >= 3:
            return str(rid), str(uid), str(cid)

    print("ERROR: no results with >=3 text messages found in DB")
    sys.exit(1)


async def main() -> None:
    queue_id = os.environ.get("USER_ANNOTATION_QUEUE_ID", "")
    if not queue_id:
        print("ERROR: USER_ANNOTATION_QUEUE_ID not set in .env")
        sys.exit(1)

    db_url = os.environ["DATABASE_URL"]

    # Pick a result_id to enqueue.
    if len(sys.argv) > 1:
        result_id_arg = sys.argv[1]
        engine = create_async_engine(db_url)
        async with engine.begin() as conn:
            r = await conn.execute(
                text("SELECT user_id FROM waiting_for_me_results WHERE id = :rid"),
                {"rid": result_id_arg},
            )
            row = r.fetchone()
        await engine.dispose()
        if row is None:
            print(f"ERROR: result_id {result_id_arg} not found in DB")
            sys.exit(1)
        result_id = result_id_arg
        user_id = str(row[0])
    else:
        result_id, user_id, chat_id = await _pick_result_id(db_url)
        print(f"Using result: {result_id} (chat={chat_id})")

    print(f"User: {user_id}")
    print(f"Result: {result_id}")
    print(f"Queue: {queue_id}")
    print()

    # Build a minimal action service with just the result repo.
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from echo_v2.persistence.chat_repositories import (
        InMemoryWaitingForMeActiveRepository,
    )
    from echo_v2.persistence.feedback_repositories import (
        InMemoryChatMuteRepository,
        InMemoryWaitingForMeActionRepository,
    )
    from echo_v2.persistence.postgres_chat import (
        PostgresWaitingForMeResultRepository,
    )

    engine = create_async_engine(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    result_repo = PostgresWaitingForMeResultRepository(factory)

    action_service = WaitingForMeActionService(
        active_repo=InMemoryWaitingForMeActiveRepository(),
        action_repo=InMemoryWaitingForMeActionRepository(
            active_repo=InMemoryWaitingForMeActiveRepository(),
            mute_repo=InMemoryChatMuteRepository(),
        ),
        mute_repo=InMemoryChatMuteRepository(),
        result_repo=result_repo,
    )

    print("Step 1: Load result from DB...")
    result = await result_repo.get_by_id(result_id)
    if result is None or not result.conversation_snapshot:
        print(f"ERROR: result {result_id} not found or has no conversation_snapshot")
        await engine.dispose()
        sys.exit(1)
    messages = result.conversation_snapshot.get("messages")
    if not messages:
        print("ERROR: conversation_snapshot has no messages")
        await engine.dispose()
        sys.exit(1)

    # Show the messages so we can verify they have text.
    with_text = [m for m in messages if m.get("text")]
    print(f"  {len(messages)} messages total, {len(with_text)} with text")
    print(f"  decision={result.decision.value}, model={result.model}, "
          f"prompt_version={result.prompt_version}")
    for i, m in enumerate(messages):
        direction = m.get("direction", "?")
        msg_text = m.get("text") or "(empty)"
        print(f"    [{i}] {direction}: {msg_text[:80]}")
    print()

    if len(with_text) < 3:
        print("WARNING: fewer than 3 messages with text. Proceeding anyway.")

    from echo_v2.observability.privacy import correlation_id

    print("Step 2: Create wfm.user_false_positive trace...")
    run_id, run_start_time = await action_service._trace_user_false_positive(
        conversation=messages,
        analyzer_decision=result.decision.value,
        next_owner=(
            result.next_owner.value if result.next_owner is not None else None
        ),
        open_obligation=result.open_obligation,
        analyzer_reason=result.reason,
        analyzer_confidence=result.confidence,
        model=result.model,
        prompt_version=result.prompt_version,
        analyzer_version=result.analyzer_version,
        target_version=result.target_version,
        user_id_hash=correlation_id(user_id),
    )
    print(f"  run_id={run_id}")
    print(f"  run_start_time={run_start_time}")

    if run_id is None:
        print("ERROR: tracing is off — run_id is None. Set LANGSMITH_TRACING=true.")
        await engine.dispose()
        sys.exit(1)

    print("Step 3: Flush tracing client (persist the run)...")
    tracing_client.flush()
    print("  flushed.")

    print("Step 4: Wait 8s for LangSmith to index the run...")
    import time
    time.sleep(8)  # noqa: ASYNC251

    # Verify the run has inputs by querying it back.
    print(f"Step 4b: Query run {run_id} to verify inputs were persisted...")
    client = Client(api_key=os.environ.get("LANGSMITH_API_KEY", ""))
    try:
        paginator = client.runs.query(
            project_ids=[os.environ.get("LANGSMITH_PROJECT_ID", "")],
            filter=f'eq(run_id, "{run_id}")',
        )
        runs = []
        async for r in paginator:
            runs.append(r)
            if len(runs) >= 1:
                break
        if runs:
            run = runs[0]
            inputs = run.inputs if hasattr(run, "inputs") else run.get("inputs")
            print(f"  run name: {run.name}")
            print(f"  inputs keys: {list(inputs.keys()) if inputs else 'NONE'}")
            if inputs and "conversation" in inputs:
                conv = inputs["conversation"]
                print(f"  conversation: {len(conv)} messages")
                if conv:
                    print(f"    first message: {conv[0]}")
            else:
                print("  WARNING: no conversation in inputs!")
        else:
            print("  run not found yet (may still be indexing)")
    except Exception as exc:  # noqa: BLE001
        print(f"  (query failed: {exc})")
    print()

    print(f"Step 5: Enqueue run {run_id} to queue {queue_id}...")
    session_id = os.environ.get("LANGSMITH_PROJECT_ID", "")
    try:
        await tracing_client.annotation_queues.items.create(
            queue_id=queue_id,
            items=[
                {
                    "item_type": "RUN",
                    "run_id": run_id,
                    "session_id": session_id,
                    "start_time": run_start_time,
                }
            ],
        )
        print("  SUCCESS: run enqueued to annotation queue!")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {exc}")
    print()

    # List recent runs to confirm the trace exists and inputs were persisted.
    print("Recent wfm.user_false_positive runs in project:")
    try:
        # Use the deprecated list_runs API which we know works and returns
        # runs with .name and .inputs attributes.
        runs = list(client.list_runs(
            project_name=os.environ.get("LANGSMITH_PROJECT", ""),
            limit=10,
        ))
        for run in runs:
            if "user_false_positive" not in (run.name or ""):
                continue
            inputs = run.inputs if hasattr(run, "inputs") else {}
            has_conv = bool(inputs and inputs.get("conversation"))
            input_keys = list(inputs.keys()) if inputs else []
            print(f"  - name={run.name} id={run.id}")
            print(f"    input_keys={input_keys}")
            print(f"    has_conversation={has_conv}")
            if has_conv:
                conv = inputs["conversation"]
                print(f"    conversation length: {len(conv)}")
                if conv:
                    print(f"    first message: {conv[0]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  (query failed: {exc})")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
