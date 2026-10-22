#!/usr/bin/env python
"""Validate the Incident Tracker against real production assessment history.

Replays historical YES assessments chronologically through the IncidentTracker
using the real LLM IncidentMatcher, against an isolated in-memory incident
repository. No notifications are scheduled, no writes to production tables.

Usage:
    # Validate a specific child's security assessments
    .venv/bin/python scripts/validate_incident_tracker.py --child <child_user_id>

    # Validate a specific chat
    .venv/bin/python scripts/validate_incident_tracker.py --chat <chat_id>

    # Validate both security and exposure assessments
    .venv/bin/python scripts/validate_incident_tracker.py --child <child_user_id> --include-exposure

    # Dry run (default) — no writes, no notifications
    .venv/bin/python scripts/validate_incident_tracker.py --child <child_user_id> --dry-run

Expected output:
    Assessment 1  → NEW         incident-abc  "Bullying at school"
    Assessment 2  → SAME        incident-abc  "Still being bullied"
    Assessment 3  → SAME        incident-abc  "Same bullying continues"
    ...
    Distinct incidents: 1
    Would schedule:  1 alert
    Would suppress:  7 alerts
    Actual sends:    0
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_logger = logging.getLogger("validate_incident_tracker")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate Incident Tracker against real assessment history (dry run)"
    )
    parser.add_argument("--child", help="Child user ID to validate")
    parser.add_argument("--chat", help="Chat ID to validate (alternative to --child)")
    parser.add_argument(
        "--include-exposure",
        action="store_true",
        help="Also replay exposure assessments (default: security only)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Dry run — no writes, no notifications (default: True)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Max assessments to replay (default 100)",
    )
    args = parser.parse_args()

    if not args.child and not args.chat:
        parser.error("specify --child or --chat")

    from openai import AsyncOpenAI

    from echo_v2.persistence.alerted_incident_repository import (
        InMemoryAlertedIncidentRepository,
    )
    from echo_v2.persistence.compose import build_postgres_repos
    from echo_v2.persistence.guard_repositories import (
        InMemoryGuardianChildLinkRepository,
    )
    from echo_v2.persistence.scheduled_actions import InMemoryScheduledActionRepository
    from echo_v2.persistence.settings import load_db_settings
    from echo_v2.services.incident_matcher import IncidentMatcher
    from echo_v2.services.incident_tracker import IncidentTracker

    settings = load_db_settings()
    repos = build_postgres_repos(settings)

    # Build the real LLM matcher (same model as production)
    openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
    matcher = IncidentMatcher(
        client=openai_client,
        model=os.environ.get(
            "INCIDENT_MATCHER_LLM_MODEL",
            os.environ.get("GUARD_LLM_MODEL_NAME", "gpt-4.1"),
        ),
    )

    # Isolated in-memory repos — no production writes
    incidents_repo = InMemoryAlertedIncidentRepository()
    links_repo = InMemoryGuardianChildLinkRepository()
    scheduling_repo = InMemoryScheduledActionRepository()

    # Fake user resolver — returns a phone for any guardian (so the tracker
    # actually creates incidents; we're testing dedup, not eligibility)
    class FakeUsers:
        async def get_phone_by_id(self, user_id: str) -> str | None:
            return f"+9725000{user_id[-6:]}"

    # Set up a fake guardian link so the tracker creates incidents
    child_id = args.child or "child-unknown"
    await links_repo.upsert_active(
        guardian_user_id="guardian-test",
        child_user_id=child_id,
        now=datetime.now(timezone.utc),
    )

    tracker = IncidentTracker(
        matcher=matcher,
        incidents=incidents_repo,
        links=links_repo,
        users=FakeUsers(),
        scheduling=scheduling_repo,
        enabled=True,
    )

    # Load historical YES assessments from production
    print(f"\n{'='*80}")
    print("Incident Tracker Validation (dry run)")
    print(f"{'='*80}")

    # Determine child_user_ids filter
    child_ids = [args.child] if args.child else []
    if args.chat and not args.child:
        # Find the child user ID for this chat
        async with repos.session_factory() as session:
            from sqlalchemy import select

            from echo_v2.persistence.orm import GuardSimpleAssessmentRow

            db_result = await session.execute(
                select(GuardSimpleAssessmentRow.child_user_id)
                .where(GuardSimpleAssessmentRow.chat_id == args.chat)
                .where(GuardSimpleAssessmentRow.parent_should_know.is_(True))
                .distinct()
            )
            child_ids = [str(row[0]) for row in db_result.all()]
            if not child_ids:
                print(f"No YES security assessments found for chat {args.chat}")
                await openai_client.close()
                return

    print(f"Child user IDs: {child_ids}")
    print("Alert type: security" + (" + exposure" if args.include_exposure else ""))
    print(f"Matcher model: {matcher._model}")
    print()

    # Load security assessments
    security_assessments = await repos.guard_simple_assessments.list_for_children(
        child_user_ids=child_ids,
        decision="yes",
        limit=args.limit,
    )
    security_assessments.sort(key=lambda a: a.created_at or datetime.min.replace(tzinfo=timezone.utc))

    print(f"Found {len(security_assessments)} YES security assessments")
    for i, a in enumerate(security_assessments):
        print(f"  [{i+1}] id={a.id} created={a.created_at} summary={a.summary[:80] if a.summary else 'N/A'}")
    print()

    # Replay security assessments
    print(f"{'='*80}")
    print("Replaying security assessments through IncidentTracker")
    print(f"{'='*80}\n")

    results: list[tuple[str, str, str]] = []  # (assessment_id, tracker_result, incident_id)
    scheduled_count = 0
    suppressed_count = 0

    for i, assessment in enumerate(security_assessments):
        assessment_id = assessment.id or f"replay-{i}"
        result: str
        try:
            result = await tracker.evaluate_security(
                assessment=assessment,
                assessment_id=assessment_id,
            )
        except Exception as exc:
            result = f"ERROR: {exc}"
            _logger.exception("evaluation failed for assessment %s", assessment_id)

        # Find the incident ID (if any)
        active = await incidents_repo.list_active(
            assessment.child_user_id, assessment.chat_id, "security"
        )
        incident_id = active[-1].id if active else "N/A"

        summary_preview = (assessment.summary or "N/A")[:60]
        print(f"  Assessment {i+1:2d}  → {result:25s}  incident={incident_id}  \"{summary_preview}\"")

        results.append((assessment_id, result, incident_id))
        if result in ("scheduled", "recurrence_scheduled"):
            scheduled_count += 1
        elif result == "suppressed":
            suppressed_count += 1

    # Replay exposure assessments if requested
    if args.include_exposure:
        from echo_v2.domain.guard_exposure import ExposureAssessment

        exposure_assessments: list[ExposureAssessment] = []
        try:
            exposure_assessments = await repos.guard_exposure_assessments.list_for_children(
                child_user_ids=child_ids,
                decision="yes",
                limit=args.limit,
            )
            exposure_assessments.sort(
                key=lambda a: a.created_at or datetime.min.replace(tzinfo=timezone.utc)
            )
        except Exception:
            _logger.debug("no exposure assessments found", exc_info=True)

        if exposure_assessments:
            print(f"\n{'='*80}")
            print(f"Replaying {len(exposure_assessments)} exposure assessments")
            print(f"{'='*80}\n")

            for i, exp_assessment in enumerate(exposure_assessments):
                assessment_id = exp_assessment.id or f"exp-replay-{i}"
                exp_result: str
                try:
                    exp_result = await tracker.evaluate_exposure(
                        assessment=exp_assessment,
                        assessment_id=assessment_id,
                    )
                except Exception as exc:
                    exp_result = f"ERROR: {exc}"
                    _logger.exception("exposure evaluation failed for %s", assessment_id)

                active = await incidents_repo.list_active(
                    exp_assessment.child_user_id, exp_assessment.chat_id, "exposure"
                )
                incident_id = active[-1].id if active else "N/A"

                summary_preview = (exp_assessment.summary or "N/A")[:60]
                print(f"  Exposure {i+1:2d}  → {exp_result:25s}  incident={incident_id}  \"{summary_preview}\"")

                results.append((assessment_id, exp_result, incident_id))
                if exp_result in ("scheduled", "recurrence_scheduled"):
                    scheduled_count += 1
                elif exp_result == "suppressed":
                    suppressed_count += 1

    # Summary
    distinct_incidents = len({r[2] for r in results if r[2] != "N/A"})
    print(f"\n{'='*80}")
    print("Summary")
    print(f"{'='*80}")
    print(f"  Total assessments replayed:  {len(results)}")
    print(f"  Distinct incidents:          {distinct_incidents}")
    print(f"  Would schedule (NEW/ESCAL):  {scheduled_count}")
    print(f"  Would suppress (SAME):       {suppressed_count}")
    print(f"  Already processed:           {sum(1 for r in results if r[1] == 'already_processed')}")
    print(f"  No guardian/phone:          {sum(1 for r in results if 'no_' in r[1])}")
    print(f"  Errors:                      {sum(1 for r in results if 'ERROR' in r[1])}")
    print("  Actual sends:                0 (dry run)")
    print()

    # Show all incidents
    all_active = []
    for cid in child_ids:
        all_active.extend(
            await incidents_repo.list_active(cid, args.chat or "", "security")
        )
    if all_active:
        print("Incidents created:")
        for inc in all_active:
            print(
                f"  {inc.id}  v{inc.notification_version}  "
                f"related={inc.related_incident_id or 'None'}  "
                f"summary={inc.summary[:60] if inc.summary else 'N/A'}"
            )

    await openai_client.close()
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
