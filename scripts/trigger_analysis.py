#!/usr/bin/env python
"""Manually trigger analysis for a specific chat or all due chats.

Usage:
    # Analyze a specific chat (bypasses quiet period):
    .venv/bin/python scripts/trigger_analysis.py --chat 972546610653@c.us

    # Analyze a specific chat for a specific user:
    .venv/bin/python scripts/trigger_analysis.py --chat 972546610653@c.us --user 82d5a7ea-92b4-4b83-8d49-a65e92080829

    # Analyze all due chats (same as worker run_once):
    .venv/bin/python scripts/trigger_analysis.py --due

    # Analyze all due chats for a specific user:
    .venv/bin/python scripts/trigger_analysis.py --due --user 82d5a7ea-92b4-4b83-8d49-a65e92080829

    # Force-analyze a chat even if not due (resets next_analysis_at to now):
    .venv/bin/python scripts/trigger_analysis.py --chat 972546610653@c.us --force

    # Dry run (show what would be analyzed, don't call LLM):
    .venv/bin/python scripts/trigger_analysis.py --due --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_logger = logging.getLogger("trigger_analysis")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Manually trigger chat analysis")
    parser.add_argument("--chat", help="Chat ID to analyze (e.g. 972546610653@c.us)")
    parser.add_argument("--user", help="User ID (required if multiple users have the same chat)")
    parser.add_argument("--due", action="store_true", help="Analyze all due chats")
    parser.add_argument("--force", action="store_true", help="Force analyze even if not due (resets next_analysis_at)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be analyzed without calling LLM")
    parser.add_argument("--no-judge", action="store_true", help="Skip the LLM judge")
    parser.add_argument("--limit", type=int, default=20, help="Max chats to process (default 20)")
    args = parser.parse_args()

    if not args.chat and not args.due:
        parser.error("specify --chat or --due")

    from sqlalchemy import select, update

    from echo_v2.persistence.compose import build_postgres_repos
    from echo_v2.persistence.orm import ChatRow
    from echo_v2.persistence.settings import load_db_settings
    from echo_v2.services.analysis_judge import AnalysisJudge
    from echo_v2.services.chat_analysis_worker import (
        ChatAnalysisProcessor,
        ChatAnalysisWorker,
    )
    from echo_v2.services.transcription_factory import build_transcriber
    from echo_v2.services.waiting_for_me_analyzer import LLMWaitingForMeAnalyzer

    settings = load_db_settings()
    repos = build_postgres_repos(settings)

    # Build the same components as main.py
    from openai import AsyncOpenAI

    raw_client = AsyncOpenAI()
    openai_client = raw_client

    # Wrap with LangSmith if tracing is enabled
    tracing_enabled = os.environ.get("LANGSMITH_TRACING", "false").lower() in ("1", "true", "yes")
    if tracing_enabled:
        from langsmith.wrappers import wrap_openai
        openai_client = wrap_openai(raw_client)

    analyzer = LLMWaitingForMeAnalyzer(
        client=openai_client,
        model=os.environ.get("LLM_MODEL_NAME", "gpt-4.1"),
    )
    transcriber = build_transcriber()
    processor = ChatAnalysisProcessor(
        message_repo=repos.messages,
        analyzer=analyzer,
        context_messages=5,
        max_no_outbound=20,
        transcriber=transcriber,
    )
    judge = None if args.no_judge else AnalysisJudge(
        client=openai_client,
        model=os.environ.get("JUDGE_MODEL_NAME", "gpt-5.4"),
    )
    worker = ChatAnalysisWorker(
        chat_state_repo=repos.chat_state,
        processor=processor,
        commit_repo=repos.analysis_commit,
        judge=judge,
    )

    # Find chats to analyze
    async with repos.session_factory() as session:
        if args.chat:
            # Find the specific chat
            query = select(ChatRow).where(ChatRow.chat_id == args.chat)
            if args.user:
                query = query.where(ChatRow.user_id == args.user)
            result = await session.execute(query)
            chats = result.scalars().all()

            if not chats:
                print(f"No chat found with id={args.chat}" + (f" user={args.user}" if args.user else ""))
                print("Available chats:")
                all_chats = await session.execute(select(ChatRow).limit(20))
                for c in all_chats.scalars().all():
                    print(f"  user={c.user_id} chat={c.chat_id} version={c.activity_version}")
                return

            if args.force:
                # Reset next_analysis_at to now so it's immediately due
                for c in chats:
                    await session.execute(
                        update(ChatRow)
                        .where(ChatRow.user_id == c.user_id, ChatRow.chat_id == c.chat_id)
                        .values(next_analysis_at=datetime.now(timezone.utc))
                    )
                await session.commit()
                print(f"Force-reset next_analysis_at for {len(chats)} chat(s)")

            # Check if due
            now = datetime.now(timezone.utc)
            for c in chats:
                due = c.next_analysis_at is not None and c.next_analysis_at <= now
                pending = c.activity_version > c.last_processed_version
                status = "PENDING" if pending else "already processed (skip)"
                print(f"Chat {c.chat_id} user={c.user_id}: version={c.activity_version} processed={c.last_processed_version} next={c.next_analysis_at} due={due} {status}")

            # Filter to only chats with new unprocessed versions
            chats = [c for c in chats if c.activity_version > c.last_processed_version]
            if not chats:
                print("No chats with unprocessed versions. Use --force with a new message to trigger re-analysis.")
                return

        elif args.due:
            # Use the worker's list_due
            now = datetime.now(timezone.utc)
            due_chats = await repos.chat_state.list_due(now, limit=args.limit)
            chats = due_chats
            print(f"Found {len(chats)} due chats")
            for c in chats:
                print(f"  user={c.user_id} chat={c.chat_id} version={c.activity_version} processed={c.last_processed_version}")

    if args.dry_run:
        print("\nDry run — not calling LLM.")
        return

    if not chats:
        print("No chats to analyze.")
        return

    print(f"\nAnalyzing {len(chats)} chat(s)...")
    for chat in chats:
        if hasattr(chat, 'user_id'):
            # It's a ChatRow ORM object — convert to ChatState
            from echo_v2.domain.chat import ChatState, MessageDirection
            chat_state = ChatState(
                user_id=chat.user_id,
                chat_id=chat.chat_id,
                activity_version=chat.activity_version,
                last_message_at=chat.last_message_at,
                last_direction=MessageDirection(chat.last_direction),
                next_analysis_at=chat.next_analysis_at,
                last_processed_version=chat.last_processed_version,
            )
        else:
            chat_state = chat  # Already a ChatState from list_due

        print(f"\n--- Processing {chat_state.chat_id} (user={chat_state.user_id}, version={chat_state.activity_version}) ---")
        try:
            result = await worker._process_chat(chat_state)
            print(f"  Result: {result}")
        except Exception as e:
            print(f"  ERROR: {e}")
            _logger.exception("analysis failed")

    # Close clients
    await raw_client.close()
    if transcriber is not None:
        try:
            await transcriber.close()
        except Exception:
            _logger.debug("transcriber close failed", exc_info=True)

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
