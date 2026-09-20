# Echo v2 — Agent Notes

## Verification commands

- **Tests**: `.venv/bin/python -m pytest` (asyncio mode is `auto` — no need to mark async tests)
- **Coverage gate**: `.venv/bin/python -m pytest --cov --cov-report=term-missing --cov-fail-under=95`
- **Lint**: `.venv/bin/ruff check src tests`
- **Type check**: `.venv/bin/mypy` (configured in `pyproject.toml` under `[tool.mypy]`, strict mode)

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
