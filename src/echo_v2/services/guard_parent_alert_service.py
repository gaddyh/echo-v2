"""Guardian-scoped, read-only Guard alert view."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from echo_v2.domain.guard_alert import GuardAlert
from echo_v2.persistence.guard_repositories import GuardAlertRepository
from echo_v2.services.waiting_list_token_service import WaitingListTokenService

__all__ = ["GuardParentAlertService", "GuardParentAlertView"]


class _UserInfoResolver(Protocol):
    async def get_user_info_by_id(
        self, user_id: str
    ) -> tuple[str, str | None] | None: ...


@dataclass(frozen=True)
class GuardParentAlertView:
    """The intentionally small, parent-safe representation of one alert."""

    alert_id: str
    child_name: str
    detected_at: datetime
    severity: str
    categories: tuple[str, ...]
    summary: str
    concern: str
    expired: bool


class GuardParentAlertService:
    def __init__(
        self,
        *,
        token_service: WaitingListTokenService,
        alerts: GuardAlertRepository,
        users: _UserInfoResolver,
    ) -> None:
        self._tokens = token_service
        self._alerts = alerts
        self._users = users

    async def get_alert(self, *, session_id: str) -> GuardParentAlertView | None:
        resolved = await self._tokens.resolve_session(session_id)
        if resolved is None:
            return None
        alert = await self._alerts.get_by_session_id(
            session_id=resolved.session_id,
            guardian_user_id=resolved.user_id,
        )
        if alert is None:
            return None
        return await self._view(alert)

    async def _view(self, alert: GuardAlert) -> GuardParentAlertView:
        child_name = "הילד/ה"
        info = await self._users.get_user_info_by_id(alert.child_user_id)
        if info is not None and info[1]:
            child_name = info[1]
        detected_at = _aware(alert.detected_at)
        return GuardParentAlertView(
            alert_id=alert.id,
            child_name=child_name,
            detected_at=detected_at,
            severity=alert.kind.value,
            categories=tuple(_humanize(category) for category in alert.categories),
            summary=alert.summary,
            concern=_parent_safe_reason(alert.policy_reason),
            expired=datetime.now(timezone.utc) >= _aware(alert.expires_at),
        )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _humanize(value: str) -> str:
    return value.replace("_", " ")


def _parent_safe_reason(reason: str) -> str:
    if reason == "Immediate or escalating high-risk evidence.":
        return "זוהה סימן מיידי או מתגבר שדורש תשומת לב."
    if reason == "A deterministic Guard alert threshold was reached.":
        return "זוהה שילוב של סימנים שעבר את סף ההתרעה של Guard."
    return "זוהה דפוס בשיחה שייתכן שחשוב לשים לב אליו."
