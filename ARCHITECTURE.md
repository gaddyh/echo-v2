# Echo v2 Architecture

This document describes the internal architecture of Echo v2: the subsystems, their boundaries, the data flows between them, and the design decisions that shape them. It is intended for contributors who need to understand how the pieces fit together before changing them.

For onboarding, configuration, environment variables, and operations, see [README.md](README.md).

## Table of contents

- [System overview](#system-overview)
- [Layering and ports](#layering-and-ports)
- [Onboarding](#onboarding)
- [Message ingestion](#message-ingestion)
- [Chat analysis and the waiting-for-me pipeline](#chat-analysis-and-the-waiting-for-me-pipeline)
- [Audio transcription](#audio-transcription)
- [Morning digest](#morning-digest)
- [Scheduled messages](#scheduled-messages)
- [Waiting-list mini web app](#waiting-list-mini-web-app)
- [Feedback loop](#feedback-loop)
- [Runtime layer](#runtime-layer)
- [Persistence](#persistence)
- [Webhook security and reliability](#webhook-security-and-reliability)
- [Observability and privacy](#observability-and-privacy)
- [Deployment and worker lifecycle](#deployment-and-worker-lifecycle)

## System overview

Echo is a WhatsApp memory and follow-up layer. It uses two WhatsApp channels:

- **Echo Bot** (360dialog WhatsApp Business API) — the conversational interface. Users chat with Echo here: onboarding, name setup, digest replies, scheduling commands, feedback.
- **User's WhatsApp** (Green API) — Echo acts on the user's behalf here: reads incoming/outgoing messages, sends scheduled messages from the user's own number.

The user links their WhatsApp to Echo via OTP-based onboarding. Once linked, Echo ingests messages, runs delayed LLM analysis after a quiet period, and maintains a version-fenced "waiting for me" state per chat. A morning digest surfaces pending conversations; a token-authenticated mini web app lets the user act on them.

### High-level data flow

```
                ┌──────────────────┐        ┌──────────────────┐
   360dialog ──▶│  Echo Bot webhook│──────▶ │   Onboarding     │
   (bot)        │  /webhooks/bot/  │        │   Scheduling flow│
                │   dialog360      │        │   Feedback handler│
                └──────────────────┘        └──────────────────┘
                        │
                        ▼
                ┌──────────────────┐        ┌──────────────────┐
   Green API  ──▶│  Green webhook   │──────▶ │  Chat ingestion  │
   (user WA)    │  /webhooks/       │        │  (messages +     │
                │   whatsapp/green  │        │   chat state)    │
                └──────────────────┘        └──────────────────┘
                                                   │
                           ┌───────────────────────┘
                           ▼
                ┌──────────────────┐        ┌──────────────────┐
                │ Chat analysis     │──audio▶│ Modal Whisper    │
                │ worker            │        │ (lazy transcript)│
                │ (LLM classifier)  │        └──────────────────┘
                └──────────────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        ┌─────────┐  ┌──────────┐  ┌──────────────┐
        │ Digest  │  │ Feedback │  │ Waiting-list │
        │ worker  │  │ handler  │  │ mini web app│
        └─────────┘  └──────────┘  └──────────────┘
              │            │            │
              ▼            ▼            ▼
        ┌─────────────────────────────────────┐
        │ Scheduled messages (scheduler loop) │
        │ → Green API send (idempotent)        │
        └─────────────────────────────────────┘
```

## Layering and ports

The codebase follows a ports-and-adapters (hexagonal) layering:

```
app/         FastAPI routes, webhook dispatchers, lifespan wiring
services/    Application orchestration (use cases); depends only on ports
domain/      Pure dataclasses and enums; no dependencies
ports/       Provider-neutral protocols (WhatsApp, bot, transcriber)
integrations/ Adapter implementations (Green, 360dialog, Modal)
persistence/ Repository protocols + Postgres implementations + ORM
runtime/     Cross-cutting execution wrapper (retry, timeout, idempotency)
observability/ Tracing, sanitizers, event sinks
```

Key rules:

- **Provider JSON never escapes the adapter.** `GreenEventAdapter.parse()` and `Dialog360EventAdapter` convert provider payloads into neutral `ProviderEvent` / `BotEvent` objects with no `raw` field. Everything downstream depends only on the neutral types in `ports/`.
- **Services depend on protocols, not implementations.** `ChatIngestionService` takes a `MessageRepository` and a `ChatStateRepository`; it never imports `PostgresMessageRepository`.
- **ORM objects never escape persistence.** Repositories translate between domain dataclasses and `MessageRow` / `ChatRow` at the boundary.
- **Composition happens in `app/main.py` and `persistence/compose.py`.** These are the only places that know which concrete adapter wires to which port.

## Onboarding

Onboarding links a user's WhatsApp to Echo via an 8-digit OTP, without a QR code.

### Flow

1. **First contact.** A 360dialog bot webhook arrives. `dialog360.py` resolves the sender phone; if unknown or already onboarding, it routes to `OnboardingService.handle_unknown_event`.
2. **Consent-first intro.** Echo sends an intro with two buttons (`onboarding:start`, `onboarding:info`). No user row or Green instance is created until the user consents.
3. **Start onboarding.** `start_onboarding` normalizes the phone, creates a `pending` user row, sends "please wait", and spawns a background `asyncio.create_task(_provision_and_send_otp)`.
4. **Provision Green instance.** The background task generates a 32-byte `webhook_token`, calls `GreenProvisioner.create_connection`, and stores the returned `ConnectionRef` + `ProviderCredentials` with `webhook_token_hash = sha256(token)` and status `PROVISIONING`.
5. **Wait for instance ready.** Polls `GreenClient.get_state_instance` every 5s for up to 60 attempts until the state is `notAuthorized` or `authorized` (instance creation takes 1–2 min).
6. **Get OTP.** Calls `get_authorization_code(id_instance, api_token, phone_int)` and sends the 8-digit code to the user via the bot with Hebrew instructions.
7. **Poll for authorization.** Polls `get_state_instance` every 10s for 30 attempts (5 min). On `authorized`, updates the connection status to `CONNECTED` and calls `handle_connection_established`.
8. **Connection webhook race.** A `stateInstanceChanged` webhook may also fire; `ChatEventDispatcher` updates the connection status and, on `CONNECTED`, calls `handle_connection_established_by_id`. Both paths are idempotent — they check the current onboarding status before transitioning.
9. **Welcome + name.** `handle_connection_established` sets `onboarding_status='connected'` and sends the welcome message asking for a name. `handle_name_response` stores the first name and sets `onboarding_status='active'`.
10. **Disconnect.** On a `CONNECTED → PAIRING_REQUIRED` transition, `ChatEventDispatcher` calls `handle_disconnect_notification` to send the disconnect message.

### Design decisions

- **Consent-first.** No DB row or Green instance is created until the user explicitly consents.
- **Background provisioning.** Instance creation runs in an `asyncio` background task so the webhook returns immediately with "please wait".
- **OTP preferred over QR.** Green's `getAuthorizationCode` (8-digit, ~2.5 min) is used rather than QR scanning.
- **Polling fallback.** Because `stateInstanceChanged` is unreliable for OTP-based linking, a 5-minute poll is the reliable completion path.
- **Webhook token hashed only.** The plaintext token is generated by Echo, sent to Green, and stored as `sha256(token)` for `hmac.compare_digest` verification.
- **Credentials encrypted at rest.** `ProviderCredentials.data` is encrypted with `CredentialCipher` (Fernet) before persistence.

### Error handling

- Idempotent: pending users get OTP re-sent, not a second instance; connected/active users no-op.
- Instance creation failure → status `failed` + user-facing error message.
- OTP failure → status `failed` + user-facing error message.
- Poll timeout → `_OTP_TIMED_OUT` message; user can re-send by texting "קוד".
- Resend flow checks `get_state_instance` first; if already `authorized`, tells the user and updates stale DB status.

## Message ingestion

Ingestion persists messages immutably and maintains per-chat state that doubles as the analysis queue.

### Flow

1. **Webhook ingress.** Green sends `POST /webhooks/whatsapp/green` with `instanceData.idInstance` and an `Authorization` header (`Bearer` or `Basic`) containing the `webhookUrlToken`.
2. **Auth + connection resolution.** The route extracts `idInstance`, looks up `connection_repo.get_by_provider_id("green", instance_id)`, and validates the token by comparing `sha256(candidate)` with the stored hash using `hmac.compare_digest`.
3. **Parse.** `GreenEventAdapter.parse(payload)` maps Green `typeWebhook` values to neutral events:
   - `incomingMessageReceived` → `ProviderMessageEvent(INBOUND)`
   - `outgoingMessageReceived` → `ProviderMessageEvent(OUTBOUND, source=USER)`
   - `outgoingAPIMessageReceived` → `ProviderMessageEvent(OUTBOUND, source=API)`
   - `outgoingMessageStatus` → `ProviderMessageStatusEvent`
   - `stateInstanceChanged` → `ProviderConnectionStateChanged`
   - Unknown types are ignored.
4. **Deduplication (split strategy).**
   - `ProviderMessageEvent` is **not** deduped by the webhook dedup store; dedup happens via the `messages` table `INSERT ON CONFLICT (connection_id, provider_message_id) DO NOTHING`.
   - Status and state events are deduped via `WebhookDedupStore.claim` on `event.event_id`, because the same `idMessage` produces many distinct status notifications.
5. **Dispatch.** `ChatEventDispatcher.dispatch` routes `ProviderMessageEvent` to `ChatIngestionService.ingest_message`.
6. **Ingestion.** `ChatIngestionService.ingest_message`:
   - Skips group chats if `private_only=True` (default) unless `chat_id.endswith("@c.us")`.
   - Computes `next_analysis_at = now + quiet_period` (default 300s) for both inbound and outbound.
   - Builds a `Message` domain object, including `audio_download_url`, `audio_mime_type`, `audio_file_name` for audio messages.
   - Calls `message_repo.save(message)`; if `False`, it was a duplicate and the operation no-ops.
   - Calls `chat_state_repo.upsert_on_message(...)` to increment `activity_version`, set `last_message_at`, `last_direction`, `next_analysis_at`, and `chat_name`.
7. **State events.** `ProviderConnectionStateChanged` updates `connection_repo.update_status`; on `CONNECTED` it completes onboarding; on `PAIRING_REQUIRED` after `was_connected`, it sends a disconnect notice.

### Design decisions

- **Static webhook URL.** The endpoint is `/webhooks/whatsapp/green` for all instances; `idInstance` inside the payload resolves the connection, because `idInstance` is not known until after `createInstance` returns.
- **Split dedup strategy.** Message events dedup via the `messages` table (natural idempotency); status/state events use `provider_webhook_events` because the same `idMessage` produces many distinct status notifications.
- **`event_id` is notification-scoped, not message-scoped.** For messages it includes `typeWebhook:provider:connection_id:msg_id`; for status it includes the status; for state it includes the raw state and timestamp, so repeated state transitions are allowed but retries suppressed.
- **Chat state doubles as analysis queue.** The `chats` table is keyed `(user_id, chat_id)` with `activity_version`, `next_analysis_at`, `last_processed_version`. There is no separate job queue.
- **Both inbound and outbound schedule analysis.** Direction alone does not resolve waiting state; the LLM decides later. `next_analysis_at` is set on every message.

## Chat analysis and the waiting-for-me pipeline

The analysis worker polls for due chats, runs the LLM classifier, and commits results atomically with a version fence.

### Worker lifecycle

`ChatAnalysisWorker.run_once` runs every `CHAT_ANALYSIS_POLL_INTERVAL` seconds (default 60):

1. `due_chats = await chat_state_repo.list_due(now, limit=20)` — chats where `next_analysis_at <= now()` and `activity_version > last_processed_version`.
2. For each due chat, `_process_chat` calls the `AnalysisProcessor` **outside any transaction**.
3. The processor returns a `PreparedAnalysis` (LLM result + conversation snapshot).
4. The worker calls `AnalysisCommitRepository.commit_if_current` inside a transaction with `SELECT ... FOR UPDATE` on the chat row.
5. The commit only persists if `chat.activity_version == target_version`. If a new message arrived during processing, the commit returns `"stale"` and nothing is written.

### Processor flow (`ChatAnalysisProcessor`)

1. `MessageRepository.list_for_analysis` loads the message window around the last outbound message (or last 20 if none).
2. If a `Transcriber` is configured, audio messages with `audio_download_url` and `text is None` are transcribed and updated via `MessageRepository.update_text`. See [Audio transcription](#audio-transcription).
3. A `ConversationInput` is built from `(direction, text, timestamp)` tuples.
4. `LLMWaitingForMeAnalyzer.analyze` classifies the conversation:
   - Returns `UNCERTAIN` if the conversation is empty.
   - Builds a transcript with `_DIR_LABELS = {"inbound": "them", "outbound": "me"}` and a system prompt.
   - Calls the injected OpenAI-compatible `ChatCompletionClient`.
   - Parses JSON, clamps confidence to `[0.0, 1.0]`, truncates Hebrew `summary` to 160 chars, and attaches `model`, `prompt_version`, `analyzer_version`.
5. Returns `PreparedAnalysis(result=WaitingForMeResult, conversation_snapshot=dict)`. The snapshot is the exact message list the model saw, for training/feedback.

### Commit outcomes

- `committed` — result, active state, and `mark_processed` all persisted in one unit.
- `stale` — a new message arrived during processing; nothing is written.
- `missing` — the chat was deleted during processing; nothing is written.

### Design decisions

- **No lock during LLM work.** The LLM runs outside the transaction; only the commit is locked. This prevents holding a DB row while waiting on OpenAI.
- **Two-phase result.** `PreparedAnalysis` separates the LLM result from the `conversation_snapshot`. The commit repo merges them at persistence, keeping the LLM output provider-agnostic.
- **Active-state preservation.** When re-analyzing and still `WAITING_FOR_ME`, `waiting_since` and `notified_at` are preserved; `acknowledged_at` is reset to `None` because it is a new version.
- **Single worker, no `FOR UPDATE SKIP LOCKED`.** The worker is designed for one process (`CHAT_ANALYSIS_ENABLED=false` in production). The atomic commit handles the race if a message arrives while analyzing.
- **Crash recovery.** No lease/claim token. If the worker crashes, `next_analysis_at` is still in the past and `activity_version > last_processed_version`, so the chat is reprocessed. This gives at-least-once analysis and at-most-once commit via the version check.
- **Prompt version vs. analyzer version.** `prompt_version` tracks prompt changes; `analyzer_version` tracks the full pipeline (preprocessing, window, rules, parsing) for feedback correlation.

### Error handling

- Processor errors are caught per chat; the worker logs and continues to the next due chat.
- LLM failures raise `AnalysisError`, which bubbles up; the worker continues.
- Audio transcription failures are logged and the message is left with `text=None`; the `ConversationInput` uses `text=""` for that message.

## Audio transcription

Audio transcription is lazy: the webhook stores the download URL and returns immediately; the analysis worker downloads and transcribes when it picks up the chat.

### Why lazy

- Green webhooks must acknowledge quickly; Modal cold starts can take ~60s.
- Audio metadata is durable in the `messages` table, so transcription can happen later without losing the URL.
- If transcription is disabled or fails, the audio message is analyzed with `text=""` — no crash, no blocked webhook.

### Flow

1. **Ingestion.** A Green `audioMessage` webhook is parsed by `GreenEventAdapter._extract_audio_metadata`, populating `audio_download_url`, `audio_mime_type`, `audio_file_name` on `ProviderMessageEvent`.
2. **Persistence.** `ChatIngestionService` stores the `Message` with `text=None` and the audio fields set.
3. **Trigger.** The `ChatAnalysisWorker` polls `ChatStateRepository.list_due` for due chats.
4. **Load conversation.** `ChatAnalysisProcessor.process` calls `MessageRepository.list_for_analysis` to load the message window.
5. **Count audio.** The processor counts total audio messages and those with a download URL.
6. **Lazy transcription.** If a `Transcriber` is configured and there is at least one audio message with `audio_download_url` and `text is None`, `_transcribe_audio_messages` is called.
7. **Per-message transcription.** For each qualifying audio message:
   - `handle_direct_audio_download_url` downloads the file to a temp dir using `httpx`.
   - `ensure_transcribable_audio` runs `ffmpeg -ar 16000 -ac 1` to convert unsupported formats to 16kHz mono WAV if needed. Common WhatsApp voice note formats (`.ogg`, `.opus`, `.mp3`, `.m4a`, `.wav`, `.webm`) pass through without ffmpeg.
   - `transcriber.transcribe_bytes(...)` is called.
   - `ModalWhisperTranscriber` delegates to `ModalTranscriptionClient`, which POSTs to the Modal endpoint with `Modal-Key`, `Modal-Secret`, `Content-Type`, `X-Filename`, `beam_size`, and `vad_filter` params.
8. **Persist transcript.** On success, `MessageRepository.update_text(msg.id, transcript)` is called, then the message dataclass is replaced with `text=transcript`.
9. **Failure fallback.** If transcription fails, the exception is logged and the message remains `text=None`; the `ConversationInput` uses `text=""` for that message.
10. **Analysis.** The `ConversationInput` is built from `(direction, text, timestamp)` tuples and passed to `LLMWaitingForMeAnalyzer.analyze`.

### Design decisions

- **Provider-neutral `Transcriber` port.** The analyzer and worker depend only on the `Transcriber` protocol; Modal is one implementation, and `transcription_factory.py` is the single composition point.
- **Audio metadata stored on `Message`.** The `messages` table has nullable `audio_download_url`, `audio_mime_type`, `audio_file_name` (migration `0024`).
- **Download URL consumed at analysis time.** Green's `fileMessageData.downloadUrl` is captured at ingestion and reused later; no separate audio storage.
- **Format normalization with ffmpeg.** Unsupported extensions are converted to 16kHz mono WAV before sending to Modal. ffmpeg is a system binary, not a Python dependency.
- **Transcript persisted back to `text`.** After transcription, the same `text` column used for textual messages holds the transcript, keeping the analyzer interface uniform.
- **Transcription failures are non-fatal.** A bad voice message does not block the entire waiting-for-me pipeline.

### Error handling

- **Transcription disabled.** If Modal env vars are missing, `build_transcriber()` returns `None`; the processor logs `transcriber=None` and audio messages are analyzed with `text=""`.
- **Download/convert errors.** `handle_direct_audio_download_url` raises on `httpx` errors or ffmpeg failure; the caller catches, logs, and leaves `text=None`.
- **Modal transport errors.** `ModalTranscriptionClient` raises `ModalTranscriptionTransportError`; `ModalWhisperTranscriber` re-raises as `TranscriptionError`.
- **Retry on 502/503/504.** `ModalTranscriptionClient` retries once with exponential backoff + jitter for `httpx.ConnectError` and 5xx status codes.
- **Invalid response.** If Modal JSON lacks a string `text` field, `TranscriptionError` is raised.

## Morning digest

The digest worker sends a daily per-user summary of actionable waiting chats via a WhatsApp template.

### Flow

1. **Poll.** `DigestWorker.run_once` gets `now_utc`, calls `user_provider()` to list `(user_id, phone, tz_name, first_name)`.
2. **Per-user window check.** Convert `now_utc` to the user's local timezone (`ZoneInfo(tz_name or DEFAULT_TZ)`, default `Asia/Jerusalem`). Check `08:00 <= hour < 11:00`.
3. **Claim-before-query.** `digest = await digest_repo.claim_or_get(user_id, local_date)`. If a row already exists, return `None` and skip (already processed today). If not, a `PROCESSING` row is inserted.
4. **Query actionable active chats.** `DigestWorker._get_current_active` lists all `WaitingForMeActive` for the user and keeps only those where `target_version == chats.activity_version`, not acknowledged, not snoozed, and not muted.
5. **Empty digest.** If no active states, update the row to `EMPTY` and stop.
6. **Build items.** For each active chat, resolve contact name (priority: `chats.chat_name` → `contacts.display_name` → message `chat_name`/`sender_name` → phone from `chat_id`) and get the latest inbound message text.
7. **Format template params.** `DigestFormatter.format(items, first_name=name)` returns `first_name` and `count` as strings. The template body itself is a WhatsApp business template and contains no conversation content.
8. **Issue optional waiting-list token.** If a `token_service` is configured, it issues a web session token used as a `url_suffix` for the template button.
9. **Send.** `bot.send_template(phone, template_name, "he", [params.first_name, params.count], url_suffix=url_suffix)`.
10. **On-demand digest.** `send_digest_for_user` skips the time window and the daily claim; it queries active items and sends immediately.

### Design decisions

- **Claim-before-query.** A `PROCESSING` row is created before querying chats. This prevents sending duplicate digests if the worker restarts mid-send.
- **Local date, not UTC.** The digest window and the uniqueness key are in the user's local timezone, so a user in a different zone does not get two digests per UTC day.
- **Template is a navigation gateway.** It only shows the count. No message content is exposed in the template itself; the full list is sent only after the user taps "צפה בשיחות" (inside the 24-hour customer-service window).
- **Filter rules in `_get_current_active`.** Acknowledged, snoozed, and muted items are excluded. This keeps the digest actionable.

### Error handling

- **Send errors are classified.**
  - `IndeterminateError` → status `INDETERMINATE`, no blind retry.
  - `PermanentError` → status `FAILED`.
  - Any other exception → status `FAILED`.
- **Token issuance failure** is logged and the digest is sent without the URL button.
- **User loop errors** are caught per user, so one bad user does not kill the worker.
- **Digest reply handler** returns `False` for unrecognized text, allowing the webhook to pass the event to the next handler (`SchedulingFlowService`).
- **No waiting chats on reply** sends a Hebrew "no chats waiting" message.

## Scheduled messages

Users schedule messages to be sent later from their own WhatsApp number.

### Flow (bot command → scheduled action)

1. **Incoming bot event** arrives at `SchedulingFlowService.handle(event)`. It resolves the sender phone to `(user_id, timezone)` via `UserResolver`.
2. **Cancel check** at any state. Hebrew/English keywords: `cancel`, `בטל`, `ביטול`, `stop`.
3. **Step 1 — contact** (`BotEventType.CONTACT`). `_handle_contact` converts the phone to Green API `chat_id` format (`{number}@c.us`), optionally saves to `ContactRepository`, and stores the context as `AWAITING_MESSAGE`.
4. **Step 1b — saved contact by name or self-reminder "לי".** In `IDLE` state, text `לי` creates a self-reminder, or a matching contact name is treated as if a vCard was sent.
5. **Step 2 — message text** (`AWAITING_MESSAGE`). Stores the message text and moves to `AWAITING_TIME`.
6. **Step 3 — time expression** (`AWAITING_TIME`). `_handle_time` calls `TimeParser.parse(text, user_timezone)`, converts to UTC, creates a `ScheduledAction`, and confirms with local time. `is_reminder` uses `SEND_BOT_MESSAGE`, otherwise `SEND_WHATSAPP_MESSAGE`.

### Flow (scheduled action → execution)

7. **Create.** `SchedulingService.create` builds a `ScheduledAction` with `status=PENDING` and saves it. `create_once` uses `uuid5(NAMESPACE_URL, f"echo:send:{user_id}:{request_id}")` for idempotent creation.
8. **Poll.** `Scheduler.run_once` calls `action_repo.claim_due(now, lease_seconds)`, which atomically transitions the oldest due `PENDING` action to `IN_PROGRESS` and sets `claimed_at`.
9. **Recover stale.** On startup `Scheduler.recover` resets `IN_PROGRESS` actions whose `claimed_at` is older than `lease_seconds` back to `PENDING`.
10. **Execute.** `SchedulingService.execute(action)` dispatches by type:
    - `SEND_WHATSAPP_MESSAGE` → `_execute_green_send`
    - `SEND_BOT_MESSAGE` → `_execute_bot_send`
11. **Idempotent send.** For Green sends:
    - Resolve the WhatsApp connection at execution time (so re-pairing is OK).
    - Call `runtime.execute(operation=_send_operation, input_=send_input, policy=EXTERNAL_WRITE, idempotency_key=f"green:send:{user_id}:{action.id}", idempotency_store=...)`.
    - On success, `mark_succeeded` with `provider_message_id`.

### Design decisions

- **Connection resolved at execution time, not scheduling time.** The user's Green API connection is fetched when the action runs, so re-pairing/re-provisioning between scheduling and execution is handled.
- **Idempotency is the logical identity.** Key is `green:send:{user_id}:{action_id}` (or `bot:send:...`). Same action, same side effect, even across retries.
- **Scheduled actions are not in-memory timers.** The `Scheduler` is a persistent poll loop; Postgres `FOR UPDATE SKIP LOCKED` is intended for safe multi-worker claiming.
- **Action status state machine.** `PENDING → claim → IN_PROGRESS → SUCCEEDED/FAILED/INDETERMINATE` or `CANCELLED`. `INDETERMINATE` is terminal and requires human reconciliation; the idempotency store will not blindly retry it.
- **Flow is a pre-handler for "self" reminders.** The keyword `לי` (to me) makes the flow create a `SEND_BOT_MESSAGE` reminder instead of a WhatsApp send.
- **Contacts saved during scheduling.** The first vCard step is also a contact-import path; `ContactRepository.save` is called but failures are only logged.

### Error handling

- **Flow errors.** Invalid time → ask again. Missing contact in `IDLE` → ask for contact. Cancel → reset state to `IDLE` and confirm.
- **Execution-time error classification in `Scheduler._execute_action`:**
  - `IndeterminateError` → action already marked `INDETERMINATE`; log and continue (no blind retry because the idempotency store will replay the indeterminate outcome).
  - `PermanentError` → action already marked `FAILED`.
  - `RetryableError` → marked `FAILED` so it does not stay `IN_PROGRESS` forever.
  - `ApplicationError` → marked `FAILED`.
  - Any other exception → marked `INDETERMINATE`.
- **Service-level execution.** Missing WhatsApp connection or missing payload fields → `mark_failed` + raise `PermanentError`. `IndeterminateError` and `PermanentError` from `runtime.execute` are re-raised after `mark_indeterminate` / `mark_failed`.
- **Idempotency replay.** If the same `idempotency_key` was already used, `runtime.execute` with `IdempotencyStore` either returns the cached `provider_message_id` or re-raises the cached `IndeterminateError`. This prevents double-sending a scheduled message after restart or retry.

## Waiting-list mini web app

A token-authenticated web page linked from the digest that lets the user act on waiting items without leaving the browser.

### Flow

1. **Token issued.** `WaitingListTokenService.issue(user_id)` generates `secrets.token_urlsafe(32)` and stores only its SHA-256 hash, plus `session_id` and expiry.
2. **User receives link.** `/q/{raw_token}` (e.g. from a digest message sent by `FeedbackHandler._handle_digest_request`).
3. **`GET /q/{token}`.** `token_service.resolve(token)` validates the hash, checks not revoked / not expired. `mark_opened` is called once. A `Secure; HttpOnly; SameSite=Lax` cookie named `wls` is set for `/api/waiting` with the `session_id`.
4. **`GET /api/waiting`.** Validates the `wls` cookie via `token_service.resolve_session(...)`. Applies per-session rate limiting (60 actions/minute, in-memory). Calls `waiting_list_service.list_items(session_id, user_id)`, which calls `WaitingListQueryService.current_actionable(user_id, now=now)` and builds `WaitingListItem` objects with name resolution, preview, and situation summary.
5. **`POST /api/waiting/items/{active_id}/actions`.** Validates `wls` cookie and rate limit. Calls `waiting_list_service.execute_action(...)` with `action_id` (client UUID for idempotency), `expected_version`, `action`, and snooze/dismiss fields. Forms `provider_message_id = f"web:{action_id}"` and dispatches to `WaitingForMeActionService` (`done`, `snooze`, `dismiss_with_reason`).
6. **`POST /api/waiting/items/{active_id}/send`.** Derives the `chat_id` server-side from the active item. Idempotency via the client `request_id`: `scheduling_service.get_by_request_id(request_id, user_id)` first. If not a duplicate, `scheduling_service.create_once(...)` creates a `SEND_WHATSAPP_MESSAGE` scheduled action, and the active item is resolved via `action_service.done(...)` only on first creation.

### Design decisions

- **Raw token used exactly once.** Only the SHA-256 hash is persisted; the raw token is used only to set the cookie.
- **Restrictive cookie.** `Secure; HttpOnly; SameSite=Lax; Path=/api/waiting` with `Cache-Control: no-store` and a restrictive CSP.
- **Shared query service.** The `/api/waiting` endpoint and the WhatsApp feedback cards share `WaitingListQueryService` so they always agree on what is actionable.
- **Idempotent actions.** Client-generated `action_id` becomes part of `provider_message_id = f"web:{action_id}"`. Send scheduling uses the client `request_id`.
- **Contact-level metadata.** Star, color label, and tags are stored on the **contact** (by phone), not on the active item, so they survive resolution.

### Error handling

- Token / session failures return `401`.
- Rate limit returns `429`.
- `stale` action outcomes return HTTP `409` so the client can refresh the card. The server returns the current `WaitingListItem` so the UI can update itself.
- `not_found` returns `404`; `invalid_snooze` / invalid send returns `422`.

## Feedback loop

The feedback handler is a pre-handler in the bot webhook pipeline that sends waiting cards and dispatches user button callbacks.

### Flow

1. `FeedbackHandler.handle(event)` runs before the scheduling flow and returns `True` if it consumed the event.
2. Events it handles:
   - Text containing "סיכום חדש" + `token_service` available → issues a waiting-list token and sends a link.
   - Text "צפה בשיחות" → sends up to 5 individual button cards for the current actionable items.
   - Button reply `action:{active_id}:{action_type}` → `_handle_action`.
   - Button reply `dismiss:{active_id}:{reason}` → `_handle_dismiss`.
3. `_handle_view_details` uses the shared `WaitingListQueryService` to get items, then for each sends a card with three buttons: "טופל" (handled), "להזכיר לי" (snooze), "לא צריד" (dismiss).
4. `_handle_action` dispatches to `WaitingForMeActionService`:
   - `handled` → resolve active + record `CORRECT` feedback.
   - `snooze` → set `snoozed_until` and schedule a WhatsApp reminder.
   - `dismiss` → send the submenu.
5. `_handle_dismiss` dispatches:
   - `not_waiting` → resolve + record `FALSE_POSITIVE` feedback.
   - `not_interested` → resolve only, no feedback.
6. `WaitingForMeActionService` records the action (idempotent by `UNIQUE(user_id, provider_message_id)`), then mutates the active state atomically:
   - `handled` calls `active_repo.delete(...)` after recording `CORRECT`.
   - `snooze` uses `active_repo.apply_if_version(...)` with `{"snoozed_until": snoozed_until}`.
   - `dismiss_not_waiting` uses `active_repo.delete(...)` and records `FALSE_POSITIVE`.
   - `dismiss_not_interested` uses `active_repo.delete(...)` with no feedback.
   - `done` (web) uses `active_repo.delete_if_version(...)` and does **not** record feedback because completing does not prove the model was correct.
   - `dismiss_with_reason` uses `delete_if_version` and records `FALSE_POSITIVE` only when `reason == "detected_incorrectly"`.
7. `WaitingForMeFeedbackService.record()` records an explicit verdict linked to a `result_id` and a 90-day conversation snapshot retention.

### Design decisions

- **Clean separation of action vs. feedback.** Actions mutate state; feedback records a correctness signal about a specific `result_id`. Feedback does not touch active state.
- **Feedback is mostly implicit in the 3-button UX.** "טופל" records `CORRECT`, "לא מחכים לי" records `FALSE_POSITIVE`, and the other options record no verdict.
- **Atomic conditional writes.** Actions use `apply_if_version` / `delete_if_version` to avoid race windows between validation and mutation.
- **First feedback for a given `result_id` wins.** Later attempts are `DUPLICATE`.
- **Mutes are per-user, per-chat.** Can be temporary or permanent (`ChatMuteRow` check constraint).
- **Snooze reminder uses the existing scheduler.** A custom `send_validator` (`make_snooze_reminder_validator`) suppresses the reminder if the active item was resolved, version changed, or re-snoozed.

### Error handling

- Every action returns `HandlingOutcome` instead of a bare bool.
- `DUPLICATE` is returned when the same `provider_message_id` is seen again (e.g. WhatsApp retrying a button callback). The handler silently skips sending a second reply.
- `STALE` is returned when the active row's `target_version` no longer matches the chat's current version; the handler sends the stale-message text.
- `NOT_FOUND` is returned when the `active_id` does not exist.
- Snooze reminder scheduling is best-effort: failures are logged and do not fail the snooze action.

## Runtime layer

A purpose-built execution wrapper (`src/echo_v2/runtime/`) that wraps external side effects with retry, timeout, idempotency, and error classification. Currently used for scheduled message sends.

### `execute()` flow

1. A caller builds a `RunContext` (e.g. `RunContext(operation_name="green.send")`) and calls `execute(...)`.
2. `execute()` validates that `idempotency_key` and `idempotency_store` are either both provided or both omitted.
3. **No idempotency**: `_run_with_retries()` is called directly.
4. **With idempotency**:
   - `store.get(key)` fast-path returns a cached terminal outcome.
   - `store.reserve(key)` returns `ACQUIRED`, `IN_PROGRESS`, or `COMPLETED`.
   - If `COMPLETED`, the stored outcome is replayed.
   - If `IN_PROGRESS`, the caller waits via `wait_for_completion()` and then replays.
   - If `ACQUIRED`, an `owner_token` is issued and `_run_with_retries()` runs the real operation.
5. `_run_with_retries()`:
   - Emits `operation.started`.
   - Runs `operation(input)` with `asyncio.wait_for(..., timeout=policy.timeout_seconds)`.
   - On success, emits `operation.succeeded` and returns `ExecutionResult`.
   - On `asyncio.TimeoutError`/`TimeoutError`, retries if not `irreversible_write` and attempts remain; for `irreversible_write` it immediately emits `operation.indeterminate` and raises `IndeterminateError`.
   - On `RetryableError`, retries with `policy.retry_delay_seconds`.
   - On `ApplicationError` or `ExecutionError` (other than `IndeterminateError`), emits `operation.failed` and raises.
   - For an `irreversible_write`, an unexpected uncaught exception is wrapped as `IndeterminateError` and the indeterminate event is emitted; otherwise it is wrapped as `PermanentError`.
6. The owner block in `execute()` then persists the terminal outcome:
   - `store.put_success(...)` on clean return.
   - `store.put_indeterminate(...)` on `IndeterminateError`, `TimeoutError` for irreversible writes, or `asyncio.CancelledError` for irreversible writes.
   - `store.put_failure(...)` on `PermanentError`.
   - `store.release(...)` on `RetryableError` or `ExecutionError` base classes, and on cancellation for reversible operations.
7. `asyncio.shield()` is used for every store-mutating cleanup so a second cancellation cannot interrupt the state transition.
8. `_handle_outcome()` replays cached outcomes: success returns `ExecutionResult`; failure re-raises `PermanentError`; indeterminate re-raises `IndeterminateError`.

### Error taxonomy

| Error | Meaning | Retried? | Example |
|-------|---------|----------|---------|
| `RetryableError` | Transient, may succeed later | Yes | 429, connection error |
| `PermanentError` | Should not be retried | No | 4xx, missing credentials |
| `IndeterminateError` | Outcome unknown (may or may not have sent) | No | Timeout mid-write, 5xx on a write |

For irreversible writes (`EXTERNAL_WRITE` policy), a timeout or unexpected error is classified as `IndeterminateError` — the side effect may have already happened, so we never retry blindly.

### Execution policies

| Policy | max_attempts | timeout | irreversible | Use case |
|--------|-------------|---------|--------------|----------|
| `NO_RETRY` | 1 | — | No | Local compute |
| `LOCAL_COMPUTE` | 1 | 5s | No | CPU-bound work |
| `EXTERNAL_READ` | 3 | 10s | No | API reads |
| `EXTERNAL_WRITE` | 1 | 10s | Yes | API sends (no retry — idempotency handles resumption) |

### Design decisions

- **`irreversible_write` flag changes the meaning of failure.** Any timeout or unexpected exception during an irreversible write is treated as indeterminate rather than "definitely failed," because the side effect may already have happened.
- **Idempotency outcomes are plain data, never exception objects.** They can be serialized and replayed across process boundaries.
- **`owner_token` is a fencing token.** A persistent store rejects writes that affect zero rows, raising `LostOwnershipError`, which is propagated as `RetryableError` so the caller restarts from `reserve()`.
- **`InMemoryIdempotencyStore` exists only for tests and dev.** The code explicitly warns that it is not production-safe.

## Persistence

PostgreSQL with SQLAlchemy 2 async. Alembic migrations are the source of truth for production; the ORM models are mapping/test convenience.

### Composition

At startup, `build_postgres_repos(DBSettings)` (`compose.py`):
1. Creates an async engine and `async_sessionmaker`.
2. Picks `LocalKeyCredentialCipher` if `settings.credential_key` is set, otherwise `IdentityCredentialCipher` for tests.
3. Builds each repository, injecting the session factory and cipher.
4. Creates a `_BoundUoW(PostgresUnitOfWork)` subclass bound to the same `session_factory` and cipher.

### Session modes

Repositories open a `_SessionContext` per call:
- If a shared `session` was injected (UoW mode), the repository does **not** commit/close.
- If it owns the session (standalone mode), it commits on clean exit and rolls back on exception.

`PostgresUnitOfWork` exposes `connections`, `webhooks`, `idempotency`, `messages`, `chat_state`, `wfm_results`, `wfm_active`, `wfm_feedback`, `wfm_actions`, `chat_mutes`. One `AsyncSession`/transaction is shared across all repositories in the UoW.

### Key atomic operations

- `PostgresMessageRepository.save()` uses `INSERT ... ON CONFLICT (connection_id, provider_message_id) DO NOTHING` for dedup and returns `True` iff a row was inserted.
- `PostgresChatStateRepository.upsert_on_message()` atomically increments `activity_version` and sets `next_analysis_at`.
- `PostgresAnalysisCommitRepository.commit_if_current()` runs the whole analysis persistence in one transaction with `SELECT ... FOR UPDATE` on the chat row, aborting if `activity_version != target_version`.
- Credential encryption happens at the repository boundary: `ProviderCredentials.data` is encrypted before persistence and decrypted on read.

### Design decisions

- **ORM objects never escape the persistence layer.** Repositories translate between domain dataclasses and rows.
- **No lazy-loading relationships.** Foreign keys are declared but not eagerly joined.
- **State columns use `TEXT` + `CHECK` constraints instead of native `ENUM`.** Adding values is a migration, not an `ALTER TYPE`.
- **`updated_at` is set explicitly by repository `UPDATE`s, not by triggers.**
- **Credential encryption is built into the schema from day one.** The `credentials` column is `BYTEA` and the cipher is chosen at composition time.
- **Two modes.** Repositories support standalone (own their session) and UoW mode (share a session, never commit themselves).

### Error handling

- Every repository `_SessionContext` rolls back on exception and closes the session if it owns it.
- `PostgresUnitOfWork.__aexit__` commits on clean exit and rolls back on any exception, then closes the session.
- `LocalKeyCredentialCipher` will raise on a wrong key / corrupt ciphertext; there is no key-rotation logic in the current code.

## Webhook security and reliability

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

## Observability and privacy

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

### Event sink

Every `execute()` call emits `operation.started`, `operation.succeeded`, `operation.failed`, `operation.retrying`, `operation.indeterminate`, and `operation.idempotent.*` events. In production, `LoggingEventSink` writes these to the app logger. In tests, `InMemoryEventSink` captures them for assertions.

## Deployment and worker lifecycle

Deployed on Render. The app starts via:

```bash
uvicorn echo_v2.app.main:app
```

Migrations must be applied separately:

```bash
alembic upgrade head
```

Background workers start in the FastAPI lifespan:
- **Scheduler** — always starts; polls for due scheduled actions.
- **Analysis worker** — gated by `CHAT_ANALYSIS_ENABLED` (default `false` in production).
- **Digest worker** — gated by `DIGEST_ENABLED` (default `false`).

The scheduler uses `FOR UPDATE SKIP LOCKED` semantics for safe multi-worker claiming, but the analysis worker is designed for a single process. The atomic version-fenced commit handles the race if a message arrives while analyzing.
