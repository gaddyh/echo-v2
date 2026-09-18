"""Programmatically build the Echo v2 production dashboard in LangSmith.

Creates a section + charts via the LangSmith charts REST API
(https://docs.langchain.com/langsmith/smith-api/charts/create-chart).

Requires these env vars (already in .env):
    LANGSMITH_API_KEY
    LANGSMITH_PROJECT_ID   — tracing project UUID to scope charts to

Usage:
    .venv/bin/python scripts/setup_langsmith_dashboard.py [section_title] [project_id] [org_id]

    section_title  — dashboard name (default: "Echo v2 — Production")
    project_id     — tracing project UUID (default: LANGSMITH_PROJECT_ID env var)
    org_id         — workspace/org UUID for the URL (default: "default")

Idempotent: if a section with the same title already exists, reuses it.
Charts are created fresh each run (delete old ones in the UI if needed).
"""

from __future__ import annotations

import os
import sys

import httpx

# --- config ----------------------------------------------------------------

API_BASE = os.environ.get("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")
# Prefer an org-level key (lsv2_sk_...) for the charts API; project tokens
# (lsv2_pt_...) can only write traces, not access management endpoints.
API_KEY = os.environ.get("LANGSMITH_ORG_API_KEY") or os.environ.get(
    "LANGSMITH_API_KEY", ""
)
PROJECT_ID = os.environ.get("LANGSMITH_PROJECT_ID", "")

SECTION_TITLE = "Echo v2 — Production"

if not API_KEY:
    sys.exit("LANGSMITH_API_KEY is required (set in .env or environment).")
if not PROJECT_ID:
    sys.exit("LANGSMITH_PROJECT_ID is required (set in .env or environment).")

HEADERS = {"x-api-key": API_KEY, "Content-Type": "application/json"}


# --- helpers ---------------------------------------------------------------


def project_filter() -> dict:
    """Filter definition scoping all charts to our tracing project."""
    return {
        "source_type": "tracing_project",
        "project_ids": [PROJECT_ID],
    }


def count_series(name: str, run_filter: str | None = None) -> dict:
    """A run-count series, optionally filtered by run name."""
    series: dict = {
        "name": name,
        "metric_definition": {"type": "count"},
        "filter_definition": project_filter(),
    }
    if run_filter:
        series["filters"] = {"filter": run_filter}
    return series


def latency_series(name: str, p: float, run_filter: str | None = None) -> dict:
    """A latency percentile series."""
    series: dict = {
        "name": name,
        "metric_definition": {
            "type": "percentile",
            "field": "latency_seconds",
            "params": {"p": p},
        },
        "filter_definition": project_filter(),
    }
    if run_filter:
        series["filters"] = {"filter": run_filter}
    return series


def error_rate_series(name: str, run_filter: str | None = None) -> dict:
    """Error-rate series using the built-in error_rate metric."""
    series: dict = {
        "name": name,
        "metric": "error_rate",
        "filter_definition": project_filter(),
    }
    if run_filter:
        series["filters"] = {"filter": run_filter}
    return series


def group_by_name() -> list[dict]:
    return [{"attribute": "name"}]


# --- API calls -------------------------------------------------------------


def get_or_create_section(title: str) -> str:
    """Find an existing section by title, or create a new one. Returns UUID."""
    resp = httpx.get(
        f"{API_BASE}/api/v1/charts/section",
        headers=HEADERS,
        params={"title_contains": title},
        timeout=30,
    )
    resp.raise_for_status()
    sections = resp.json()
    for s in sections:
        if s.get("title") == title:
            print(f"  Reusing existing section: {title} ({s['id']})")
            return s["id"]

    resp = httpx.post(
        f"{API_BASE}/api/v1/charts/section",
        headers=HEADERS,
        json={"title": title},
        timeout=30,
    )
    resp.raise_for_status()
    section = resp.json()
    print(f"  Created section: {title} ({section['id']})")
    return section["id"]


def create_chart(section_id: str, chart: dict, index: int) -> None:
    chart["section_id"] = section_id
    chart["index"] = index
    resp = httpx.post(
        f"{API_BASE}/api/v1/charts/create",
        headers=HEADERS,
        json=chart,
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"  ERROR creating '{chart['title']}': {resp.status_code} {resp.text}")
    else:
        print(f"  Created chart: {chart['title']}")


# --- dashboard definition --------------------------------------------------


def build_overview_dashboard(section_id: str) -> None:
    """High-level dashboard — trends by days/weeks."""
    charts: list[dict] = [
        # 1. KPI — total analyzer runs
        {
            "title": "Analyzer Runs",
            "description": "Total wfm.llm_analyze runs (one per LLM analysis call)",
            "chart_type": "kpi",
            "series": [count_series("runs", 'eq(name, "wfm.llm_analyze")')],
        },
        # 2. Line — LLM analysis latency p50/p99
        {
            "title": "LLM Analysis Latency",
            "description": "p50 and p99 latency for wfm.llm_analyze",
            "chart_type": "line",
            "series": [
                latency_series("p50", 0.5, 'eq(name, "wfm.llm_analyze")'),
                latency_series("p99", 0.99, 'eq(name, "wfm.llm_analyze")'),
            ],
        },
        # 3. Bar — action distribution (all wfm.action.* counts)
        {
            "title": "User Actions",
            "description": "Count of each wfm.action.* span (handled, snooze, done, dismiss_*)",
            "chart_type": "bar",
            "series": [
                {
                    "name": "actions",
                    "metric_definition": {"type": "count"},
                    "filter_definition": project_filter(),
                    "filters": {"filter": 'search(name, "wfm.action.")'},
                    "group_by_definitions": group_by_name(),
                }
            ],
        },
        # 4. Line — inbound message volume (green + 360)
        {
            "title": "Inbound Message Volume",
            "description": "wfm.ingest.green + wfm.webhook.dialog360 runs over time",
            "chart_type": "line",
            "series": [
                count_series("green arrivals", 'eq(name, "wfm.ingest.green")'),
                count_series("360 webhook", 'eq(name, "wfm.webhook.dialog360")'),
            ],
        },
        # 5. Line — outbound HTTP volume (green + 360)
        {
            "title": "Outbound HTTP Volume",
            "description": "wfm.http.green + wfm.http.dialog360 runs over time",
            "chart_type": "line",
            "series": [
                count_series("green HTTP", 'eq(name, "wfm.http.green")'),
                count_series("360 HTTP", 'eq(name, "wfm.http.dialog360")'),
            ],
        },
        # 6. Line — bot send latency
        {
            "title": "Bot Send Latency",
            "description": "p95 latency for wfm.scheduling.bot_send",
            "chart_type": "line",
            "series": [
                latency_series("p95", 0.95, 'eq(name, "wfm.scheduling.bot_send")'),
            ],
        },
        # 7. Line — scheduled action execution volume
        {
            "title": "Scheduled Action Executions",
            "description": "wfm.scheduling.execute runs over time",
            "chart_type": "line",
            "series": [
                count_series("executions", 'eq(name, "wfm.scheduling.execute")'),
            ],
        },
        # 8. KPI — error rate across all runs
        {
            "title": "Error Rate",
            "description": "Error rate across all traced runs in this project",
            "chart_type": "kpi",
            "series": [error_rate_series("error_rate")],
        },
        # 9. Bar — feedback handle volume
        {
            "title": "Feedback Handles",
            "description": "wfm.feedback.handle runs (user clicked a feedback button)",
            "chart_type": "bar",
            "series": [
                count_series("feedback", 'eq(name, "wfm.feedback.handle")'),
            ],
        },
    ]

    for i, chart in enumerate(charts):
        create_chart(section_id, chart, i)


def build_realtime_dashboard(section_id: str) -> None:
    """Real-time dashboard — last 1-5 hours operational health."""
    charts: list[dict] = [
        # 1. Line — recent analyzer runs (is the analyzer working?)
        {
            "title": "Recent Analyzer Runs",
            "description": "wfm.analysis runs — should show activity every ~5 min",
            "chart_type": "line",
            "series": [
                count_series("analysis", 'eq(name, "wfm.analysis")'),
            ],
        },
        # 2. Line — recent inbound messages (are messages flowing?)
        {
            "title": "Recent Inbound Messages",
            "description": "wfm.ingest.green runs — messages arriving from Green API",
            "chart_type": "line",
            "series": [
                count_series("green arrivals", 'eq(name, "wfm.ingest.green")'),
            ],
        },
        # 3. Line — recent errors (any failures right now?)
        {
            "title": "Recent Errors",
            "description": "Error count across all traced runs — spikes indicate problems",
            "chart_type": "line",
            "series": [
                {
                    "name": "errors",
                    "metric_definition": {"type": "count"},
                    "filter_definition": project_filter(),
                    "filters": {"filter": 'eq(status, "error")'},
                },
            ],
        },
        # 4. Line — recent bot sends (are we sending messages?)
        {
            "title": "Recent Bot Sends",
            "description": "wfm.scheduling.bot_send runs — outbound messages to users",
            "chart_type": "line",
            "series": [
                count_series("bot sends", 'eq(name, "wfm.scheduling.bot_send")'),
            ],
        },
        # 5. Line — recent feedback handles (user interactions)
        {
            "title": "Recent Feedback Handles",
            "description": "wfm.feedback.handle runs — users clicking feedback buttons",
            "chart_type": "line",
            "series": [
                count_series("feedback", 'eq(name, "wfm.feedback.handle")'),
            ],
        },
    ]

    for i, chart in enumerate(charts):
        create_chart(section_id, chart, i)


# --- main ------------------------------------------------------------------


def main() -> None:
    global PROJECT_ID
    project_id = sys.argv[1] if len(sys.argv) > 1 else PROJECT_ID
    org_id = sys.argv[2] if len(sys.argv) > 2 else "default"
    # Override the module-level PROJECT_ID used by project_filter()
    PROJECT_ID = project_id
    print("Building LangSmith dashboards")
    print(f"  Project ID: {PROJECT_ID}")
    print(f"  Org ID: {org_id}")
    print(f"  API: {API_BASE}")

    # Overview dashboard (days/weeks)
    overview_title = "echo v2 overview"
    overview_id = get_or_create_section(overview_title)
    build_overview_dashboard(overview_id)
    print(f"Overview dashboard: https://smith.langchain.com/o/{org_id}/monitor/dashboards/{overview_id}")

    # Real-time dashboard (1-5 hours)
    realtime_title = "echo v2 realtime"
    realtime_id = get_or_create_section(realtime_title)
    build_realtime_dashboard(realtime_id)
    print(f"Realtime dashboard: https://smith.langchain.com/o/{org_id}/monitor/dashboards/{realtime_id}")
    print("  (Monitoring tab in the left sidebar -> Dashboards)")


if __name__ == "__main__":
    main()
