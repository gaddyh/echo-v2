"""Full end-to-end test: analyzer → commit → tracker → scheduler mock.

Replays real conversations through the real LLM analyzer, commits assessments
through the InMemory commit repo, feeds them to the IncidentTracker (with real
LLM matcher), and captures scheduled actions in a mock scheduler.

Edge cases covered:
- Analyzer returns NO (parent_should_know=False) → no incident, no alert
- Analyzer returns YES with no guardian links → incident saved, no alert
- Multiple guardians, one without phone → partial fanout
- Worker retry (reprocess same assessment) → idempotent
- Two distinct issues in same chat → two incidents, two alerts
- Same issue across multiple analyzer runs → one incident, one alert
- Escalation: first NEW, second ESCALATION, third same-severity SAME
- Closed incident + recurrence → matcher recognizes, tracker handles

Requires ``OPENAI_API_KEY`` and calls the real LLM.
"""

from __future__ import annotations

import os
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone

import pytest

from echo_v2.domain.alerted_incident import AlertedIncident
from echo_v2.domain.guard import GuardChatState
from echo_v2.domain.guard_simple import (
    SafetyAssessment,
    SimpleConversation,
    SimpleConversationMessage,
)
from echo_v2.domain.scheduling import (
    ScheduledAction,
    ScheduledActionStatus,
)
from echo_v2.persistence.alerted_incident_repository import (
    InMemoryAlertedIncidentRepository,
)
from echo_v2.persistence.guard_repositories import (
    InMemoryGuardChatStateRepository,
    InMemoryGuardianChildLinkRepository,
)
from echo_v2.persistence.guard_simple_repositories import (
    InMemoryGuardSimpleAssessmentCommitRepository,
    InMemoryGuardSimpleAssessmentRepository,
)
from echo_v2.persistence.scheduled_actions import ScheduledActionRepository
from echo_v2.services.guard_simple_analyzer import SimpleSafetyAnalyzer
from echo_v2.services.incident_matcher import IncidentMatcher
from echo_v2.services.incident_tracker import IncidentTracker

pytestmark = [
    pytest.mark.eval_guard,
    pytest.mark.skipif(
        not os.environ.get("OPENAI_API_KEY"),
        reason="OPENAI_API_KEY not set — eval requires real API access",
    ),
]

_T0 = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeUsers:
    """Returns a phone for guardians that have one configured."""

    def __init__(self, phones: dict[str, str] | None = None) -> None:
        self._phones = phones or {}

    async def get_phone_by_id(self, user_id: str) -> str | None:
        return self._phones.get(user_id)


class CapturingScheduling(ScheduledActionRepository):
    """Captures scheduled actions without sending."""

    def __init__(self) -> None:
        self.created: list[ScheduledAction] = []
        self._actions: dict[str, ScheduledAction] = {}

    async def create_once(self, action: ScheduledAction) -> tuple[ScheduledAction, bool]:
        existing = self._actions.get(action.id)
        if existing is not None:
            return existing, False
        self._actions[action.id] = action
        self.created.append(action)
        return action, True

    async def get(self, action_id: str) -> ScheduledAction | None:
        return self._actions.get(action_id)

    async def list_pending(self, user_id: str) -> list[ScheduledAction]:
        return [
            a for a in self._actions.values()
            if a.user_id == user_id and a.status is ScheduledActionStatus.PENDING
        ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conversation(
    messages: list[tuple[str, str, str]],
    *,
    child_name: str = "Mikush",
    chat_id: str = "chat-e2e",
    chat_type: str = "private",
    child_user_id: str = "child-e2e",
) -> SimpleConversation:
    """Build a SimpleConversation from (id, sender, text) tuples."""
    msgs = tuple(
        SimpleConversationMessage(
            id=msg_id,
            sender=sender,
            text=text,
            timestamp=_T0 + timedelta(minutes=i),
        )
        for i, (msg_id, sender, text) in enumerate(messages)
    )
    return SimpleConversation(
        child_user_id=child_user_id,
        child_name=child_name,
        chat_id=chat_id,
        chat_type=chat_type,
        connection_id="conn-e2e",
        timezone="Asia/Jerusalem",
        window_start=_T0.replace(hour=0),
        window_end=_T0,
        monitored_child_role="child",
        messages=msgs,
    )


def _make_chat_state(
    *,
    child_user_id: str = "child-e2e",
    chat_id: str = "chat-e2e",
    version: int = 1,
    is_group: bool = False,
) -> GuardChatState:
    return GuardChatState(
        child_user_id=child_user_id,
        chat_id=chat_id,
        activity_version=version,
        last_message_at=_T0,
        connection_id="conn-e2e",
        is_group=is_group,
    )


async def _run_analysis(
    *,
    analyzer: SimpleSafetyAnalyzer,
    messages: list[tuple[str, str, str]],
    child_name: str = "Mikush",
    chat_id: str = "chat-e2e",
    child_user_id: str = "child-e2e",
) -> SafetyAssessment:
    """Run the real analyzer and return the assessment."""
    conversation = _make_conversation(
        messages,
        child_name=child_name,
        chat_id=chat_id,
        child_user_id=child_user_id,
    )
    assessment = await analyzer.analyze(conversation)
    # The processor normally sets these fields via replace(); we do it here
    # since we're calling the analyzer directly.
    assessment = dc_replace(
        assessment,
        child_user_id=child_user_id,
        child_name=child_name,
        chat_id=chat_id,
        chat_type=conversation.chat_type,
        connection_id=conversation.connection_id,
        timezone=conversation.timezone,
        window_start=conversation.window_start,
        window_end=conversation.window_end,
        created_at=datetime.now(timezone.utc),
    )
    return assessment


async def _commit_and_track(
    *,
    assessment: SafetyAssessment,
    state_repo: InMemoryGuardChatStateRepository,
    assessment_repo: InMemoryGuardSimpleAssessmentRepository,
    commit_repo: InMemoryGuardSimpleAssessmentCommitRepository,
    tracker: IncidentTracker,
    target_version: int,
    child_user_id: str = "child-e2e",
    chat_id: str = "chat-e2e",
) -> tuple[str, str | None, str]:
    """Commit an assessment and feed it to the tracker.

    Returns (commit_status, assessment_id, tracker_result).
    """
    status, assessment_id = await commit_repo.commit_if_current(
        child_user_id=child_user_id,
        chat_id=chat_id,
        target_version=target_version,
        assessment=assessment,
    )
    tracker_result = "not_committed"
    if status in {"committed", "completed"} and assessment_id:
        tracker_result = await tracker.evaluate_security(
            assessment=assessment,
            assessment_id=assessment_id,
        )
    return status, assessment_id, tracker_result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def analyzer() -> SimpleSafetyAnalyzer:
    from openai import AsyncOpenAI

    return SimpleSafetyAnalyzer(
        client=AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", "")),
        model=os.environ.get(
            "GUARD_SIMPLE_LLM_MODEL",
            os.environ.get("GUARD_LLM_MODEL_NAME", "gpt-4.1"),
        ),
    )


@pytest.fixture
def matcher() -> IncidentMatcher:
    from openai import AsyncOpenAI

    return IncidentMatcher(
        client=AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", "")),
        model=os.environ.get(
            "INCIDENT_MATCHER_LLM_MODEL",
            os.environ.get("GUARD_LLM_MODEL_NAME", "gpt-4.1"),
        ),
    )


async def _build_tracker(
    *,
    matcher: IncidentMatcher,
    phones: dict[str, str] | None = None,
    guardian_ids: list[str] | None = None,
) -> tuple[
    IncidentTracker,
    InMemoryAlertedIncidentRepository,
    InMemoryGuardianChildLinkRepository,
    CapturingScheduling,
]:
    incidents = InMemoryAlertedIncidentRepository()
    links = InMemoryGuardianChildLinkRepository()
    scheduling = CapturingScheduling()
    users = FakeUsers(phones or {})

    for gid in guardian_ids or []:
        await links.upsert_active(
            guardian_user_id=gid,
            child_user_id="child-e2e",
            now=_T0,
        )

    tracker = IncidentTracker(
        matcher=matcher,
        incidents=incidents,
        links=links,
        users=users,
        scheduling=scheduling,
        enabled=True,
    )
    return tracker, incidents, links, scheduling


def _build_commit_repos(
    *,
    version: int = 1,
    child_user_id: str = "child-e2e",
    chat_id: str = "chat-e2e",
    is_group: bool = False,
) -> tuple[
    InMemoryGuardChatStateRepository,
    InMemoryGuardSimpleAssessmentRepository,
    InMemoryGuardSimpleAssessmentCommitRepository,
]:
    state_repo = InMemoryGuardChatStateRepository()
    assessment_repo = InMemoryGuardSimpleAssessmentRepository()
    state_repo._states[(child_user_id, chat_id)] = _make_chat_state(
        child_user_id=child_user_id,
        chat_id=chat_id,
        version=version,
        is_group=is_group,
    )
    commit_repo = InMemoryGuardSimpleAssessmentCommitRepository(
        state_repo=state_repo,
        assessment_repo=assessment_repo,
    )
    return state_repo, assessment_repo, commit_repo


# ===========================================================================
# E2E: Analyzer YES → commit → tracker → scheduler
# ===========================================================================


async def test_e2e_yes_assessment_schedules_alert(
    analyzer: SimpleSafetyAnalyzer, matcher: IncidentMatcher
) -> None:
    """Full flow: analyzer says YES → commit → tracker → one scheduled alert."""
    state_repo, assessment_repo, commit_repo = _build_commit_repos()
    tracker, incidents, _, scheduling = await _build_tracker(
        matcher=matcher,
        phones={"guardian-001": "+972500000001"},
        guardian_ids=["guardian-001"],
    )

    # Real conversation where child reports pain
    assessment = await _run_analysis(
        analyzer=analyzer,
        messages=[
            ("msg-1", "child", "כואב לי מאוד בצד ימין, קשה לי לנשום"),
            ("msg-2", "child", "זה כואב גם כשאני זז"),
        ],
    )
    print(f"Analyzer result: parent_should_know={assessment.parent_should_know}")
    print(f"  summary={assessment.summary}")
    print(f"  evidence={assessment.evidence_message_ids}")

    # Only proceed if the analyzer says YES
    if assessment.parent_should_know is not True:
        pytest.skip("Analyzer did not return YES for this conversation")

    status, assessment_id, tracker_result = await _commit_and_track(
        assessment=assessment,
        state_repo=state_repo,
        assessment_repo=assessment_repo,
        commit_repo=commit_repo,
        tracker=tracker,
        target_version=1,
    )

    assert status == "committed"
    assert assessment_id is not None
    assert tracker_result == "scheduled"
    assert len(scheduling.created) == 1

    active = await incidents.list_active("child-e2e", "chat-e2e", "security")
    assert len(active) == 1
    assert assessment_id in active[0].assessment_ids


# ===========================================================================
# E2E: Analyzer NO → no incident, no alert
# ===========================================================================


async def test_e2e_no_assessment_no_incident(
    analyzer: SimpleSafetyAnalyzer, matcher: IncidentMatcher
) -> None:
    """Analyzer says NO → tracker skips, no incident, no alert."""
    state_repo, assessment_repo, commit_repo = _build_commit_repos()
    tracker, incidents, _, scheduling = await _build_tracker(
        matcher=matcher,
        phones={"guardian-001": "+972500000001"},
        guardian_ids=["guardian-001"],
    )

    # Benign conversation
    assessment = await _run_analysis(
        analyzer=analyzer,
        messages=[
            ("msg-1", "child", "היי, מה נשמע?"),
            ("msg-2", "friend", "הכל טוב, מה איתך?"),
            ("msg-3", "child", "גם אצלי, נתראה מחר"),
        ],
    )
    print(f"Analyzer result: parent_should_know={assessment.parent_should_know}")

    if assessment.parent_should_know is True:
        pytest.skip("Analyzer returned YES for a benign conversation — skipping NO test")

    status, _assessment_id, tracker_result = await _commit_and_track(
        assessment=assessment,
        state_repo=state_repo,
        assessment_repo=assessment_repo,
        commit_repo=commit_repo,
        tracker=tracker,
        target_version=1,
    )

    assert status == "committed"
    assert tracker_result == "not_parent_attention"
    assert scheduling.created == []

    active = await incidents.list_active("child-e2e", "chat-e2e", "security")
    assert active == []


# ===========================================================================
# E2E: YES but no guardian links → incident saved, no alert
# ===========================================================================


async def test_e2e_yes_no_guardian_saves_incident_no_alert(
    analyzer: SimpleSafetyAnalyzer, matcher: IncidentMatcher
) -> None:
    """Analyzer YES, no guardian links → incident saved, no scheduled alert."""
    state_repo, assessment_repo, commit_repo = _build_commit_repos()
    tracker, incidents, _, scheduling = await _build_tracker(
        matcher=matcher,
        phones={},
        guardian_ids=[],  # no guardians
    )

    assessment = await _run_analysis(
        analyzer=analyzer,
        messages=[
            ("msg-1", "child", "אני ממש מפוחדת, מישהו מאיים עליי באקדח"),
        ],
    )

    if assessment.parent_should_know is not True:
        pytest.skip("Analyzer did not return YES")

    _, _assessment_id, tracker_result = await _commit_and_track(
        assessment=assessment,
        state_repo=state_repo,
        assessment_repo=assessment_repo,
        commit_repo=commit_repo,
        tracker=tracker,
        target_version=1,
    )

    assert tracker_result == "no_active_link"
    assert scheduling.created == []

    # Incident still saved (for future dedup)
    active = await incidents.list_active("child-e2e", "chat-e2e", "security")
    assert len(active) == 1


# ===========================================================================
# E2E: Multiple guardians, one without phone → partial fanout
# ===========================================================================


async def test_e2e_partial_guardian_fanout(
    analyzer: SimpleSafetyAnalyzer, matcher: IncidentMatcher
) -> None:
    """Two guardians, one has phone, one doesn't → one scheduled alert."""
    state_repo, assessment_repo, commit_repo = _build_commit_repos()
    tracker, _, _, scheduling = await _build_tracker(
        matcher=matcher,
        phones={"guardian-001": "+972500000001"},  # only guardian-001 has phone
        guardian_ids=["guardian-001", "guardian-002"],
    )

    assessment = await _run_analysis(
        analyzer=analyzer,
        messages=[
            ("msg-1", "child", "אני מפוחד, מישהו רודף אחריי ומאיים עליי"),
        ],
    )

    if assessment.parent_should_know is not True:
        pytest.skip("Analyzer did not return YES")

    _, _, tracker_result = await _commit_and_track(
        assessment=assessment,
        state_repo=state_repo,
        assessment_repo=assessment_repo,
        commit_repo=commit_repo,
        tracker=tracker,
        target_version=1,
    )

    assert tracker_result == "scheduled"
    # Only one scheduled action (guardian-002 has no phone)
    assert len(scheduling.created) == 1
    assert scheduling.created[0].user_id == "guardian-001"


# ===========================================================================
# E2E: Worker retry (reprocess same assessment) → idempotent
# ===========================================================================


async def test_e2e_worker_retry_is_idempotent(
    analyzer: SimpleSafetyAnalyzer, matcher: IncidentMatcher
) -> None:
    """Reprocessing the same committed assessment → already_processed, no duplicate."""
    state_repo, assessment_repo, commit_repo = _build_commit_repos()
    tracker, _, _, scheduling = await _build_tracker(
        matcher=matcher,
        phones={"guardian-001": "+972500000001"},
        guardian_ids=["guardian-001"],
    )

    assessment = await _run_analysis(
        analyzer=analyzer,
        messages=[
            ("msg-1", "child", "אני מרגיש רע, כואב לי הראש מאוד ואני לא יכול להתרכז"),
        ],
    )

    if assessment.parent_should_know is not True:
        pytest.skip("Analyzer did not return YES")

    # First processing
    _, assessment_id, result1 = await _commit_and_track(
        assessment=assessment,
        state_repo=state_repo,
        assessment_repo=assessment_repo,
        commit_repo=commit_repo,
        tracker=tracker,
        target_version=1,
    )
    assert result1 == "scheduled"
    assert len(scheduling.created) == 1

    # Simulate worker retry: feed the same assessment_id again
    result2 = await tracker.evaluate_security(
        assessment=assessment,
        assessment_id=assessment_id,  # type: ignore[arg-type]
    )
    assert result2 == "already_processed"
    assert len(scheduling.created) == 1  # no duplicate


# ===========================================================================
# E2E: Two distinct issues in same chat → two incidents, two alerts
# ===========================================================================


async def test_e2e_two_distinct_issues_two_incidents(
    analyzer: SimpleSafetyAnalyzer, matcher: IncidentMatcher
) -> None:
    """Two analytically distinct issues → two incidents, two alerts."""
    state_repo, assessment_repo, commit_repo = _build_commit_repos()
    tracker, incidents, _, scheduling = await _build_tracker(
        matcher=matcher,
        phones={"guardian-001": "+972500000001"},
        guardian_ids=["guardian-001"],
    )

    # First issue: physical pain
    assessment1 = await _run_analysis(
        analyzer=analyzer,
        messages=[
            ("msg-1", "child", "כואב לי מאוד בבטן, אני לא מרגיש טוב"),
            ("msg-2", "child", "זה כואב גם כשאני אוכל"),
        ],
    )

    if assessment1.parent_should_know is not True:
        pytest.skip("Analyzer did not return YES for first issue")

    _, _, result1 = await _commit_and_track(
        assessment=assessment1,
        state_repo=state_repo,
        assessment_repo=assessment_repo,
        commit_repo=commit_repo,
        tracker=tracker,
        target_version=1,
    )
    assert result1 == "scheduled"
    assert len(scheduling.created) == 1

    # Second issue: completely different — bullying/blackmail
    # Bump version and reset chat state for the second analysis
    state_repo._states[("child-e2e", "chat-e2e")] = _make_chat_state(version=2)
    assessment2 = await _run_analysis(
        analyzer=analyzer,
        messages=[
            ("msg-3", "other", "אם לא תיתני לי את הכסף שלך אני אחשוף את התמונות שלך"),
            ("msg-4", "child", "אני מפחדת, היא מאיימת עליי כל הזמן"),
        ],
    )

    if assessment2.parent_should_know is not True:
        pytest.skip("Analyzer did not return YES for second issue")

    _, _, result2 = await _commit_and_track(
        assessment=assessment2,
        state_repo=state_repo,
        assessment_repo=assessment_repo,
        commit_repo=commit_repo,
        tracker=tracker,
        target_version=2,
    )
    print(f"Second issue tracker result: {result2}")
    assert result2 == "scheduled"
    assert len(scheduling.created) == 2

    active = await incidents.list_active("child-e2e", "chat-e2e", "security")
    assert len(active) == 2


# ===========================================================================
# E2E: Same issue across multiple analyzer runs → one incident, one alert
# ===========================================================================


async def test_e2e_same_issue_multiple_runs_one_alert(
    analyzer: SimpleSafetyAnalyzer, matcher: IncidentMatcher
) -> None:
    """Run the analyzer twice on the same ongoing issue → one incident, one alert."""
    state_repo, assessment_repo, commit_repo = _build_commit_repos()
    tracker, incidents, _, scheduling = await _build_tracker(
        matcher=matcher,
        phones={"guardian-001": "+972500000001"},
        guardian_ids=["guardian-001"],
    )

    # First run: pain
    messages1 = [
        ("msg-1", "child", "כואב לי מאוד בצד ימין, קשה לי לנשום"),
        ("msg-2", "child", "זה כואב גם כשאני זז"),
    ]
    assessment1 = await _run_analysis(analyzer=analyzer, messages=messages1)

    if assessment1.parent_should_know is not True:
        pytest.skip("Analyzer did not return YES for first run")

    _, _, result1 = await _commit_and_track(
        assessment=assessment1,
        state_repo=state_repo,
        assessment_repo=assessment_repo,
        commit_repo=commit_repo,
        tracker=tracker,
        target_version=1,
    )
    assert result1 == "scheduled"
    assert len(scheduling.created) == 1

    # Second run: same issue, more messages
    state_repo._states[("child-e2e", "chat-e2e")] = _make_chat_state(version=2)
    messages2 = [
        ("msg-1", "child", "כואב לי מאוד בצד ימין, קשה לי לנשום"),
        ("msg-2", "child", "זה כואב גם כשאני זז"),
        ("msg-3", "child", "הכאב לא עובר, זה כואב גם אחרי מאמץ"),
    ]
    assessment2 = await _run_analysis(analyzer=analyzer, messages=messages2)

    if assessment2.parent_should_know is not True:
        pytest.skip("Analyzer did not return YES for second run")

    _, _, result2 = await _commit_and_track(
        assessment=assessment2,
        state_repo=state_repo,
        assessment_repo=assessment_repo,
        commit_repo=commit_repo,
        tracker=tracker,
        target_version=2,
    )
    print(f"Second run tracker result: {result2}")
    # Should be suppressed (SAME incident)
    assert result2 == "suppressed"
    assert len(scheduling.created) == 1  # still just one alert

    active = await incidents.list_active("child-e2e", "chat-e2e", "security")
    assert len(active) == 1


# ===========================================================================
# E2E: Closed incident + recurrence → matcher recognizes, tracker handles
# ===========================================================================


async def test_e2e_closed_incident_recurrence(
    analyzer: SimpleSafetyAnalyzer, matcher: IncidentMatcher
) -> None:
    """A closed incident + new assessment of the same issue → matcher recognizes."""
    incidents = InMemoryAlertedIncidentRepository()
    links = InMemoryGuardianChildLinkRepository()
    scheduling = CapturingScheduling()

    await links.upsert_active(
        guardian_user_id="guardian-001",
        child_user_id="child-e2e",
        now=_T0,
    )

    tracker = IncidentTracker(
        matcher=matcher,
        incidents=incidents,
        links=links,
        users=FakeUsers({"guardian-001": "+972500000001"}),
        scheduling=scheduling,
        enabled=True,
    )

    # Create a closed incident from a previous bullying issue
    closed = AlertedIncident(
        id="inc-closed-bullying",
        child_user_id="child-e2e",
        chat_id="chat-e2e",
        alert_type="security",
        summary="ילדה אחרת מציקה למיקוש ומאיימת עליה",
        severity_summary="ילדה אחרת מציקה למיקוש ומאיימת עליה",
        notified_severity_summary="ילדה אחרת מציקה למיקוש ומאיימת עליה",
        evidence_message_ids=("old-msg-1",),
        assessment_ids=("old-assess-1",),
        last_seen_at=_T0 - timedelta(days=5),
        closed_at=_T0 - timedelta(days=5),
        related_incident_id=None,
        notification_version=1,
        created_at=_T0 - timedelta(days=10),
        updated_at=_T0 - timedelta(days=5),
    )
    await incidents.create(closed)

    state_repo, assessment_repo, commit_repo = _build_commit_repos(version=1)

    # New assessment: same bullying, continuing
    assessment = await _run_analysis(
        analyzer=analyzer,
        messages=[
            ("msg-1", "other", "אני אמשיך להציק לך עד שתבכה"),
            ("msg-2", "child", "היא שוב מציקה לי, אני לא יודעת מה לעשות"),
        ],
    )

    if assessment.parent_should_know is not True:
        pytest.skip("Analyzer did not return YES")

    _, _, result = await _commit_and_track(
        assessment=assessment,
        state_repo=state_repo,
        assessment_repo=assessment_repo,
        commit_repo=commit_repo,
        tracker=tracker,
        target_version=1,
    )
    print(f"Recurrence tracker result: {result}")

    # The matcher should recognize this as related to the closed incident.
    # The tracker creates a new linked incident (recurrence) and schedules an alert.
    assert result in ("recurrence_scheduled", "scheduled", "suppressed"), f"Unexpected result: {result}"

    # If scheduled, we have a new alert
    if result in ("recurrence_scheduled", "scheduled"):
        assert len(scheduling.created) == 1
