"""Guard policy evaluation and WhatsApp alert scheduling."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from echo_v2.domain.guard import GuardAnalysisRecord
from echo_v2.domain.guard_alert import (
    GuardAlert,
    GuardAlertEvaluation,
    GuardAlertEvaluationOutcome,
    GuardAlertKind,
    GuardAlertStatus,
    GuardThresholdState,
    GuardThresholdStateStatus,
)
from echo_v2.domain.scheduling import (
    ScheduledAction,
    ScheduledActionStatus,
    ScheduledActionType,
)
from echo_v2.persistence.guard_repositories import (
    GuardAlertRepository,
    GuardAnalysisRepository,
    GuardianChildLinkRepository,
)
from echo_v2.persistence.user_repository import PostgresUserRepository
from echo_v2.services.guard_alert_policy import (
    GUARD_ALERT_POLICY_VERSION,
    AlertKind,
    ChildContext,
    ConversationContext,
    DefaultAlertPolicy,
)
from echo_v2.services.guard_analyzer import GuardAnalysis
from echo_v2.services.guard_signal_ledger import GuardSignalState
from echo_v2.services.waiting_list_token_service import WaitingListTokenService

__all__ = ["GuardAlertService"]

_NAMESPACE = uuid.NAMESPACE_URL
_DEFAULT_QUIET_SECONDS = 30 * 60
_DEFAULT_NOTIFY_TTL_SECONDS = 60 * 60
_DEFAULT_URGENT_TTL_SECONDS = 15 * 60


class _UnitOfWorkFactory(Protocol):
    def __call__(self) -> Any: ...


class GuardAlertService:
    def __init__(
        self,
        *,
        links: GuardianChildLinkRepository,
        users: PostgresUserRepository,
        token_service: WaitingListTokenService,
        unit_of_work: _UnitOfWorkFactory,
        alerts: GuardAlertRepository,
        analyses: GuardAnalysisRepository,
        base_url: str,
        enabled: bool = False,
        rollout_started_at: datetime | None = None,
        quiet_period_seconds: int = _DEFAULT_QUIET_SECONDS,
        notify_ttl_seconds: int = _DEFAULT_NOTIFY_TTL_SECONDS,
        urgent_ttl_seconds: int = _DEFAULT_URGENT_TTL_SECONDS,
        template_name: str = "guard_alert_v1",
        template_language: str = "he",
    ) -> None:
        self._links = links
        self._users = users
        self._tokens = token_service
        self._uow_factory = unit_of_work
        self._alerts = alerts
        self._analyses = analyses
        self._base_url = base_url.rstrip("/")
        self._enabled = enabled
        self._rollout_started_at = rollout_started_at
        self._quiet_period = timedelta(seconds=quiet_period_seconds)
        self._notify_ttl = timedelta(seconds=notify_ttl_seconds)
        self._urgent_ttl = timedelta(seconds=urgent_ttl_seconds)
        self._template_name = template_name
        self._template_language = template_language

    async def evaluate_committed(
        self,
        *,
        record: GuardAnalysisRecord,
        result_id: str,
        now: datetime | None = None,
    ) -> str:
        """Evaluate one committed result and atomically schedule at most one alert."""
        if not self._enabled:
            return "disabled"
        current = now or datetime.now(timezone.utc)
        if self._rollout_started_at is not None and (
            record.created_at is None or record.created_at < self._rollout_started_at
        ):
            return "before_rollout"

        link = await self._links.get_active_for_child(record.child_user_id)
        if link is None:
            return "no_active_link"
        guardian_phone = await self._users.get_phone_by_id(link.guardian_user_id)
        if guardian_phone is None:
            return "no_guardian_phone"

        async with self._uow_factory() as uow:
            alerts: GuardAlertRepository = uow.guard_alerts
            if await alerts.get_evaluation(result_id) is not None:
                return "already_evaluated"

            state_rows = await alerts.list_threshold_states(
                child_user_id=record.child_user_id,
                chat_id=record.chat_id,
            )
            cumulative_signals = tuple(
                record.diagnostics.get("cumulative_signals", list(record.signals))
            )
            cumulative_categories = tuple(
                record.diagnostics.get("cumulative_categories", list(record.categories))
            )
            analysis = GuardAnalysis(
                signals=record.signals,
                categories=record.categories,
                evidence_message_ids=record.evidence_message_ids,
                confidence=record.confidence,
                reason=record.reason,
                model=record.model,
                prompt_version=record.prompt_version,
                analyzer_version=record.analyzer_version,
                taxonomy_version=record.taxonomy_version,
            )
            signal_state = GuardSignalState(
                active_signals=cumulative_signals,
                active_categories=cumulative_categories,
            )
            prior_kind = self._prior_kind(state_rows)
            outcome = DefaultAlertPolicy().evaluate(
                analysis=analysis,
                child_context=ChildContext(),
                conversation_context=ConversationContext(prior_alert_kind=prior_kind),
                signal_state=signal_state,
            )
            family = outcome.threshold_family
            alert: GuardAlert | None = None

            for state in state_rows:
                if state.last_processed_version >= record.target_version:
                    continue
                if family == state.threshold_family and outcome.should_alert:
                    continue
                updated = self._advance_negative_state(state, record.target_version, current)
                await alerts.save_threshold_state(updated)

            if family is not None and outcome.should_alert:
                state = await alerts.get_threshold_state(
                    child_user_id=record.child_user_id,
                    chat_id=record.chat_id,
                    threshold_family=family,
                )
                if state.last_processed_version < record.target_version:
                    should_alert = state.status is GuardThresholdStateStatus.INACTIVE
                    if (
                        state.status is GuardThresholdStateStatus.ACTIVE
                        and state.current_kind is GuardAlertKind.NOTIFY
                        and outcome.kind is AlertKind.URGENT
                    ):
                        should_alert = True
                    state = GuardThresholdState(
                        child_user_id=state.child_user_id,
                        chat_id=state.chat_id,
                        threshold_family=family,
                        status=GuardThresholdStateStatus.ACTIVE,
                        episode_start_version=(
                            record.target_version
                            if state.status is GuardThresholdStateStatus.INACTIVE
                            or state.episode_start_version is None
                            else state.episode_start_version
                        ),
                        current_kind=GuardAlertKind(outcome.kind.value),
                        last_processed_version=record.target_version,
                        last_supporting_version=record.target_version,
                        last_supporting_at=current,
                        non_actionable_count=0,
                        non_actionable_since=None,
                        updated_at=current,
                    )
                    await alerts.save_threshold_state(state)
                    if should_alert:
                        alert = await self._build_alert(
                            record=record,
                            result_id=result_id,
                            guardian_user_id=link.guardian_user_id,
                            guardian_phone=guardian_phone,
                            outcome=outcome,
                            episode_start_version=state.episode_start_version or record.target_version,
                            detected_at=record.created_at or current,
                            now=current,
                        )

            if alert is None:
                await alerts.save_evaluation(
                    GuardAlertEvaluation(
                        analysis_result_id=result_id,
                        child_user_id=record.child_user_id,
                        chat_id=record.chat_id,
                        target_version=record.target_version,
                        policy_version=GUARD_ALERT_POLICY_VERSION,
                        outcome=(
                            GuardAlertEvaluationOutcome.NO_ACTION
                            if not outcome.should_alert
                            else GuardAlertEvaluationOutcome.SUPPRESSED
                        ),
                        evaluated_at=current,
                    )
                )
                return "no_action"

            await uow.scheduled_actions.create_once(self._scheduled_action(alert, guardian_phone))
            await alerts.save_alert(alert)
            await alerts.save_evaluation(
                GuardAlertEvaluation(
                    analysis_result_id=result_id,
                    child_user_id=record.child_user_id,
                    chat_id=record.chat_id,
                    target_version=record.target_version,
                    policy_version=GUARD_ALERT_POLICY_VERSION,
                    outcome=GuardAlertEvaluationOutcome.SCHEDULED,
                    evaluated_at=current,
                    alert_id=alert.id,
                )
            )
            return "scheduled"

    async def reconcile_pending(self, *, limit: int = 20) -> int:
        if not self._enabled:
            return 0
        since = self._rollout_started_at or datetime.now(timezone.utc) - timedelta(hours=24)
        records = await self._analyses.list_needing_alert_evaluation(
            since=since, limit=limit
        )
        recovered = 0
        for record in records:
            if record.id is None:
                continue
            result = await self.evaluate_committed(record=record, result_id=record.id)
            if result not in {"already_evaluated", "before_rollout", "disabled"}:
                recovered += 1
        return recovered

    async def validate_before_send(self, payload: dict[str, Any]) -> bool:
        """Recheck authorization and freshness immediately before provider I/O."""
        guardian_user_id = str(payload.get("guardian_user_id", ""))
        child_user_id = str(payload.get("child_user_id", ""))
        alert_id = str(payload.get("alert_id", ""))
        if not guardian_user_id or not child_user_id:
            return False
        link = await self._links.get_active_for_pair(
            guardian_user_id=guardian_user_id,
            child_user_id=child_user_id,
        )
        if link is None:
            if alert_id:
                await self._alerts.update_alert_status(alert_id, GuardAlertStatus.SUPPRESSED.value)
            return False
        expires_at = _parse_datetime(payload.get("expires_at"))
        if expires_at is None or datetime.now(timezone.utc) >= expires_at:
            if alert_id:
                await self._alerts.update_alert_status(alert_id, GuardAlertStatus.EXPIRED.value)
            return False
        return True

    async def _build_alert(
        self,
        *,
        record: GuardAnalysisRecord,
        result_id: str,
        guardian_user_id: str,
        guardian_phone: str,
        outcome: Any,
        episode_start_version: int,
        detected_at: datetime,
        now: datetime,
    ) -> GuardAlert:
        kind = GuardAlertKind(outcome.kind.value)
        event_key = ":".join(
            (
                "guard",
                record.child_user_id,
                record.chat_id,
                str(outcome.threshold_family),
                str(episode_start_version),
                kind.value,
            )
        )
        alert_id = str(uuid.uuid5(_NAMESPACE, event_key))
        action_id = str(uuid.uuid5(_NAMESPACE, f"{event_key}:action"))
        _session_id, raw_token = await self._tokens.issue(guardian_user_id)
        del _session_id
        category = outcome.threshold_family.replace("_", " ") if outcome.threshold_family else "safety concern"
        summary = f"Guard detected a potentially concerning {category}."
        ttl = self._urgent_ttl if kind is GuardAlertKind.URGENT else self._notify_ttl
        return GuardAlert(
            id=alert_id,
            analysis_result_id=result_id,
            guardian_user_id=guardian_user_id,
            child_user_id=record.child_user_id,
            chat_id=record.chat_id,
            kind=kind,
            categories=record.categories,
            summary=summary,
            policy_reason=outcome.reason,
            policy_version=GUARD_ALERT_POLICY_VERSION,
            status=GuardAlertStatus.SCHEDULED,
            scheduled_action_id=action_id,
            logical_event_key=event_key,
            threshold_family=str(outcome.threshold_family),
            episode_start_version=episode_start_version,
            detected_at=detected_at,
            expires_at=detected_at + ttl,
            created_at=now,
            metadata={"guardian_phone": guardian_phone, "review_token": raw_token},
        )

    def _scheduled_action(self, alert: GuardAlert, guardian_phone: str) -> ScheduledAction:
        return ScheduledAction(
            id=alert.scheduled_action_id,
            user_id=alert.guardian_user_id,
            type=ScheduledActionType.SEND_BOT_MESSAGE,
            execute_at_utc=alert.created_at,
            timezone="UTC",
            status=ScheduledActionStatus.PENDING,
            payload={
                "kind": "guard_alert",
                "chat_id": guardian_phone,
                "guardian_user_id": alert.guardian_user_id,
                "child_user_id": alert.child_user_id,
                "alert_id": alert.id,
                "detected_at": alert.detected_at.isoformat(),
                "expires_at": alert.expires_at.isoformat(),
                "idempotency_key": f"guard:alert:{alert.id}:guardian:{alert.guardian_user_id}",
                "message": alert.summary,
                "template": {
                    "name": self._template_name,
                    "language": self._template_language,
                    "body_params": [alert.summary],
                    "url_suffix": f"q/guard/{alert.metadata['review_token'] if alert.metadata else ''}",
                },
            },
        )

    def _advance_negative_state(
        self, state: GuardThresholdState, target_version: int, now: datetime
    ) -> GuardThresholdState:
        count = state.non_actionable_count + 1
        quiet_since = state.non_actionable_since or now
        supporting_at = state.last_supporting_version
        should_close = (
            state.status is GuardThresholdStateStatus.ACTIVE
            and count >= 2
            and state.last_supporting_at is not None
            and now - state.last_supporting_at >= self._quiet_period
        )
        return GuardThresholdState(
            child_user_id=state.child_user_id,
            chat_id=state.chat_id,
            threshold_family=state.threshold_family,
            status=GuardThresholdStateStatus.INACTIVE if should_close else state.status,
            episode_start_version=state.episode_start_version,
            current_kind=state.current_kind,
            last_processed_version=target_version,
            last_supporting_version=supporting_at,
            last_supporting_at=state.last_supporting_at,
            non_actionable_count=count,
            non_actionable_since=quiet_since,
            updated_at=now,
        )

    @staticmethod
    def _prior_kind(states: list[GuardThresholdState]) -> Any:
        kinds = [state.current_kind for state in states if state.status is GuardThresholdStateStatus.ACTIVE]
        if any(kind is GuardAlertKind.URGENT for kind in kinds):
            return AlertKind.URGENT
        if any(kind is GuardAlertKind.NOTIFY for kind in kinds):
            return AlertKind.NOTIFY
        return AlertKind.NONE


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        return None
    return result if result.tzinfo is not None else result.replace(tzinfo=timezone.utc)
