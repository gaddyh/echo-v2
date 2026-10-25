# Echo v2 — Agent Notes

## Verification commands

- **Tests**: `.venv/bin/python -m pytest` (asyncio mode is `auto` — no need to mark async tests)
- **Coverage gate**: `.venv/bin/python -m pytest --cov --cov-report=term-missing --cov-fail-under=95`
- **Lint**: `.venv/bin/ruff check src tests`
- **Type check**: `.venv/bin/mypy` (configured in `pyproject.toml` under `[tool.mypy]`, strict mode)

### Test selection guidance

- Do not run the entire suite for every local edit. Prefer the narrowest relevant test file or group during development.
- Guard unit tests: `.venv/bin/python -m pytest 'tests/**/test_guard*.py' -q`
- Guard review/persistence changes: include `tests/services/test_guard_review_service.py`, `tests/persistence/test_postgres_guard.py`, and `tests/persistence/test_postgres_guard_review.py`; the persistence tests start a Postgres Testcontainer and run migrations.
- Real media E2E tests in `tests/services/test_media_e2e.py` call OpenAI, Modal, or external HTTP services when credentials and fixtures are present. Exclude them from routine local runs with: `.venv/bin/python -m pytest --ignore=tests/services/test_media_e2e.py -q`
- Run `.venv/bin/python -m pytest --durations=30` when investigating slow tests.
- Run the full suite before a PR or for broad changes. CI also runs the full suite on Python 3.10 and 3.13.

## Git workflow

- Never push feature work directly to `main`.
- Feature branch names must be unique, meaningful, and briefly describe the work (for example, `feature/add-digest-funnel` or `fix/python310-z-timestamps`).
- Start work from an up-to-date `main` branch on a feature branch:
  `git switch main && git pull --ff-only origin main && git switch -c feature/<short-name>`.
- Push the feature branch and open a pull request targeting `main`.
- Enable GitHub auto-merge with squash: `gh pr merge --auto --squash <PR_NUMBER>`.
- `main` requires the CI checks `Test (Python 3.10)` and `Test (Python 3.13)`; merge happens automatically after both pass.
- The repository is configured for solo development, so no manual PR approval is required.

## Architecture notes

- `WaitingListService` (the former god service) has been removed.
- Read path: `WaitingListQueryService.current_views()` returns canonical `WaitingForMeView` rows. All surfaces (mini-app, digest, WhatsApp bot cards) consume this single read model.
- Name/phone resolution lives once in `ContactNameResolver` (`services/waiting_for_me_view.py`).
- Mutation/session orchestration lives in `WaitingListActionService` (`services/waiting_list_action_service.py`).
- `BotChannel.send_template` accepts an optional `url_suffix` keyword used by the digest worker to embed a waiting-list session token.

## Incident Tracker (Guard)

- `IncidentTracker` provides semantic deduplication for parent notifications using the `IncidentMatcher` (LLM) and the `alerted_incidents` table.
- Feature flag: `GUARD_INCIDENT_TRACKER_ENABLED` (defaults `false`).
- When enabled, the tracker uses the existing `PostgresUnitOfWork` to atomically persist the incident and its scheduled actions in a single transaction. A PostgreSQL advisory lock (`pg_advisory_xact_lock`) is acquired on the UoW session and held until commit, preventing concurrent workers from creating duplicate incidents.
- When the tracker is active, legacy `reconcile_pending` in `ParentAttentionDeliveryService` and `ExposureDeliveryService` is skipped by the worker to avoid duplicate alerts from the old evidence-diff delivery path.

### Cutover policy

- Before enabling `GUARD_INCIDENT_TRACKER_ENABLED`, drain pending legacy delivery records (`parent_attention_outcomes`, exposure outcomes). The tracker does not process these; once enabled, only `IncidentTracker.reconcile_pending` runs (which is a no-op for the pilot).
- Enable the tracker only after the Postgres integration tests pass (`tests/persistence/test_postgres_incident_tracker_concurrency.py`).

### Pilot limitation

- For the pilot, if scheduling fails, the UoW transaction rolls back (no incident, no schedule) and the error is logged. There is no reconciliation or retry queue — the alert may be missed if no new assessment arrives. This is an explicitly accepted limitation.

## Gemini Video Enrichment

- `GeminiVideoSummarizer` (`services/gemini_video_summarizer.py`) provides semantic understanding of WhatsApp videos using Gemini's native video+audio input.
- Feature flag: `GEMINI_VIDEO_ENABLED` (defaults `false`). When disabled, videos fall back to the existing `_video_placeholder` path.
- Default model: `gemini-3.5-flash-lite` (configurable via `GEMINI_VIDEO_MODEL`). Measured ~15s per 16MB video vs ~244s with `gemini-3.8-flash`.
- The whole video file is sent inline (no ffmpeg trimming). A bounded streaming download aborts on overflow with `VideoTooLargeError` (default cap `GEMINI_VIDEO_MAX_BYTES=15MB`), which is marked terminal immediately (no retry).
- Gemini describes; the existing Guard analyzers decide. No risk classification or notification policy is added.
- Videos are always enriched (`_target` returns `MEDIA_SUMMARY` for all videos, including captioned ones). Caption is preserved alongside the Gemini description (`caption | description`).
- Per-kind versioning: `VIDEO_ENRICHMENT_VERSION` (default `2026-10-23.video.1`) is used only for video messages, so existing image/document/audio/link enrichments are not reprocessed on a version bump.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `GEMINI_VIDEO_ENABLED` | `false` | Enable Gemini video enrichment |
| `GEMINI_API_KEY` | — | Gemini API key (required when enabled) |
| `GEMINI_VIDEO_MODEL` | `gemini-3.5-flash-lite` | Gemini model for video analysis |
| `GEMINI_VIDEO_MAX_BYTES` | `15728640` (15MB) | Max accepted video size; oversized → terminal |
| `VIDEO_ENRICHMENT_VERSION` | `2026-10-23.video.1` | Per-kind enrichment version for videos |

## Video Link Enrichment

- Detects direct video file URLs (`.mp4`/`.mov`/`.webm`/`.mkv`) and YouTube URLs (`/watch`, `youtu.be/`, `/shorts/`, `/embed/`) in text messages and routes them through the existing `GeminiVideoSummarizer` instead of the webpage summarizer.
- Feature flag: `VIDEO_LINK_ENRICHMENT_ENABLED` (defaults `false`). When disabled, video links fall through to the existing `LINK_SUMMARY` webpage path (no behavior change).
- Direct video URLs reuse the existing `summarize_media` path (download → Gemini), including the 15 MB cap, caching, and retries. YouTube URLs use a separate `summarize_youtube` method that passes the URL directly to Gemini via the Interactions API (no local download).
- SSRF guard: the existing `_download_bounded` validates every destination **before** connecting — HTTPS-only, rejects private/loopback/link-local/reserved/cloud-metadata IPs, disables auto-redirects and re-validates each redirect hop. The request to an unsafe destination never happens.
- YouTube cost cap: `GEMINI_VIDEO_YOUTUBE_MAX_SECONDS` (default 60) clips analysis to the first N seconds via Gemini's `video_metadata` (`start_offset`/`end_offset`). Static processing (1 FPS frame extraction) is Gemini's default; clipping is only supported in static mode. If the installed `google-genai` SDK rejects `video_metadata`, the YouTube path raises `MediaSummaryError` (terminal) rather than silently processing the whole video. **Validate with a real YouTube URL before enabling.**
- Fallback: transient Gemini failures use the existing enrichment retry mechanism; permanent failures terminate. No webpage-summarizer fallback is added (the existing `LINK_SUMMARY` path still handles non-video links).
- Cache separation: the enrichment cache is keyed by `(message_id, enrichment_version)`, not `kind`. A distinct `VIDEO_LINK_ENRICHMENT_VERSION` ensures previously-summarized YouTube URLs (under the shared `LINK_SUMMARY` version) are reanalyzed as `VIDEO_LINK_SUMMARY` when the feature is enabled. The old rows remain but are no longer consulted.
- Metrics: `url_kind` (`direct_video` / `youtube`) is classified in `_enrich_message` **before** `_produce` is called, so it is available on both success and failure `media.enrichment.completed/failed` pipeline events. `outcome` is derived from `event_name` in pilot SQL.
- Privacy: `pipeline_events` carries only `url_kind`, not the URL itself. The enrichment text includes the URL for the analyzer's context (same as today's `LINK_SUMMARY`).
- Pilot limitation: YouTube support is preview-only and works for public videos. Private/unlisted videos will fail and terminate (no fallback).

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `VIDEO_LINK_ENRICHMENT_ENABLED` | `false` | Enable video link enrichment |
| `VIDEO_LINK_ENRICHMENT_VERSION` | `2026-10-24.video_link.1` | Per-kind enrichment version for video links |
| `GEMINI_VIDEO_YOUTUBE_MAX_SECONDS` | `60` | Cap YouTube analysis to first N seconds |

## AI Usage & Cost Tracking (PR 1)

- `AiUsageRecorder` (`services/reporting/ai_usage_recorder.py`) captures one
  row per billable AI provider attempt in the `ai_usage_events` table.
- `PricingService` (`services/reporting/pricing_service.py`) estimates USD
  cost from measured usage using versioned YAML rates
  (`src/echo_v2/reporting/pricing.yaml`). Each event stores the active
  `price_version` so historical reports don't change when rates are updated.
- Migration: `0059_ai_usage_events`.
- Cost source semantics: `measured_usage_priced` (provider tokens × YAML
  rates), `fallback_estimate` (assumption-based, e.g. Modal audio minutes),
  `provider_reported` (reserved for invoice reconciliation), `unknown`.
- Every actual provider attempt gets a fresh `invocation_id`. Persistence
  retries reuse the same invocation ID (idempotent upsert). Provider retries
  and manual reanalysis get new invocation IDs (separate rows).
- Usage persistence failure does NOT fail the AI analysis; it is logged and
  surfaced via the recorder's error path.

### Modal cost model

Modal bills per second of compute resources (GPU, CPU, memory), NOT per
minute of audio. Container startup and warm-idle overhead can dominate a
low-volume pilot. The `modal.audio_cost.estimated_usd_per_minute` in
`pricing.yaml` is a configurable effective rate calibrated from actual
Modal billing reports, NOT a published Modal rate.

Recalibration procedure (after each billing period):
1. Export the Modal billing report:
   `modal billing report --start YYYY-MM-01 --end YYYY-MM-01 --show-resources --csv`
2. Sum total Modal cost for the period.
3. Query `ai_usage_events` for total `audio_duration_seconds` where
   `provider='modal'` in the same period.
4. New rate = total_cost / (total_audio_seconds / 60).
5. Update `modal.audio_cost.estimated_usd_per_minute` and bump `version`.

The published GPU-second rates (`modal.gpu_second_usd`) are retained for
reference and future GPU-second metering, but are NOT used for current cost
estimation. `processing_seconds` is stored on each event but is NOT priced
separately — that would double-count overhead already baked into the
per-minute rate.
