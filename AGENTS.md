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
