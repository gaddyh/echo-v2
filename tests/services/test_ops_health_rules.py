"""Unit tests for the OpsHealthRules evaluation engine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from echo_v2.services.ops_center_service import OpsCenterSettings
from echo_v2.services.ops_health_rules import OpsHealthRules, overall_status

NOW = datetime(2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc)


def _settings(**overrides) -> OpsCenterSettings:
    return OpsCenterSettings(
        waha_heartbeat_timeout_seconds=90.0,
        waha_message_max_attempts=3,
        **overrides,
    )


def _snapshot(sections: dict) -> dict:
    return {"sections": sections}


def test_empty_snapshot_is_healthy() -> None:
    rules = OpsHealthRules(_settings())
    result = rules.evaluate(_snapshot({}))
    assert result["status"] == "healthy"
    assert result["issues"] == []
    assert result["unavailable_sections"] == []


def test_stale_worker_is_critical_and_names_worker() -> None:
    rules = OpsHealthRules(_settings())
    snapshot = _snapshot({
        "waha_infrastructure": {
            "available": True,
            "worker_summary": {"capacity_exhausted": False},
            "workers": [
                {
                    "worker_id": "w-1",
                    "derived_availability": "stale",
                    "heartbeat_age_seconds": 3600.0,
                    "session_count_mismatch": None,
                    "resources": None,
                }
            ],
        }
    })
    result = rules.evaluate(snapshot)
    assert result["status"] == "critical"
    issue = result["issues"][0]
    assert issue["category"] == "worker_stale"
    assert issue["worker_id"] == "w-1"
    assert "w-1" in issue["message"]


def test_degraded_worker_and_mismatch_are_warnings() -> None:
    rules = OpsHealthRules(_settings())
    snapshot = _snapshot({
        "waha_infrastructure": {
            "available": True,
            "worker_summary": {"capacity_exhausted": False},
            "workers": [
                {
                    "worker_id": "w-2",
                    "derived_availability": "degraded",
                    "heartbeat_age_seconds": 10.0,
                    "session_count_mismatch": 2,
                    "assigned_sessions": 4,
                    "reported_active_sessions": 2,
                    "resources": None,
                }
            ],
        }
    })
    result = rules.evaluate(snapshot)
    assert result["status"] == "warning"
    categories = {i["category"] for i in result["issues"]}
    assert categories == {"worker_degraded", "session_count_mismatch"}


def test_missing_resource_telemetry_is_not_an_issue() -> None:
    rules = OpsHealthRules(_settings())
    snapshot = _snapshot({
        "waha_infrastructure": {
            "available": True,
            "worker_summary": {},
            "workers": [
                {
                    "worker_id": "w-3",
                    "derived_availability": "healthy",
                    "resources": None,
                }
            ],
        }
    })
    result = rules.evaluate(snapshot)
    assert result["status"] == "healthy"
    assert not any("resource" in i["category"] for i in result["issues"])


def test_resource_utilization_near_limit_is_warning() -> None:
    rules = OpsHealthRules(_settings())
    snapshot = _snapshot({
        "waha_infrastructure": {
            "available": True,
            "worker_summary": {},
            "workers": [
                {
                    "worker_id": "w-4",
                    "derived_availability": "healthy",
                    "resources": {
                        "disk_total_bytes": 100,
                        "disk_used_bytes": 90,
                    },
                }
            ],
        }
    })
    result = rules.evaluate(snapshot)
    assert result["status"] == "warning"
    assert result["issues"][0]["category"] == "disk_utilization"


def test_exhausted_inbox_retries_are_critical() -> None:
    rules = OpsHealthRules(_settings())
    snapshot = _snapshot({
        "ingestion": {
            "available": True,
            "waha_inbox": {
                "current": {
                    "failed_exhausted": 4,
                    "oldest_pending_at": None,
                    "expired_leases": 0,
                }
            },
        },
    })
    result = rules.evaluate(snapshot)
    assert result["status"] == "critical"
    issue = result["issues"][0]
    assert issue["category"] == "inbox_retries_exhausted"
    assert "4" in issue["message"]
    assert "never be processed" in issue["message"]


def test_aging_backlog_and_expired_leases_are_warnings() -> None:
    rules = OpsHealthRules(_settings(inbox_backlog_warning_seconds=3600))
    snapshot = _snapshot({
        "ingestion": {
            "available": True,
            "waha_inbox": {
                "current": {
                    "failed_exhausted": 0,
                    "expired_leases": 2,
                    "oldest_pending_at": (NOW - timedelta(hours=2)).isoformat(),
                }
            },
        },
    })
    result = rules.evaluate(snapshot)
    categories = {i["category"] for i in result["issues"]}
    assert categories == {"inbox_backlog_aging", "expired_leases"}


def test_media_backlog_approaching_url_expiry_is_critical() -> None:
    rules = OpsHealthRules(_settings(media_url_expiry_seconds=86400))
    snapshot = _snapshot({
        "media": {
            "available": True,
            "oldest_pending_at": NOW - timedelta(hours=30),
        },
    })
    result = rules.evaluate(snapshot)
    assert result["status"] == "critical"
    assert result["issues"][0]["category"] == "media_backlog_url_expiry"


def test_agent_reported_backlog_is_warning() -> None:
    rules = OpsHealthRules(_settings())
    snapshot = _snapshot({
        "ingestion": {
            "available": True,
            "waha_inbox": {"current": {}},
        },
        "waha_infrastructure": {
            "available": True,
            "workers": [
                {
                    "worker_id": "w-5",
                    "derived_availability": "healthy",
                    "reported_pending_backlog": 12,
                }
            ],
        },
    })
    result = rules.evaluate(snapshot)
    assert any(i["category"] == "agent_backlog" for i in result["issues"])


def test_analysis_overdue_and_persistent_failures() -> None:
    rules = OpsHealthRules(
        _settings(analysis_overdue_seconds=600, analyzer_failure_threshold=5)
    )
    snapshot = _snapshot({
        "guard_analysis": {
            "available": True,
            "scheduled": {
                "overdue": 7,
                "significantly_overdue": 3,
            },
            "analysis_events": [
                {"event_name": "guard.analysis.failed", "count": 6},
            ],
        },
    })
    result = rules.evaluate(snapshot)
    assert result["status"] == "critical"
    categories = {i["category"] for i in result["issues"]}
    assert categories == {"analysis_overdue", "analyzer_failures"}


def test_growing_analysis_backlog_threshold() -> None:
    rules = OpsHealthRules(_settings(analysis_backlog_threshold=20))
    snapshot = _snapshot({
        "guard_analysis": {
            "available": True,
            "scheduled": {"overdue": 25, "significantly_overdue": 0},
            "analysis_events": [],
        },
    })
    result = rules.evaluate(snapshot)
    assert result["issues"][0]["category"] == "analysis_backlog_growing"


def test_notification_overdue_and_terminal_failure() -> None:
    rules = OpsHealthRules(_settings(notification_overdue_seconds=900))
    snapshot = _snapshot({
        "notifications": {
            "available": True,
            "summary": {"failed": 2},
            "scheduled_actions": [
                {"status": "pending", "overdue_pending": 3},
            ],
        },
    })
    result = rules.evaluate(snapshot)
    assert result["status"] == "critical"
    categories = {i["category"] for i in result["issues"]}
    assert categories == {"notification_failure", "notification_overdue"}


def test_unavailable_sections_degrade_status() -> None:
    rules = OpsHealthRules(_settings())
    snapshot = _snapshot({
        "media": {"available": False, "error": "InternalError"},
    })
    result = rules.evaluate(snapshot)
    assert result["status"] == "warning"
    assert result["unavailable_sections"] == ["media"]


def test_overall_status_precedence() -> None:
    assert overall_status([], []) == "healthy"
    assert overall_status([], ["media"]) == "warning"
    assert overall_status([], []) == "healthy"


def test_capacity_exhausted_warning_for_onboarding() -> None:
    rules = OpsHealthRules(_settings())
    snapshot = _snapshot({
        "waha_infrastructure": {
            "available": True,
            "worker_summary": {"capacity_exhausted": True},
            "workers": [],
        }
    })
    result = rules.evaluate(snapshot)
    assert any(
        i["category"] == "capacity_exhausted" for i in result["issues"]
    )


def test_fresh_media_backlog_below_expiry_is_not_an_issue() -> None:
    rules = OpsHealthRules(_settings(media_url_expiry_seconds=86400))
    snapshot = _snapshot({
        "media": {
            "available": True,
            "oldest_pending_at": NOW - timedelta(minutes=30),
        }
    })
    result = rules.evaluate(snapshot)
    assert result["issues"] == []


def test_analysis_backlog_below_threshold_is_not_an_issue() -> None:
    rules = OpsHealthRules(_settings(analysis_backlog_threshold=20))
    snapshot = _snapshot({
        "guard_analysis": {
            "available": True,
            "scheduled": {"overdue": 5, "significantly_overdue": 0},
            "analysis_events": [
                {"event_name": "guard.analysis.failed", "count": 1},
            ],
        }
    })
    result = rules.evaluate(snapshot)
    assert result["issues"] == []


def test_pending_notification_not_overdue_is_not_an_issue() -> None:
    rules = OpsHealthRules(_settings(notification_overdue_seconds=900))
    snapshot = _snapshot({
        "notifications": {
            "available": True,
            "summary": {"failed": 0},
            "scheduled_actions": [
                {"status": "pending", "overdue_pending": 0},
            ],
        }
    })
    result = rules.evaluate(snapshot)
    assert result["issues"] == []
