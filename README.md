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
- Each result records `model`, `prompt_version`, and `analyzer_version`, so feedback can be correlated with the exact algorithm that produced it
- Outbound messages do not automatically resolve `WAITING_FOR_ME` — every meaningful message triggers re-analysis
- Supports GPT-5+ reasoning models (default temperature, larger completion budget) alongside gpt-4.x
- **Audio transcription**: inbound voice notes (Green API `audioMessage`) are transcribed via Modal Hebrew Whisper before LLM analysis, so the analyzer sees the spoken content as text. Transcription is lazy — the webhook stores the download URL and returns immediately; the analysis worker downloads and transcribes when it picks up the chat. If transcription is disabled or fails, the audio message is analyzed with `text=None` (no crash, no blocked webhook).

### Waiting for me

Three analysis decisions per chat:

- `WAITING_FOR_ME` — another person is waiting for the user
- `NOT_WAITING_FOR_ME` — no one is waiting
- `UNCERTAIN` — not enough information (media-only, ambiguous)

An active result is valid only when `active.target_version == chats.activity_version`. This version-fence prevents stale analysis from overriding newer state.

All surfaces (morning digest, WhatsApp cards, mini web app) query actionable items through a single shared `WaitingListQueryService`, so they can never disagree on what's waiting.

### Actions vs. feedback semantics

Operational actions and correctness feedback are deliberately separate:

- **בוצע** (done) — operational resolution only; not a correctness judgment
- **נודניק / snooze** — postpone; not feedback
- **לא דורש תגובה** (no response required) — dismisses without feedback
- **לא מחכים לי** (not waiting for me) — dismisses and records a `FALSE_POSITIVE`, the real accuracy signal
- Feedback rows link to the result (and its model/prompt/analyzer version) with the conversation snapshot the model actually saw

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

- One-time token issued per digest, exchanged for a Secure/HttpOnly session cookie (TTL 48h default)
- Card-per-item UI with RTL swipe navigation between items
- Actions: **בוצע** (done), snooze (1h / tomorrow / custom), **לא דורש תגובה**, **לא מחכים לי** (false-positive feedback), schedule a message
- **הודעות +** context viewer: read-only overlay with the last 8 messages of the chat (on-demand, ownership-checked)
- Contact conveniences: star (sorts first), color label, free-text tags with filtering
- Schedule a WhatsApp message to the contact (sent from the user's own number, preset or custom time)
- Every action carries a client-generated `action_id` — double clicks, retries, and timeouts are idempotent
- Server returns authoritative state after every action; the client never guesses
- No login required — the token in the URL is the only credential; raw tokens are never stored (SHA-256 hash only)

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

- Service-level traces (`wfm.analysis`, `wfm.llm_analyze`, `wfm.judge`, `wfm.ingest.green`, etc.) use a separate `tracing_client` with `LANGSMITH_HIDE_INPUTS=false` — the `@traceable` sanitizers hash IDs and strip PII at the application level.
- LLM call traces (via `wrap_openai`) use the default client. During development, `LANGSMITH_HIDE_INPUTS=false` and `LANGSMITH_HIDE_OUTPUTS=false` show full prompts/completions. Re-enable hiding for production privacy.
- `OBSERVABILITY_HASH_KEY` is required (startup fails without it).
- All user/chat IDs in trace metadata are HMAC-hashed via `correlation_id()` — not reversible, not enumerable.

### LangSmith dashboards

Two dashboards created via `scripts/setup_langsmith_dashboard.py`:

- **echo v2 overview** — days/weeks view: analysis runs, decisions, latency, error rates
- **echo v2 realtime** — 1-5 hour view: recent runs, judge scores, ingestion events

### Alert checker

A background task (`observability/alert_checker.py`) polls the database every 5 minutes and sends WhatsApp alert summaries to `ECHO_OWNER_PHONE` when it detects:

- **Bot send failures** — `scheduled_actions` with `status='failed'` in the last 60 min
- **Bot send indeterminate** — `scheduled_actions` with `status='indeterminate'` in the last 60 min
- **Analyzer stuck** — chats where `next_analysis_at` is overdue by >10 min AND `activity_version > last_processed_version`
- **High error rate** — failed/(succeeded+failed) > 20% in `scheduled_actions` over 60 min

Rate-limited to max 1 alert summary per hour.

### LLM-as-judge

After each analysis run, an LLM judge (`services/analysis_judge.py`) evaluates whether the decision was correct given the conversation. The judge runs as a `wfm.judge` child trace under `wfm.analysis` and stores its score as LangSmith feedback (`judge_correctness` key):

- **1.0** = decision is correct
- **0.5** = debatable / ambiguous
- **0.0** = decision is wrong

The judge uses a different model (`JUDGE_MODEL_NAME`, default `gpt-5.4`) from the analyzer (`gpt-5.6-luna`) to reduce same-model bias. Judge scores are visible in the LangSmith dashboard as feedback on each `wfm.analysis` run.

### Judge disagreement flywheel

When the judge scores 0.0 (disagreement) or 0.5 (ambiguous), the `wfm.analysis` run is automatically added to a LangSmith annotation queue for human review. The annotator labels the correct decision, creating a flywheel:

```
Judge disagrees → annotation queue → human labels → new eval cases → better judge/analyzer
```

Setup:
```bash
.venv/bin/python scripts/setup_annotation_queue.py   # creates queue, prints ID
# Add to .env: JUDGE_ANNOTATION_QUEUE_ID=<id from script output>
```

### User false-positive flywheel

When a user taps **לא מחכים לי** (WhatsApp) or marks a card as **detected_incorrectly** (web mini-app), they're reporting a false positive — Echo was wrong, nobody is actually waiting. This is a stronger, explicit human signal than the judge's. The conversation + analyzer decision are sent to a dedicated LangSmith annotation queue for human review, feeding the same flywheel:

```
User reports false positive → annotation queue → human labels → gold examples → better analyzer
```

The DB `waiting_for_me_feedback` row (FALSE_POSITIVE) is the durable source of truth; the annotation queue is a best-effort, fire-and-forget projection — the user's dismiss action is never blocked by LangSmith. The trace (`wfm.user_false_positive`) is self-contained: it carries the conversation snapshot, analyzer decision, next_owner, model, prompt_version, and analyzer_version as run inputs, so reviewers can filter by prompt/model when investigating false-positive patterns.

Setup:
```bash
.venv/bin/python scripts/setup_user_annotation_queue.py   # creates queue, prints ID
# Add to .env: USER_ANNOTATION_QUEUE_ID=<id from script output>
```

### Logging

- No phone numbers, message text, user names, or raw LLM reasons are logged.
- Error logs include operation names, status codes, and internal IDs (UUIDs) only.
- Authorization codes, API tokens, and credential-bearing URLs are never logged.

## Architecture

For the full system design — subsystem boundaries, data flows, design decisions, and error handling per subsystem — see [ARCHITECTURE.md](ARCHITECTURE.md). The tree below is a quick module map.

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
│   ├── analysis_judge.py             # LLM-as-judge: evaluates analysis correctness
│   ├── transcription.py              # Transcriber protocol + audio download/convert/transcribe pipeline
│   ├── transcription_factory.py      # Builds Transcriber from env (Modal or None)
│   ├── digest_worker.py              # Morning digest sender (scheduled + on-demand)
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
│   │   ├── events.py                 # Green webhook JSON → canonical events (incl. audio metadata)
│   │   ├── messaging.py             # Green send_message adapter
│   │   ├── models.py                 # State mapping, subscription translation
│   │   └── settings.py               # Green API env config
│   ├── modal/
│   │   ├── client.py                 # Modal Hebrew Whisper HTTP transport (retry, backoff)
│   │   ├── transcriber.py            # ModalWhisperTranscriber (implements Transcriber protocol)
│   │   └── settings.py               # Modal transcription env config
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
│   └── alembic/versions/            # Migrations 0001-0024
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
    ├── tracing.py                    # Separate LangSmith client for sanitized traces
    ├── alert_checker.py              # Background alert checker (WhatsApp alerts)
    ├── alerts.py                     # Alert formatting + 360dialog bot send
    └── sinks.py                      # LoggingEventSink (prod), InMemoryEventSink (tests)
```

## Database

PostgreSQL with 24 Alembic migrations:

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
| 0018 | Contact starred |
| 0019 | Contact color labels + tags |
| 0020 | Result model / prompt_version / analyzer_version |
| 0024 | Message audio metadata (audio_download_url, audio_mime_type, audio_file_name) |
| 0029 | Rename audio_* columns to media_* (covers image/video/document) |

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
pytest -m eval_soc -v -s           # LLM eval harness (real API calls)
pytest -m eval -k judge -v -s      # Judge eval harness (real API calls)
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
| `LLM_MODEL_NAME` | No | `gpt-4.1` | LLM model for analysis + time parsing (production runs `gpt-5.6-luna`; GPT-5+/o* reasoning models are auto-detected) |
| `ECHO_WEBHOOK_BASE_URL` | No | `https://i-me.onrender.com` | Public URL for Green API webhook configuration |
| `ECHO_BOT_PHONE` | No | — | Echo bot's WhatsApp number (for web app "back to WhatsApp" link) |
| `SCHEDULER_LEASE_SECONDS` | No | `300` | Scheduler action lease duration |
| `SCHEDULER_POLL_INTERVAL` | No | `5` | Scheduler poll interval (seconds) |
| `CHAT_QUIET_PERIOD_SECONDS` | No | `300` | Quiet period before a chat is eligible for analysis |
| `CHAT_PRIVATE_ONLY` | No | `true` | Only ingest private chats (not groups) |
| `CHAT_ANALYSIS_ENABLED` | No | `false` | Start the analysis worker loop in the app lifespan |
| `CHAT_ANALYSIS_POLL_INTERVAL` | No | `60` | Analysis worker poll interval (seconds) |
| `MODAL_TRANSCRIPTION_URL` | No | — | Modal Hebrew Whisper endpoint URL (enables audio transcription when set) |
| `MODAL_TRANSCRIPTION_KEY` | No | — | Modal proxy key (required if `MODAL_TRANSCRIPTION_URL` is set) |
| `MODAL_TRANSCRIPTION_SECRET` | No | — | Modal proxy secret (required if `MODAL_TRANSCRIPTION_URL` is set) |
| `MODAL_TRANSCRIPTION_TIMEOUT_SECONDS` | No | `180` | Modal transcription request timeout |
| `DIGEST_ENABLED` | No | `false` | Start the digest worker loop |
| `DIGEST_POLL_INTERVAL` | No | `300` | Digest worker poll interval (seconds) |
| `DIGEST_TEMPLATE_NAME` | No | `morning_waiting_digest6` | WhatsApp template name for the morning digest |
| `ECHO_WAITING_LIST_TOKEN_TTL_HOURS` | No | `48` | Waiting-list session token TTL |
| `LANGSMITH_TRACING` | No | `false` | Enable LangSmith LLM tracing |
| `LANGSMITH_HIDE_INPUTS` | No | `false` | Hide raw LLM inputs from LangSmith (set `true` for production privacy) |
| `LANGSMITH_HIDE_OUTPUTS` | No | `false` | Hide raw LLM outputs from LangSmith (set `true` for production privacy) |
| `LANGSMITH_API_KEY` | No | — | LangSmith API key (read by LangSmith SDK) |
| `LANGSMITH_PROJECT` | No | `echo-v2-local` | LangSmith project name |
| `LANGSMITH_PROJECT_ID` | No | — | LangSmith project UUID |
| `LANGSMITH_WORKSPACE_ID` | No | — | LangSmith workspace ID |
| `OBSERVABILITY_HASH_KEY` | If tracing | — | HMAC key for hashing IDs in trace metadata (required when `LANGSMITH_TRACING=true`) |
| `JUDGE_MODEL_NAME` | No | `gpt-5.4` | LLM model for the judge (different from analyzer to reduce bias) |
| `JUDGE_ANNOTATION_QUEUE_ID` | No | — | LangSmith annotation queue ID for judge disagreements (score 0.0/0.5). Create with `scripts/setup_annotation_queue.py` |
| `USER_ANNOTATION_QUEUE_ID` | No | — | LangSmith annotation queue ID for user-reported false positives ("לא מחכים לי" / detected_incorrectly). Create with `scripts/setup_user_annotation_queue.py` |
| `ECHO_OWNER_PHONE` | No | — | Phone number for WhatsApp alert summaries |
| `ALERT_CHECKER_ENABLED` | No | `false` | Start the alert checker background task |
| `ALERT_CHECKER_POLL_INTERVAL` | No | `300` | Alert checker poll interval (seconds) |

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

The waiting-for-me classifier is evaluated against real LLM API calls using three labeled suites. The harness prints rich tables (accuracy, confusion matrix, per-case detail) and persists every run to `tests/evaluation/results/` (JSON + Markdown) with model and prompt version.

| Suite | Cases | What it tests |
|-------|-------|---------------|
| Sanity (`eval` marker) | 40 | Baseline forms: direct questions, requests, closings |
| SOC families (`eval_soc`) | 80 | 19 obligation-state phenomena as contrast pairs: acknowledgement ≠ fulfillment, conditional activation, cancellation/supersession, ball handoffs, implicit completion. Train/dev/test splits with leakage validation |
| SOC-2508 realistic (`eval_soc`) | 40 | Grounded in the [SOC-2508 dataset](https://huggingface.co/datasets/marcodsn/SOC-2508): long noisy windows, buried obligations, base-rate banter negatives, UNCERTAIN labels, time decay via `<delay/>`, wrong-chat retractions |

### Judge evaluation

The LLM-as-judge is evaluated against golden labels (not analyzer output) to measure alignment with our ground truth:

| Suite | Cases | What it tests |
|-------|-------|---------------|
| Judge sanity (`eval` marker, `-k judge`) | 40 | Does the judge agree with our golden labels on baseline cases? |
| Judge SOC (`eval` marker, `-k judge`) | 9 | Does the judge agree on harder state-transition cases? |
| Judge SOC-2508 (`eval` marker, `-k judge`) | 40 | Does the judge agree on realistic long/noisy cases (dev + test)? |

Baseline (gpt-5.4 judge): sanity 39/40 (97.5%), SOC 9/9 (100%), SOC-2508 40/40 (100%).

```bash
pytest -m eval_soc -v -s                 # SOC families + SOC-2508 realistic
pytest -m eval_soc -v -s -k soc2508      # realistic suite only
pytest -m eval -v -s                     # sanity suite
pytest -m eval -k judge -v -s            # judge eval (sanity + SOC)
```

Latest results (gpt-5.6-luna, prompt v1): SOC dev 39/40, SOC test 39/40, SOC-2508 dev 21/21, SOC-2508 test 18/19. Remaining failures are all in the conservative direction (predicting `WAITING_FOR_ME` on ambiguous negatives).

## Tech stack

- Python 3.13, FastAPI, SQLAlchemy 2 async, PostgreSQL, Alembic
- httpx, websockets (Green API)
- OpenAI-compatible LLM API (gpt-5.6-luna in production; gpt-4.x supported)
- Modal Hebrew Whisper (audio transcription via `ivrit-ai/whisper-large-v3-turbo-ct2`)
- 360dialog WhatsApp Business API
- Green API (user's WhatsApp)
- LangSmith tracing (privacy-hardened)
- Render deployment
- pytest, pytest-asyncio, pytest-cov, testcontainers, Rich
