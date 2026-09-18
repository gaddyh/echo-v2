"""Create the LangSmith annotation queue for user false positives.

Creates an annotation queue with a rubric for human review of cases where
the user reported the analyzer was wrong — i.e. tapped "לא מחכים לי"
(WhatsApp) or marked a card as "detected_incorrectly" (web mini-app).

The queue rubric asks the annotator:
1. correct_decision — What should the analyzer have decided? (WFM / NWM / UNC)
2. user_feedback_valid — Was the user's "not waiting for me" feedback valid? (yes / no)
3. annotation_notes — Free-form observations

Usage:
    .venv/bin/python scripts/setup_user_annotation_queue.py

Outputs the queue ID to add to .env as USER_ANNOTATION_QUEUE_ID.

Idempotent: if a queue with the same name exists, reuses it.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
from langsmith import Client

QUEUE_NAME = "echo-v2 user false positives"
QUEUE_DESCRIPTION = (
    "Cases where the user reported the analyzer was wrong — tapped "
    "'לא מחכים לי' (WhatsApp) or marked a card as 'detected_incorrectly' "
    "(web mini-app). Review the conversation and label the correct decision."
)
RUBRIC_INSTRUCTIONS = (
    "Review the conversation and the analyzer's decision.\n"
    "The user reported this as a false positive (not actually waiting).\n\n"
    "1. Label what the correct decision should have been (correct_decision).\n"
    "2. Indicate whether the user's 'not waiting for me' feedback was valid "
    "(user_feedback_valid).\n"
    "3. Add notes if the case is interesting or edge-casey.\n\n"
    "Definitions:\n"
    "- waiting_for_me: The next step is expected from the user (ball in user's court).\n"
    "- not_waiting_for_me: No open expectation; conversation closed or other person acts next.\n"
    "- uncertain: Not enough information to decide confidently.\n\n"
    "user_feedback_valid:\n"
    "- yes: Analyzer produced a false positive; user feedback is valid.\n"
    "- no: Analyzer was actually correct; user dismissed it for another reason."
)


def main() -> None:
    load_dotenv()
    api_key = os.environ.get("LANGSMITH_API_KEY", "")
    if not api_key:
        print("ERROR: LANGSMITH_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    client = Client(api_key=api_key)

    # Create feedback configs (idempotent — returns existing if same config).
    print("Creating feedback configs...")
    client.create_feedback_config(
        "correct_decision",
        feedback_config={
            "type": "categorical",
            "categories": [
                {"value": 0, "label": "waiting_for_me"},
                {"value": 1, "label": "not_waiting_for_me"},
                {"value": 2, "label": "uncertain"},
            ],
        },
    )
    print("  correct_decision: categorical (WFM / NWM / UNC)")

    client.create_feedback_config(
        "user_feedback_valid",
        feedback_config={
            "type": "categorical",
            "categories": [
                {"value": 1, "label": "yes"},
                {"value": 0, "label": "no"},
            ],
        },
    )
    print("  user_feedback_valid: categorical (yes / no)")

    client.create_feedback_config(
        "annotation_notes",
        feedback_config={"type": "freeform"},
    )
    print("  annotation_notes: freeform")

    # Check if queue already exists.
    existing = None
    try:
        for q in client.list_annotation_queues():
            if q.name == QUEUE_NAME:
                existing = q
                break
    except Exception as exc:  # noqa: BLE001
        print(f"  (list_annotation_queues failed: {exc})")

    if existing is not None:
        print(f"\nQueue already exists: {existing.id} ({existing.name})")
        print("\nAdd to .env:")
        print(f"  USER_ANNOTATION_QUEUE_ID={existing.id}")
        return

    # Create the annotation queue with rubric items.
    print(f"\nCreating annotation queue: {QUEUE_NAME!r}...")
    queue = client.create_annotation_queue(
        name=QUEUE_NAME,
        description=QUEUE_DESCRIPTION,
        rubric_instructions=RUBRIC_INSTRUCTIONS,
        rubric_items=[
            {
                "feedback_key": "correct_decision",
                "description": "What should the analyzer have decided?",
                "value_descriptions": {
                    "waiting_for_me": "The next step is expected from the user",
                    "not_waiting_for_me": "No open expectation",
                    "uncertain": "Not enough information",
                },
                "is_required": True,
            },
            {
                "feedback_key": "user_feedback_valid",
                "description": (
                    "Was the user's 'not waiting for me' feedback valid?"
                ),
                "value_descriptions": {
                    "yes": "Analyzer produced a false positive; user feedback is valid",
                    "no": "Analyzer was actually correct; user dismissed it for another reason",
                },
                "is_required": True,
            },
            {
                "feedback_key": "annotation_notes",
                "description": "Any additional observations (edge cases, patterns, etc.)",
                "is_required": False,
            },
        ],
    )

    print(f"\nQueue created: {queue.id}")
    print(f"  Name: {queue.name}")
    print("\nAdd to .env:")
    print(f"  USER_ANNOTATION_QUEUE_ID={queue.id}")
    print("\nReview URL:")
    org_id = os.environ.get("LANGSMITH_WORKSPACE_ID", "default")
    print(f"  https://smith.langchain.com/o/{org_id}/annotation-queues/{queue.id}")


if __name__ == "__main__":
    main()
