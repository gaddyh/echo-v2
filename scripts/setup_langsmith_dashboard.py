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


def cost_series(name: str, run_filter: str | None = None) -> dict:
    """A total-cost sum series (USD)."""
    series: dict = {
        "name": name,
        "metric_definition": {"type": "sum", "field": "total_cost"},
        "filter_definition": project_filter(),
    }
    if run_filter:
        series["filters"] = {"filter": run_filter}
    return series


def token_series(name: str, field: str, run_filter: str | None = None) -> dict:
    """A token-count sum series (field: total_tokens, prompt_tokens, completion_tokens)."""
    series: dict = {
        "name": name,
        "metric_definition": {"type": "sum", "field": field},
        "filter_definition": project_filter(),
    }
    if run_filter:
        series["filters"] = {"filter": run_filter}
    return series


def group_by_name() -> list[dict]:
    return [{"attribute": "name"}]


def group_by_metadata(path: str) -> list[dict]:
    """Group by a metadata key (e.g. 'user_id_hash', 'chat_id_hash')."""
    return [{"attribute": "metadata", "path": path}]


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


def list_charts_in_section(section_id: str) -> list[dict]:
    """List all charts in a section. Returns list of chart dicts with 'id' and 'title'.

    Uses POST /api/v1/charts/section/{section_id} which returns a single
    section with its charts.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=365)
    resp = httpx.post(
        f"{API_BASE}/api/v1/charts/section/{section_id}",
        headers=HEADERS,
        json={
            "omit_data": True,
            "start_time": start.isoformat(),
            "end_time": now.isoformat(),
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("charts", [])


def delete_chart(chart_id: str) -> bool:
    """Delete a chart by ID. Returns True on success."""
    resp = httpx.delete(
        f"{API_BASE}/api/v1/charts/{chart_id}",
        headers=HEADERS,
        timeout=30,
    )
    return resp.status_code == 200


def clean_section(section_id: str, title: str) -> None:
    """Delete all charts in a section so it can be repopulated cleanly."""
    charts = list_charts_in_section(section_id)
    if not charts:
        print(f"  Section '{title}' has no charts to clean.")
        return
    print(f"  Cleaning {len(charts)} chart(s) from '{title}'...")
    for chart in charts:
        chart_id = chart.get("id")
        chart_title = chart.get("title", "?")
        if chart_id and delete_chart(chart_id):
            print(f"    Deleted: {chart_title}")
        else:
            print(f"    FAILED to delete: {chart_title} ({chart_id})")


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
                    "filters": {"filter": 'search("wfm.action.")'},
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
        # 10. KPI — total LLM cost (analyzer + judge)
        {
            "title": "Total LLM Cost",
            "description": "Sum of total_cost across all LLM runs (analyzer + judge)",
            "chart_type": "kpi",
            "series": [cost_series("cost", 'eq(run_type, "llm")')],
        },
        # 11. Line — cost over time: analyzer vs judge
        {
            "title": "LLM Cost Over Time",
            "description": "Cost (USD) split by analyzer (wfm.llm_analyze) vs judge (wfm.judge) traces",
            "chart_type": "line",
            "series": [
                cost_series("analyzer", 'eq(name, "wfm.llm_analyze")'),
                cost_series("judge", 'eq(name, "wfm.judge")'),
            ],
        },
        # 12. Line — token usage over time: prompt vs completion
        {
            "title": "Token Usage",
            "description": "Prompt vs completion tokens across all LLM runs",
            "chart_type": "line",
            "series": [
                token_series("prompt", "prompt_tokens", 'eq(run_type, "llm")'),
                token_series("completion", "completion_tokens", 'eq(run_type, "llm")'),
            ],
        },
        # 13. Bar — cost per user (top spenders)
        {
            "title": "Cost Per User",
            "description": "Total LLM cost grouped by user_id_hash (from wfm.analysis metadata)",
            "chart_type": "bar",
            "series": [
                {
                    "name": "cost",
                    "metric_definition": {"type": "sum", "field": "total_cost"},
                    "filter_definition": project_filter(),
                    "filters": {"filter": 'eq(name, "wfm.analysis")'},
                    "group_by_definitions": group_by_metadata("user_id_hash"),
                }
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


def ratio_series(name: str, numerator_filter: str, denominator_filter: str) -> dict:
    """A ratio of two filtered run counts (for quality/funnel rates)."""
    return {
        "name": name,
        "metric_definition": {
            "type": "ratio",
            "numerator": {"type": "count", "filter": numerator_filter},
            "denominator": {"type": "count", "filter": denominator_filter},
        },
        "filter_definition": project_filter(),
    }


def build_usage_dashboard(section_id: str) -> None:
    """Product quality dashboard for the waiting-list experience.

    The action trace names intentionally encode the semantic outcome, rather
    than relying on button metadata. This keeps the dashboard useful even
    when the UI labels change.
    """
    action_names = {
        "resolved now": "done",
        "deferred": "snooze",
        "acted externally": "send",
        "rejected": "false_positive",
        "ignored / not needed": "not_needed",
    }
    all_actions = 'search("wfm.miniapp.action.")'
    useful_actions = (
        'or(eq(name, "wfm.miniapp.action.done"), '
        'eq(name, "wfm.miniapp.action.snooze"), '
        'eq(name, "wfm.miniapp.action.send"))'
    )
    rejected_actions = (
        'or(eq(name, "wfm.miniapp.action.false_positive"), '
        'eq(name, "wfm.miniapp.action.not_needed"))'
    )
    charts: list[dict] = [
        {
            "title": "False Positive Rate Over Time",
            "description": "False-positive detections divided by all successful semantic actions",
            "chart_type": "line",
            "series": [ratio_series("false-positive rate", 'eq(name, "wfm.miniapp.action.false_positive")', all_actions)],
        },
        {
            "title": "Useful vs Rejected Detections",
            "description": "Useful = done + snooze + send; rejected = false_positive + not_needed",
            "chart_type": "bar",
            "series": [
                count_series("useful detections", useful_actions),
                count_series("rejected detections", rejected_actions),
            ],
        },
        {
            "title": "Action Mix Per Detected Item",
            "description": "Semantic outcomes: resolved now, deferred, acted externally, rejected, ignored",
            "chart_type": "bar",
            "series": [
                count_series(label, f'eq(name, "wfm.miniapp.action.{button}")')
                for label, button in action_names.items()
            ],
        },
        {
            "title": "Detection Quality",
            "description": "WFM detections, user false positives, done rate, and annotation queue additions",
            "chart_type": "line",
            "series": [
                count_series("WFM detections", 'eq(name, "wfm.analysis")'),
                count_series("false positives", 'eq(name, "wfm.miniapp.action.false_positive")'),
                count_series("done", 'eq(name, "wfm.miniapp.action.done")'),
                count_series("annotation candidates", 'eq(name, "wfm.user_false_positive")'),
            ],
        },
        {
            "title": "Digest Open Action Funnel",
            "description": "Digest sent → mini-app opened → at least one meaningful action",
            "chart_type": "bar",
            "series": [
                count_series("digest sent", 'eq(name, "wfm.digest.sent")'),
                count_series("mini-app opened", 'eq(name, "wfm.miniapp.opened")'),
                count_series("action taken", useful_actions),
            ],
        },
        {
            "title": "Onboarding Funnel",
            "description": "Intro → consent → name → pairing → pool → QR → authorization",
            "chart_type": "bar",
            "series": [
                count_series(event.replace("_", " "), f'eq(name, "wfm.onboarding.{event}")')
                for event in (
                    "intro_shown", "consent", "name_entered", "pairing_start",
                    "pool_hit", "pool_miss", "qr_shown", "authorized",
                )
            ],
        },
        {
            "title": "Meaningful Actions By User",
            "description": "Meaningful actions per user, grouped by privacy-safe user hash",
            "chart_type": "bar",
            "series": [
                {
                    "name": "meaningful actions",
                    "metric_definition": {"type": "count"},
                    "filter_definition": project_filter(),
                    "filters": {"filter": useful_actions},
                    "group_by_definitions": group_by_metadata("user_id_hash"),
                }
            ],
        },
    ]
    for i, chart in enumerate(charts):
        create_chart(section_id, chart, i)


# --- main ------------------------------------------------------------------


def main() -> None:
    global PROJECT_ID
    # Parse args: project_id, org_id, and optional --clean flag.
    args = [a for a in sys.argv[1:] if a != "--clean"]
    clean = "--clean" in sys.argv
    project_id = args[0] if len(args) > 0 else PROJECT_ID
    org_id = args[1] if len(args) > 1 else "default"
    # Override the module-level PROJECT_ID used by project_filter()
    PROJECT_ID = project_id
    print("Building LangSmith dashboards")
    print(f"  Project ID: {PROJECT_ID}")
    print(f"  Org ID: {org_id}")
    print(f"  API: {API_BASE}")
    if clean:
        print("  --clean: will delete existing charts before recreating")

    # Overview dashboard (days/weeks)
    overview_title = "echo v2 overview"
    overview_id = get_or_create_section(overview_title)
    if clean:
        clean_section(overview_id, overview_title)
    build_overview_dashboard(overview_id)
    print(f"Overview dashboard: https://smith.langchain.com/o/{org_id}/monitor/dashboards/{overview_id}")

    # Real-time dashboard (1-5 hours)
    realtime_title = "echo v2 realtime"
    realtime_id = get_or_create_section(realtime_title)
    if clean:
        clean_section(realtime_id, realtime_title)
    build_realtime_dashboard(realtime_id)
    print(f"Realtime dashboard: https://smith.langchain.com/o/{org_id}/monitor/dashboards/{realtime_id}")

    # Product quality dashboard (semantic outcomes and detection quality)
    quality_title = "echo v2 quality"
    quality_id = get_or_create_section(quality_title)
    if clean:
        clean_section(quality_id, quality_title)
    build_usage_dashboard(quality_id)
    print(f"Quality dashboard: https://smith.langchain.com/o/{org_id}/monitor/dashboards/{quality_id}")
    print("  (Monitoring tab in the left sidebar -> Dashboards)")


if __name__ == "__main__":
    main()
