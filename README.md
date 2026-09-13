# Echo v2

> Echo remembers what needs to happen next in your WhatsApp conversations.

Echo is a WhatsApp memory and follow-up layer that tracks who's waiting for a reply, sends a morning digest of pending conversations, and lets you schedule messages to be sent later from your own WhatsApp number.

**Main goal:** surface every conversation where someone is waiting for you, so nothing falls through the cracks. The morning digest is the primary touchpoint; the waiting-list mini web app lets you act on items without leaving the browser.

## How it works

Echo uses two WhatsApp channels:

- **Echo Bot** (360dialog WhatsApp Business API) — the conversational interface. Users chat with Echo here: onboarding, name setup, digest replies, scheduling commands, feedback.
- **User's WhatsApp** (Green API) — Echo acts on the user's behalf here: reads incoming/outgoing messages, sends scheduled messages from the user's own number.

The user links their WhatsApp to Echo via OTP-based onboarding (no QR code needed). Once linked, Echo ingests messages, runs delayed LLM analysis after a quiet period, and maintains a version-fenced "waiting for me" state per chat.

## Features

### Onboarding (OTP-based)

New users message the Echo bot → Echo creates a Green API instance → sends an 8-digit OTP via the bot → user enters it in WhatsApp → Linked Devices → Echo detects authorization → sends welcome → user sends their name → onboarding complete.

- Idempotent: pending users get OTP re-sent, not a second instance
- Polls `getStateInstance` until `notAuthorized` before requesting OTP (instance creation takes 1-2 min)
- Polls for `authorized` after OTP sent (Green's `stateInstanceChanged` webhook is unreliable for OTP-based linking)
- Background task: webhook returns immediately with "please wait", provisioning runs async

### Message ingestion & analysis

- Inbound and outbound messages persisted immutably
- Per-chat state with `activity_version` (incremented on every meaningful message)
- Deduplication on provider message ID (`INSERT ON CONFLICT DO NOTHING`)
- Delayed LLM analysis after a quiet period (default 5 min)
- Analysis results stored historically + active projection (version-fenced)
- Outbound messages do not automatically resolve `WAITING_FOR_ME` — every meaningful message triggers re-analysis

### Waiting for me

Three states per chat:

- `WAITING_FOR_ME` — another person is waiting for the user
- `WAITING_FOR_THEM` — the user is waiting for someone else (planned)
- `NOT_WAITING_FOR_ME` — no one is waiting

An active result is valid only when `active.target_version == chats.activity_version`. This version-fence prevents stale analysis from overriding newer state.

### Feedback flyloop

Users correct Echo's analysis directly from the digest or waiting-list:

- **"לא מחכה לי"** (not waiting for me) — dismisses an item; records a negative signal
- **"לא מעניין"** (not interested) — dismisses + mutes the chat
- **"טעיתי"** (I was wrong) — undoes a previous dismiss; restores the item
- **Miss report** — user reports that Echo missed a waiting item; recorded for model improvement
- Feedback is stored per-result and used to improve future analysis

### Morning digest

- Daily digest per user/local date
- Version-fenced active waiting items
- Sorted by `waiting_since`, up to 20 items + overflow
- "הצג הכול" button to expand the full list
- Claim-before-query to avoid duplicate sends
- Local timezone delivery window
- On-demand digest available via the "שלח עכשיו" command

### Waiting-list mini web app

A token-authenticated web page linked from the digest:

- One-time token issued per digest, stored as a session cookie (TTL 48h default)
- Shows all active waiting items with snooze/dismiss/resolve actions
- Snooze: push the item back by a duration (e.g. "tomorrow", "3 hours")
- Resolve: mark as handled, removes from the active list
- No login required — the token in the URL is the only credential
- Server-rendered HTML (no JS framework); JSON API for actions

### Scheduled messages

- User sends a contact + time via the bot
- LLM time parser (with regex fallback for common Hebrew expressions)
- Scheduled action stored in DB, executed by a scheduler loop
- Sent from the user's own WhatsApp via Green API
- Idempotent execution: stable key `green:send:{user_id}:{action_id}` prevents double-sends across restarts

### Provider name capture

- `sender_id`, `sender_name`, `chat_name` extracted from Green API webhooks
- Used in digest display with fallback to contact records or phone number

## Reliability infrastructure

Echo has a purpose-built runtime layer (`src/echo_v2/runtime/`) that wraps external side effects with retry, timeout, idempotency, and error classification. It is currently used for scheduled message sends.

### Error taxonomy

All integration clients (Green, 360dialog) classify failures into three categories:

| Error | Meaning | Retried? | Example |
|-------|---------|----------|---------|
| `RetryableError` | Transient, may succeed later | Yes | 429, connection error |
| `PermanentError` | Should not be retried | No | 4xx, missing credentials |
| `IndeterminateError` | Outcome unknown (may or may not have sent) | No | Timeout mid-write, 5xx on a write |

For irreversible writes (`EXTERNAL_WRITE` policy), a timeout or unexpected error is classified as `IndeterminateError` — the side effect may have already happened, so we never retry blindly.

### Idempotency

The `IdempotencyStore` protocol (`runtime/idempotency.py`) provides:

- `reserve(key)` — atomic claim with a fencing token and lease
- `put_success` / `put_failure` / `put_indeterminate` — terminal writes, token-guarded
- `renew_lease` — extend a lease for long operations
- `LostOwnershipError` — raised if a lease expired and was reclaimed; the caller re-attempts from the top

The Postgres implementation (`persistence/postgres_idempotency.py`) uses `INSERT ON CONFLICT DO NOTHING` for claims and `UPDATE ... WHERE owner_token = :token` for terminal writes, so a slow prior owner cannot overwrite a new owner's outcome.

### Execution policies

| Policy | max_attempts | timeout | irreversible | Use case |
|--------|-------------|---------|--------------|----------|
| `NO_RETRY` | 1 | — | No | Local compute |
| `LOCAL_COMPUTE` | 1 | 5s | No | CPU-bound work |
| `EXTERNAL_READ` | 3 | 10s | No | API reads |
| `EXTERNAL_WRITE` | 1 | 10s | Yes | API sends (no retry — idempotency handles resumption) |

### Event sink

Every `execute()` call emits `operation.started`, `operation.succeeded`, `operation.failed`, `operation.retrying`, `operation.indeterminate`, and `operation.idempotent.*` events. In production, `LoggingEventSink` writes these to the app logger. In tests, `InMemoryEventSink` captures them for assertions.

## Webhook security & reliability

### Authentication

- **360dialog (bot) webhook**: bearer token auth via `D360_WEBHOOK_SECRET`. The server **refuses to start** without it — an empty secret leaves the endpoint unauthenticated. Validated with `hmac.compare_digest` (constant-time).
- **Green API webhook**: per-instance token auth. Each WhatsApp connection gets a unique `webhook_token`; the token is stored as a SHA-256 hash and validated per request.

### Persistent inbox (callback loss prevention)

The 360dialog webhook uses a persistent inbox (`bot_webhook_events` table) to track the processing lifecycle per event:

```
claim → processing → processed (terminal success)
                  → failed (re-claimable on next provider retry)
```

- A **failed** event is re-claimed on the next provider retry — no lost callbacks.
- A **processed** event is a permanent duplicate — no double-processing.
- A stale **processing** entry past the lease timeout is reclaimable via `reclaim_stale()`.
- The Green API webhook uses `provider_webhook_events` (Postgres `INSERT ON CONFLICT DO NOTHING`) for status/state dedup; message events are deduped via the messages table.

## Privacy

### LangSmith tracing

When `LANGSMITH_TRACING=true`:

- `LANGSMITH_HIDE_INPUTS=true` and `LANGSMITH_HIDE_OUTPUTS=true` are enforced in code — raw LLM prompts/completions (which may contain message text) are never sent to LangSmith.
- `OBSERVABILITY_HASH_KEY` is required (startup fails without it).
- All user/chat IDs in trace metadata are HMAC-hashed via `correlation_id()` — not reversible, not enumerable.
- `@traceable` sanitizers strip PII from every traced method's inputs/outputs.

### Logging

- No phone numbers, message text, user names, or raw LLM reasons are logged.
- Error logs include operation names, status codes, and internal IDs (UUIDs) only.
- Authorization codes, API tokens, and credential-bearing URLs are never logged.

## Architecture

```
src/echo_v2/
├── app/
│   ├── main.py                        # FastAPI app factory + dependency wiring
│   ├── waiting_list_routes.py         # Waiting-list mini web app (token auth, actions)
│   ├── waiting_list_page.py           # Server-rendered HTML templates
│   └── webhooks/
│       ├── dialog360.py               # Echo bot webhook (auth + inbox + dispatch)
│       ├── green.py                   # User's WhatsApp webhook (auth + dedup + dispatch)
│       ├── dedup.py                   # Webhook dedup protocol (Green API)
│       └── inbox.py                   # Persistent inbox protocol + Postgres impl (360dialog)
├── domain/
│   ├── chat.py                        # Message, ChatState domain models
│   ├── conversation.py               # SchedulingFlowContext
│   ├── digest.py                      # Digest domain model
│   ├── feedback.py                    # Feedback actions, HandlingOutcome
│   ├── scheduling.py                  # ScheduledAction domain model
│   └── waiting_for_me.py             # WaitingForMe analysis result
├── services/
│   ├── onboarding.py                  # OTP-based onboarding orchestrator
│   ├── scheduling_flow.py            # Bot command → scheduled action (multi-step)
│   ├── scheduling.py                 # Executes scheduled sends via runtime executor
│   ├── scheduler.py                  # Polls for due actions, records terminal state
│   ├── chat_ingestion.py             # Message persistence + chat state
│   ├── chat_analysis_worker.py       # Polls for due chats, runs LLM analysis, commits
│   ├── waiting_for_me_analyzer.py    # LLM analysis (WaitingForMe classification)
│   ├── digest_worker.py              # Morning digest sender (scheduled + on-demand)
│   ├── digest_formatter.py           # Formats digest template parameters
│   ├── digest_reply.py              # "הצג הכול" full-list reply handler
│   ├── feedback_handler.py           # Feedback flyloop (dismiss, undo, miss report)
│   ├── feedback_service.py           # Feedback actions, mutes, snooze validation
│   ├── time_parser.py                # LLM + regex time expression parser
│   ├── time_parser_regex.py          # Hebrew regex time parser (fallback/primary)
│   ├── waiting_list_query.py         # Query active items for the web app
│   ├── waiting_list_service.py       # Snooze/dismiss/resolve actions
│   └── waiting_list_token_service.py # Issue/validate web session tokens
├── integrations/
│   ├── green/
│   │   ├── client.py                 # Green API HTTP client (error-classified)
│   │   ├── provisioner.py            # Instance creation + settings + OTP
│   │   ├── events.py                 # Green webhook JSON → canonical events
│   │   ├── messaging.py             # Green send_message adapter
│   │   ├── models.py                 # State mapping, subscription translation
│   │   └── settings.py               # Green API env config
│   └── dialog360/
│       ├── client.py                 # 360dialog HTTP client (error-classified)
│       ├── events.py                 # 360dialog webhook JSON → BotEvent
│       └── settings.py               # 360dialog env config
├── persistence/
│   ├── orm.py                        # SQLAlchemy ORM models (17 tables)
│   ├── compose.py                    # Postgres repository composition
│   ├── db.py                         # Engine + session factory + migration runner
│   ├── settings.py                   # DBSettings from env
│   ├── postgres_chat.py              # Messages, chat state, WfM results/active, analysis commit
│   ├── postgres_feedback.py         # Feedback, actions, mutes
│   ├── postgres_digest.py            # Daily digests
│   ├── postgres_idempotency.py       # Idempotency store (fencing tokens, lease reclaim)
│   ├── postgres_scheduled_actions.py # Scheduled actions
│   ├── postgres_webhook_dedup.py     # Green webhook dedup (INSERT ON CONFLICT)
│   ├── postgres_whatsapp_connections.py # WhatsApp connections (encrypted credentials)
│   ├── unit_of_work.py              # UoW pattern (shared session, atomic commits)
│   ├── credential_cipher.py         # Fernet credential encryption
│   ├── user_repository.py            # User CRUD for onboarding
│   ├── user_resolver.py              # Phone → user_id resolution
│   ├── identity.py                   # Phone normalization (E.164)
│   ├── contacts.py                   # Contact repository
│   ├── conversation_state.py        # In-memory scheduling flow state (MVP)
│   ├── waiting_list_tokens.py       # Waiting-list session tokens (Postgres)
│   └── alembic/versions/            # Migrations 0001-0017
├── ports/
│   ├── whatsapp.py                   # Provider-neutral WhatsApp ports (events, messaging)
│   └── bot.py                         # Provider-neutral bot ports (events, channel)
├── runtime/
│   ├── executor.py                   # execute() — retry, timeout, idempotency, events
│   ├── idempotency.py                # IdempotencyStore protocol + in-memory impl
│   ├── policy.py                     # ExecutionPolicy presets (NO_RETRY, EXTERNAL_*, etc.)
│   ├── errors.py                     # Error taxonomy (Retryable, Permanent, Indeterminate)
│   ├── events.py                     # EventSink protocol + NO_OP_SINK
│   └── context.py                    # RunContext (operation_name + run_id)
└── observability/
    ├── privacy.py                    # HMAC-hashing of IDs, startup key validation
    ├── sanitizers.py                 # LangSmith @traceable input/output sanitizers
    └── sinks.py                      # LoggingEventSink (prod), InMemoryEventSink (tests)
```

## Database

PostgreSQL with 17 Alembic migrations:

| Migration | Description |
|-----------|-------------|
| 0001 | Foundation: users, contacts, whatsapp_connections, provider_webhook_events, idempotency_operations |
| 0002 | Scheduled actions |
| 0003 | Contacts |
| 0004 | Send bot message |
| 0005 | Chat ingestion (messages, chat_state) |
| 0006 | Waiting-for-me analysis results |
| 0007 | Waiting-for-me active projection |
| 0008 | Daily digests |
| 0009 | User first_name |
| 0010 | Sender/chat names |
| 0011 | Onboarding status |
| 0012 | Feedback and mutes |
| 0013 | Feedback hardening |
| 0014 | Waiting-list sessions |
| 0015 | Result summary |
| 0016 | WfM results unique constraint (user_id, chat_id, target_version) |
| 0017 | Bot webhook inbox (persistent processing/processed/failed) |

## Getting started

### Prerequisites

- Python 3.13+
- PostgreSQL 14+
- Docker (for testcontainers-based Postgres tests)
- Green API partner account
- 360dialog WhatsApp Business account
- OpenAI API key

### Install

```bash
git clone <repo>
cd echo-v2
python -m venv .venv
source .venv/bin/activate
pip install -e ".[prod,dev]"
```

### Configure

```bash
cp .env.example .env
# Fill in:
# - DATABASE_URL
# - ECHO_CREDENTIAL_KEY (generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
# - GREEN_API_PARTNER_TOKEN
# - D360_API_KEY
# - D360_WEBHOOK_SECRET (required — server refuses to start without it)
# - OPENAI_API_KEY
# - ECHO_WEBHOOK_BASE_URL (your public URL, e.g. https://your-app.onrender.com)
# - OBSERVABILITY_HASH_KEY (required if LANGSMITH_TRACING=true)
```

### Migrate

```bash
alembic upgrade head
```

### Run

```bash
uvicorn echo_v2.app.main:app --factory
```

### Test

```bash
ruff check src tests scripts
pytest                              # unit + integration (testcontainers Postgres)
pytest --cov                        # with coverage (gate: 95%)
pytest tests/evaluation/ -v        # LLM eval harness (real API calls)
```

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATABASE_URL` | Yes | — | PostgreSQL connection string (`postgresql+psycopg://...`) |
| `ECHO_CREDENTIAL_KEY` | Yes | — | Fernet key for credential encryption at rest |
| `ECHO_DEFAULT_PHONE_REGION` | No | `IL` | Phone normalization region (ISO 3166-1 alpha-2) |
| `GREEN_API_PARTNER_TOKEN` | Yes | — | Green API partner token |
| `GREEN_API_PARTNER_URL` | No | `https://api.green-api.com` | Green API base URL |
| `D360_API_KEY` | Yes | — | 360dialog per-phone-number API key |
| `D360_API_BASE_URL` | No | `https://waba-v2.360dialog.io` | 360dialog API base URL |
| `D360_WEBHOOK_SECRET` | **Yes** | — | 360dialog webhook bearer secret (server refuses to start without it) |
| `OPENAI_API_KEY` | Yes | — | OpenAI API key |
| `LLM_MODEL_NAME` | No | `gpt-4.1` | LLM model for analysis + time parsing |
| `ECHO_WEBHOOK_BASE_URL` | No | `https://i-me.onrender.com` | Public URL for Green API webhook configuration |
| `ECHO_BOT_PHONE` | No | — | Echo bot's WhatsApp number (for web app "back to WhatsApp" link) |
| `SCHEDULER_LEASE_SECONDS` | No | `300` | Scheduler action lease duration |
| `SCHEDULER_POLL_INTERVAL` | No | `5` | Scheduler poll interval (seconds) |
| `CHAT_QUIET_PERIOD_SECONDS` | No | `300` | Quiet period before a chat is eligible for analysis |
| `CHAT_PRIVATE_ONLY` | No | `true` | Only ingest private chats (not groups) |
| `CHAT_ANALYSIS_ENABLED` | No | `false` | Start the analysis worker loop in the app lifespan |
| `CHAT_ANALYSIS_POLL_INTERVAL` | No | `60` | Analysis worker poll interval (seconds) |
| `DIGEST_ENABLED` | No | `false` | Start the digest worker loop |
| `DIGEST_POLL_INTERVAL` | No | `300` | Digest worker poll interval (seconds) |
| `DIGEST_TEMPLATE_NAME` | No | `morning_waiting_digest6` | WhatsApp template name for the morning digest |
| `ECHO_WAITING_LIST_TOKEN_TTL_HOURS` | No | `48` | Waiting-list session token TTL |
| `LANGSMITH_TRACING` | No | `false` | Enable LangSmith LLM tracing |
| `LANGSMITH_HIDE_INPUTS` | No | `true` | Hide raw LLM inputs from LangSmith (enforced in code) |
| `LANGSMITH_HIDE_OUTPUTS` | No | `true` | Hide raw LLM outputs from LangSmith (enforced in code) |
| `LANGSMITH_API_KEY` | No | — | LangSmith API key (read by LangSmith SDK) |
| `LANGSMITH_PROJECT` | No | `echo-v2-local` | LangSmith project name |
| `LANGSMITH_WORKSPACE_ID` | No | — | LangSmith workspace ID |
| `OBSERVABILITY_HASH_KEY` | If tracing | — | HMAC key for hashing IDs in trace metadata (required when `LANGSMITH_TRACING=true`) |

## Webhooks

| Endpoint | Provider | Auth | Purpose |
|----------|----------|------|---------|
| `POST /webhooks/bot/dialog360` | 360dialog | Bearer (`D360_WEBHOOK_SECRET`) | Echo bot messages (onboarding, commands, replies, feedback) |
| `POST /webhook/360dialog` | 360dialog | Bearer (`D360_WEBHOOK_SECRET`) | Alias for the above |
| `POST /webhooks/whatsapp/green` | Green API | Per-instance token | User's WhatsApp (messages, status, state changes) |
| `GET /health` | — | — | Health check |
| `GET /q/{token}` | — | Token in URL | Waiting-list mini web app |

## Security

- Provider credentials encrypted at rest (Fernet, `ECHO_CREDENTIAL_KEY`)
- Webhook tokens stored as SHA-256 hashes, validated with `hmac.compare_digest`
- 360dialog webhook requires a non-empty `D360_WEBHOOK_SECRET` — the server refuses to start without it
- Authorization codes, API tokens, and credential-bearing URLs never logged
- No phone numbers, message text, user names, or raw LLM reasons in logs
- LangSmith tracing hides raw inputs/outputs; IDs are HMAC-hashed

## Deployment

Deployed on Render. The app auto-creates when `DATABASE_URL` is set:

```bash
uvicorn echo_v2.app.main:app
```

Migrations must be applied separately:

```bash
alembic upgrade head
```

Background workers (scheduler, analysis, digest) start in the FastAPI lifespan. The scheduler always starts; analysis and digest workers are gated by `CHAT_ANALYSIS_ENABLED` and `DIGEST_ENABLED`.

## Evaluation

The waiting-for-me classifier is evaluated against 40 labeled cases (including 12 realistic 8-10 message conversations) using the real LLM API. The harness prints Rich tables with accuracy, confusion matrix, and per-case details.

```bash
pytest tests/evaluation/ -v
```

Latest result: 95% accuracy (38/40), 100% for `WAITING_FOR_ME`, 100% for `NOT_WAITING_FOR_ME`.

## Tech stack

- Python 3.13, FastAPI, SQLAlchemy 2 async, PostgreSQL, Alembic
- httpx, websockets (Green API)
- OpenAI-compatible LLM API (gpt-4.1)
- 360dialog WhatsApp Business API
- Green API (user's WhatsApp)
- LangSmith tracing (privacy-hardened)
- Render deployment
- pytest, pytest-asyncio, pytest-cov, testcontainers, Rich
