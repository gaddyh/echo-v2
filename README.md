# Echo v2

> Echo remembers what needs to happen next in your WhatsApp conversations.

Echo is a WhatsApp memory and follow-up layer that tracks who's waiting for a reply, sends a morning digest of pending conversations, and lets you schedule messages to be sent later from your own WhatsApp number.

## How it works

Echo uses two WhatsApp channels:

- **Echo Bot** (360dialog WhatsApp Business API) — the conversational interface. Users chat with Echo here: onboarding, name setup, digest replies, scheduling commands.
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

An active result is valid only when `active.target_version == chats.activity_version`.

### Morning digest

- Daily digest per user/local date
- Version-fenced active waiting items
- Sorted by `waiting_since`, up to 20 items + overflow
- "הצג הכול" button to expand
- Claim-before-query to avoid duplicate sends
- Local timezone delivery window

### Scheduled messages

- User sends a contact + time via the bot
- LLM time parser (with regex fallback)
- Scheduled action stored in DB, executed by a scheduler loop
- Sent from the user's own WhatsApp via Green API

### Provider name capture

- `sender_id`, `sender_name`, `chat_name` extracted from Green API webhooks
- Used in digest display with fallback to contact records or phone number

## Architecture

```
src/echo_v2/
├── app/
│   ├── main.py                    # FastAPI app factory + dependency wiring
│   └── webhooks/
│       ├── dialog360.py            # Echo bot webhook (onboarding, digest, scheduling)
│       ├── green.py                # User's WhatsApp webhook (messages, state changes)
│       └── dedup.py                 # Webhook deduplication
├── domain/
│   ├── chat.py                     # Message, ChatState domain models
│   ├── digest.py                   # Digest domain model
│   └── waiting_for_me.py           # WaitingForMe analysis result
├── services/
│   ├── onboarding.py               # OTP-based onboarding orchestrator
│   ├── scheduling_flow.py          # Bot command → scheduled action
│   ├── chat_ingestion.py           # Message persistence + chat state
│   ├── chat_analysis.py            # LLM analysis worker
│   ├── digest_worker.py            # Morning digest sender
│   └── digest_reply.py             # "הצג הכול" button handler
├── integrations/
│   ├── green/
│   │   ├── client.py               # Green API HTTP client (createInstance, OTP, QR, send)
│   │   ├── provisioner.py           # Provider-neutral provisioning
│   │   ├── events.py                # Green webhook JSON → canonical events
│   │   ├── messaging.py            # Green send_message adapter
│   │   └── models.py                # State mapping, subscription translation
│   └── dialog360/
│       ├── client.py               # 360dialog HTTP client (bot channel)
│       └── events.py                # 360dialog webhook JSON → BotEvent
├── persistence/
│   ├── orm.py                      # SQLAlchemy ORM models
│   ├── compose.py                   # Postgres repository composition
│   ├── postgres_*.py                # Postgres repository implementations
│   ├── user_repository.py           # User CRUD for onboarding
│   ├── user_resolver.py             # Phone → user_id resolution
│   ├── credential_cipher.py         # Fernet credential encryption
│   └── alembic/versions/            # Migrations 0001-0011
├── ports/
│   ├── whatsapp.py                  # Provider-neutral WhatsApp ports
│   └── bot.py                       # Provider-neutral bot ports
└── runtime/
    ├── retries.py                   # Retry policies
    ├── idempotency.py               # Idempotency keys
    └── errors.py                    # Error classification
```

## Database

PostgreSQL with 11 Alembic migrations:

| Migration | Description |
|-----------|-------------|
| 0001 | Foundation: users, contacts, whatsapp_connections |
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

## Getting started

### Prerequisites

- Python 3.13+
- PostgreSQL 14+
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
# - D360_WEBHOOK_SECRET
# - OPENAI_API_KEY
# - ECHO_WEBHOOK_BASE_URL (your public URL, e.g. https://your-app.onrender.com)
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
ruff check .
pytest
pytest --cov
```

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATABASE_URL` | Yes | — | PostgreSQL connection string |
| `ECHO_CREDENTIAL_KEY` | Yes | — | Fernet key for credential encryption |
| `ECHO_DEFAULT_PHONE_REGION` | No | `IL` | Phone normalization region |
| `GREEN_API_PARTNER_TOKEN` | Yes | — | Green API partner token |
| `GREEN_API_PARTNER_URL` | No | `https://api.green-api.com` | Green API base URL |
| `D360_API_KEY` | Yes | — | 360dialog API key |
| `D360_WEBHOOK_SECRET` | Yes | — | 360dialog webhook bearer secret |
| `OPENAI_API_KEY` | Yes | — | OpenAI API key |
| `LLM_MODEL_NAME` | No | `gpt-4.1` | LLM model for analysis + time parsing |
| `ECHO_WEBHOOK_BASE_URL` | No | `https://i-me.onrender.com` | Public URL for Green webhooks |
| `CHAT_ANALYSIS_ENABLED` | No | `false` | Start the analysis worker loop |
| `DIGEST_ENABLED` | No | `false` | Start the digest worker loop |

## Webhooks

| Endpoint | Provider | Purpose |
|----------|----------|---------|
| `POST /webhook/360dialog` | 360dialog | Echo bot messages (onboarding, commands, replies) |
| `POST /webhooks/whatsapp/green` | Green API | User's WhatsApp (messages, status, state changes) |
| `GET /health` | — | Health check |

## Security

- Provider credentials encrypted at rest (Fernet)
- Webhook tokens stored as SHA-256 hashes, validated with `hmac.compare_digest`
- Authorization codes, API tokens, and credential-bearing URLs never logged
- Bearer auth on bot webhook, per-instance token auth on Green webhook

## Deployment

Deployed on Render. The app auto-creates when `DATABASE_URL` is set:

```bash
uvicorn echo_v2.app.main:app
```

Migrations must be applied separately:

```bash
alembic upgrade head
```

## Evaluation

The waiting-for-me classifier is evaluated against 40 labeled cases (including 12 realistic 8-10 message conversations) using the real LLM API. The harness prints Rich tables with accuracy, confusion matrix, and per-case details.

```bash
pytest tests/evaluation/ -v
```

Latest result: 95% accuracy (38/40), 100% for `WAITING_FOR_ME`, 100% for `NOT_WAITING_FOR_ME`.

## Tech stack

- Python 3.13, FastAPI, SQLAlchemy 2 async, PostgreSQL, Alembic
- httpx, websockets (Green API)
- OpenAI-compatible LLM API
- 360dialog WhatsApp Business API
- Green API (user's WhatsApp)
- Render deployment
- pytest, pytest-asyncio, pytest-cov, Rich
