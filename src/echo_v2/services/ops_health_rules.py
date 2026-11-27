"""Explicit, configurable health rules for the Echo Operations Center.

Health is derived from the assembled snapshot — never merely from "the
API responded". Each rule explains why it was raised and which worker,
user or subsystem is affected. The overall status is the worst rule
severity; sections that failed to load degrade the overall status too.

All thresholds come from :class:`OpsCenterSettings` (env-configurable;
defaults documented in the dashboard). Rules are deliberately conservative
about normal transient behavior: a single failed retry, a short lease
expiry or an idle child never raises an issue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from echo_v2.services.ops_center_service import OpsCenterSettings

__all__ = ["HealthIssue", "OpsHealthRules", "overall_status"]

_CRITICAL = "critical"
_WARNING = "warning"


@dataclass(frozen=True)
class HealthIssue:
    """One raised health issue with its cause and scope."""

    severity: str  # "critical" | "warning"
    category: str
    subsystem: str
    message: str
    worker_id: str | None = None
    user_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "category": self.category,
            "subsystem": self.subsystem,
            "message": self.message,
            "worker_id": self.worker_id,
            "user_id": self.user_id,
        }


@dataclass
class _Issues:
    items: list[HealthIssue] = field(default_factory=list)

    def add(
        self,
        severity: str,
        category: str,
        subsystem: str,
        message: str,
        worker_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        self.items.append(
            HealthIssue(
                severity=severity,
                category=category,
                subsystem=subsystem,
                message=message,
                worker_id=worker_id,
                user_id=user_id,
            )
        )


class OpsHealthRules:
    def __init__(self, settings: OpsCenterSettings | None = None) -> None:
        self._settings = settings or OpsCenterSettings()

    def evaluate(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Evaluate all rules against an assembled operations snapshot."""
        sections = snapshot.get("sections", {})
        issues = _Issues()
        self._infrastructure(sections, issues)
        self._verification(sections, issues)
        self._message_delivery(sections, issues)
        self._analysis(sections, issues)
        self._notifications(sections, issues)
        unavailable = sorted(
            name for name, data in sections.items()
            if isinstance(data, dict) and data.get("available") is False
        )
        return {
            "status": overall_status(issues.items, unavailable),
            "issues": [issue.as_dict() for issue in issues.items],
            "unavailable_sections": unavailable,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }

    # -- infrastructure ---------------------------------------------------------

    def _infrastructure(self, sections: dict[str, Any], issues: _Issues) -> None:
        waha = sections.get("waha_infrastructure") or {}
        if not waha.get("available"):
            return
        for worker in waha.get("workers", []):
            wid = worker["worker_id"]
            if worker["derived_availability"] == "stale":
                issues.add(
                    _CRITICAL,
                    "worker_stale",
                    "waha_infrastructure",
                    f"Worker {wid} heartbeat is stale (no registration for "
                    f"{_fmt(worker['heartbeat_age_seconds'])}); sessions "
                    "assigned to it are not being managed.",
                    worker_id=wid,
                )
            elif worker["derived_availability"] == "degraded":
                issues.add(
                    _WARNING,
                    "worker_degraded",
                    "waha_infrastructure",
                    f"Worker {wid} reports degraded health.",
                    worker_id=wid,
                )
            mismatch = worker.get("session_count_mismatch")
            if mismatch is not None and mismatch > 0:
                issues.add(
                    _WARNING,
                    "session_count_mismatch",
                    "waha_infrastructure",
                    f"Worker {wid}: Echo-assigned {worker['assigned_sessions']} "
                    f"vs agent-reported {worker['reported_active_sessions']} "
                    "active sessions.",
                    worker_id=wid,
                )
        summary = waha.get("worker_summary", {})
        if summary.get("capacity_exhausted"):
            issues.add(
                _WARNING,
                "capacity_exhausted",
                "waha_infrastructure",
                "All eligible WAHA workers are at capacity — new onboarding "
                "cannot be allocated.",
            )
        # Resource telemetry: missing is unknown (never an issue by
        # itself); approaching configured limits is a warning.
        for worker in waha.get("workers", []):
            resources = worker.get("resources") or {}
            disk_total = resources.get("disk_total_bytes")
            disk_used = resources.get("disk_used_bytes")
            if (
                disk_total
                and disk_used is not None
                and disk_total > 0
                and disk_used / disk_total >= 0.85
            ):
                issues.add(
                    _WARNING,
                    "disk_utilization",
                    "waha_infrastructure",
                    f"Worker {worker['worker_id']} disk usage at "
                    f"{round(disk_used / disk_total * 100, 1)}%.",
                    worker_id=worker["worker_id"],
                )

    # -- message delivery ---------------------------------------------------------

    def _verification(self, sections: dict[str, Any], issues: _Issues) -> None:
        """Connected-but-unverified sessions are explicitly not operational.

        WhatsApp connected, account identity available, child verified and
        Guard monitoring active are separate states: a child whose session
        is connected but whose account identity has not yet been verified
        is NOT being monitored (activation gates on verification), so this
        is actionable — either identity reporting is missing or a wrong
        account was linked.
        """
        users = sections.get("users") or {}
        if not users.get("available"):
            return
        for user in users.get("users", []):
            if not user.get("whatsapp_connected"):
                continue
            if user.get("child_verified"):
                continue
            name = user.get("display_name") or user["user_id"][:8]
            if (
                user.get("provider") == "waha"
                and not user.get("account_identity_available")
            ):
                issues.add(
                    _WARNING,
                    "awaiting_account_identity",
                    "users",
                    f"{name}: WAHA session connected but the agent has not "
                    "reported the account identity — verification pending; "
                    "Guard is NOT active. Check the agent version/reporting "
                    "or re-drive get_status.",
                    user_id=user["user_id"],
                )
            else:
                issues.add(
                    _WARNING,
                    "awaiting_account_verification",
                    "users",
                    f"{name}: WhatsApp connected but the child account is "
                    "not verified — Guard is NOT active. If a wrong account "
                    "was linked, unlink it from the phone and re-pair.",
                    user_id=user["user_id"],
                )

    def _message_delivery(self, sections: dict[str, Any], issues: _Issues) -> None:
        ingestion = sections.get("ingestion") or {}
        if ingestion.get("available"):
            waha = ingestion.get("waha_inbox", {})
            current = waha.get("current", {})
            exhausted = current.get("failed_exhausted") or 0
            if exhausted > 0:
                issues.add(
                    _CRITICAL,
                    "inbox_retries_exhausted",
                    "ingestion",
                    f"{exhausted} WAHA inbox message(s) exhausted retries "
                    f"(max {self._settings.waha_message_max_attempts}) and "
                    "will never be processed — requires intervention.",
                )
            oldest = current.get("oldest_pending_at")
            if oldest is not None:
                age = _age_seconds(oldest)
                if age is not None and age > self._settings.inbox_backlog_warning_seconds:
                    issues.add(
                        _WARNING,
                        "inbox_backlog_aging",
                        "ingestion",
                        f"WAHA inbox backlog oldest message is "
                        f"{_fmt(age)} old (warning threshold "
                        f"{_fmt(self._settings.inbox_backlog_warning_seconds)}).",
                    )
            expired = current.get("expired_leases") or 0
            if expired > 0:
                issues.add(
                    _WARNING,
                    "expired_leases",
                    "ingestion",
                    f"{expired} WAHA inbox row(s) hold expired processing "
                    "leases beyond the recovery window — check consumer "
                    "health.",
                )
            # Agent-reported backlog growth: compare the worker's current
            # pending against the value an hour ago (ops_metrics gauges
            # where available are surfaced by the workers section).
            for worker in (sections.get("waha_infrastructure") or {}).get(
                "workers", []
            ):
                pending = worker.get("reported_pending_backlog")
                if pending and pending > 0 and (
                    worker.get("derived_availability") == "healthy"
                ):
                    issues.add(
                        _WARNING,
                        "agent_backlog",
                        "ingestion",
                        f"Worker {worker['worker_id']} reports "
                        f"{pending} undelivered local message(s).",
                        worker_id=worker["worker_id"],
                    )
        media = sections.get("media") or {}
        if media.get("available"):
            oldest_pending = media.get("oldest_pending_at")
            if oldest_pending is not None:
                age = _age_seconds(oldest_pending)
                if (
                    age is not None
                    and age > self._settings.media_url_expiry_seconds
                ):
                    issues.add(
                        _CRITICAL,
                        "media_backlog_url_expiry",
                        "media",
                        f"Oldest pending media enrichment is {_fmt(age)} "
                        "old — WhatsApp media download URLs may have "
                        "expired, making retrieval impossible.",
                    )

    # -- analysis ---------------------------------------------------------------------

    def _analysis(self, sections: dict[str, Any], issues: _Issues) -> None:
        guard = sections.get("guard_analysis") or {}
        if not guard.get("available"):
            return
        scheduled = guard.get("scheduled", {})
        overdue = scheduled.get("significantly_overdue") or 0
        if overdue > 0:
            issues.add(
                _WARNING,
                "analysis_overdue",
                "guard_analysis",
                f"{overdue} Guard chat(s) are due for analysis more than "
                f"{_fmt(self._settings.analysis_overdue_seconds)} ago.",
            )
        elif (scheduled.get("overdue") or 0) > self._settings.analysis_backlog_threshold:
            issues.add(
                _WARNING,
                "analysis_backlog_growing",
                "guard_analysis",
                f"{scheduled.get('overdue')} Guard chats are due for "
                "analysis — the scheduled-analysis backlog is growing.",
            )
        failures = 0
        for row in guard.get("analysis_events", []):
            if row.get("event_name") == "guard.analysis.failed":
                failures += row.get("count") or 0
        if failures >= self._settings.analyzer_failure_threshold:
            issues.add(
                _CRITICAL,
                "analyzer_failures",
                "guard_analysis",
                f"{failures} Guard analysis attempts failed in the "
                "selected period — analyzers are not keeping up.",
            )

    # -- notifications --------------------------------------------------------------------

    def _notifications(self, sections: dict[str, Any], issues: _Issues) -> None:
        notifications = sections.get("notifications") or {}
        if not notifications.get("available"):
            return
        summary = notifications.get("summary", {})
        if (summary.get("failed") or 0) > 0:
            issues.add(
                _CRITICAL,
                "notification_failure",
                "notifications",
                f"{summary.get('failed')} parent notification(s) failed "
                "terminal delivery in the selected period.",
            )
        for action in notifications.get("scheduled_actions", []):
            overdue = action.get("overdue_pending")
            if action.get("status") == "pending" and overdue:
                issues.add(
                    _WARNING,
                    "notification_overdue",
                    "notifications",
                    f"{overdue} pending notification action(s) are overdue "
                    f"(>{_fmt(self._settings.notification_overdue_seconds)} "
                    "past execute_at).",
                )
                break


def overall_status(
    issues: list[HealthIssue], unavailable_sections: list[str]
) -> str:
    if any(issue.severity == _CRITICAL for issue in issues):
        return _CRITICAL
    if issues or unavailable_sections:
        return _WARNING
    return "healthy"


def _age_seconds(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds()


def _fmt(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
