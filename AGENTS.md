# Echo v2 — Agent Notes

## Verification commands

- **Tests**: `.venv/bin/python -m pytest` (asyncio mode is `auto` — no need to mark async tests)
- **Lint**: `.venv/bin/ruff check src tests`
- **Type check**: `.venv/bin/mypy` (configured in `pyproject.toml` under `[tool.mypy]`, strict mode)

## Architecture notes

- `WaitingListService` (the former god service) has been removed.
- Read path: `WaitingListQueryService.current_views()` returns canonical `WaitingForMeView` rows. All surfaces (mini-app, digest, WhatsApp bot cards) consume this single read model.
- Name/phone resolution lives once in `ContactNameResolver` (`services/waiting_for_me_view.py`).
- Mutation/session orchestration lives in `WaitingListActionService` (`services/waiting_list_action_service.py`).
- `BotChannel.send_template` accepts an optional `url_suffix` keyword used by the digest worker to embed a waiting-list session token.
