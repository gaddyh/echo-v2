# Plan: LangSmith tracing + operational metrics

## Correctness prerequisite (separate PR, before any tracing)

### Problem
`ChatAnalysisProcessor.process()` saves the result and updates active state
BEFORE `ChatAnalysisWorker._process_chat()` checks the version. If a new
message arrives during LLM processing, the worker logs "discarding result"
but the stale result and active state are already persisted.

Additionally, there is no `UNIQUE (user_id, chat_id, target_version)`
constraint on `waiting_for_me_results` — a crash can produce multiple
results for the same version.

### Fix: atomic commit repository

The atomic commit cannot live only inside `chat_analysis_worker.py`.
Existing repositories open their own sessions, so three repository calls
do not form one transaction.

Add an explicit component:

```python
@dataclass(frozen=True)
class PreparedAnalysis:
    result: WaitingForMeResult
    conversation_snapshot: dict


@dataclass(frozen=True)
class AnalysisCommitOutcome:
    status: Literal["committed", "stale"]
    result_id: str | None


class AnalysisCommitRepository(Protocol):
    async def commit_if_current(
        self,
        *,
        user_id: str,
        chat_id: str,
        target_version: int,
        analysis: PreparedAnalysis,
    ) -> AnalysisCommitOutcome: ...
```

The Postgres implementation uses one session and transaction:
1. Lock the chat row (`SELECT ... FOR UPDATE`).
2. Compare `activity_version`.
3. Return stale without writes if different.
4. Insert the result.
5. Upsert/delete active state.
6. Mark processed.
7. Commit.

The in-memory implementation mirrors the same logic.

### Worker changes
`ChatAnalysisProcessor.process()` returns `PreparedAnalysis` (analysis
without persistence). `ChatAnalysisWorker._process_chat()` calls
`commit_if_current()` and returns a traceable outcome:

```python
async def _process_chat(...) -> Literal["committed", "stale", "missing"]:
    ...
```

### Migration: UNIQUE constraint with preflight

Before adding `UNIQUE (user_id, chat_id, target_version)`, run a preflight
query inside the migration:

```sql
SELECT user_id, chat_id, target_version, COUNT(*)
FROM waiting_for_me_results
GROUP BY user_id, chat_id, target_version
HAVING COUNT(*) > 1;
```

If duplicates exist, the migration aborts with a clear message. Do NOT
silently delete duplicates — active rows and feedback may reference a
specific `result_id`. Reconcile manually before rerunning.

For the POC: verify production has no duplicates, abort if it does.

### Files touched (real standalone PR)
- `src/echo_v2/services/chat_analysis_worker.py` — split analyze/commit
- `src/echo_v2/domain/` — `PreparedAnalysis`, `AnalysisCommitOutcome`
- `src/echo_v2/persistence/chat_repositories.py` — `AnalysisCommitRepository` protocol + in-memory
- `src/echo_v2/persistence/postgres_chat.py` — Postgres `commit_if_current`
- `src/echo_v2/persistence/orm.py` — add UNIQUE constraint
- `src/echo_v2/persistence/alembic/versions/0016_*` — migration with preflight
- `src/echo_v2/app/main.py` — composition wiring
- Tests: race, crash, duplicate version, stale, missing

---

## Sprint 1: WfM tracing foundation (one PR)

### 1a. Dependency + env
- `pyproject.toml`: add to BOTH `prod` and `dev` extras:
  ```toml
  "langsmith==0.12.4",
  ```
  Exact pin for this first observability implementation. Base
  `dependencies = []` stays empty — this repo's convention.

- `.env.example`:
  ```
  LANGSMITH_TRACING=false
  LANGSMITH_API_KEY=
  LANGSMITH_PROJECT=echo-v2-local
  LANGSMITH_WORKSPACE_ID=
  OBSERVABILITY_HASH_KEY=
  ```
- Default OFF. No fake-looking API key.

- **Startup guard**: when `LANGSMITH_TRACING=true`, startup must fail if
  `OBSERVABILITY_HASH_KEY` is missing. Privacy depends on this key —
  it cannot be optional when tracing is on.

```python
if os.environ.get("LANGSMITH_TRACING", "false").lower() == "true":
    if not os.environ.get("OBSERVABILITY_HASH_KEY"):
        raise RuntimeError(
            "OBSERVABILITY_HASH_KEY is required when LANGSMITH_TRACING=true"
        )
```

### 1b. Privacy contract (document before code)

**Allowed (safe metadata):**
- `user_id_hash` (HMAC-SHA256, not plain SHA-256 — phone numbers are
  enumerable, so a keyed HMAC is required)
- `chat_id_hash` (HMAC-SHA256)
- `connection_id_hash` (HMAC-SHA256)
- `target_version` (int)
- `activity_version` (int)
- `message_count` (int)
- `decision` (enum: waiting_for_me / not_waiting_for_me / uncertain)
- `confidence` (float)
- `prompt_version` (str, e.g. "wfm-v1")
- `model` (str)
- `direction` (enum: inbound / outbound)
- `dedup` (enum: first / duplicate)
- `event_type` (str)
- `is_write` (bool)
- `attempts` (int)
- `result_id` (UUID — internal, linkable identifier, restricted access)
- `active_id` (UUID — internal, linkable identifier)
- `scheduled_action_id` (UUID — internal)
- `action` (enum: done / snooze / dismiss)
- `snooze_preset` (str | None)
- `outcome` (enum: applied / duplicate / stale / not_found / invalid)
- `reminder_outcome` (enum: sent / skipped / failed)
- `kind` (str — scheduled action type, sanitized)
- `idempotency_outcome` (enum: executed / cached)
- `indeterminate` (bool)
- `provider` (str: dialog360 / green)
- `operation` (str)
- `http_status` (int)
- `provider_error_code` (str | None)

**Forbidden (never in traces):**
- Phone numbers, chat IDs (raw), contact names
- WhatsApp message text / conversation content
- Webhook payloads (raw)
- HTTP headers, API keys, bearer tokens, URLs (Green URLs contain credentials)
- HTTP request body, full response body
- OpenAI prompt text / completion text (unless explicitly enabled locally)
- User PII of any kind
- Idempotency keys (contain raw user_id)

**LLM content — conscious decision:**
- Default: `LANGSMITH_HIDE_INPUTS=true`, `LANGSMITH_HIDE_OUTPUTS=true`
- For dev/debug: can set `LANGSMITH_HIDE_INPUTS=false` locally
- Production: always hidden. Metadata only.

### 1c. HMAC correlation helper
- `src/echo_v2/observability/privacy.py`:
  ```python
  import hashlib
  import hmac
  import os

  _HASH_KEY: bytes | None = None

  def _get_key() -> bytes:
      global _HASH_KEY
      if _HASH_KEY is None:
          key = os.environ.get("OBSERVABILITY_HASH_KEY")
          if not key:
              raise RuntimeError(
                  "OBSERVABILITY_HASH_KEY is required when tracing is enabled"
              )
          _HASH_KEY = key.encode()
      return _HASH_KEY

  def correlation_id(value: str) -> str:
      """HMAC-SHA256 of an ID for trace metadata. Not reversible."""
      digest = hmac.new(_get_key(), value.encode(), hashlib.sha256).hexdigest()
      return digest[:24]
  ```
- Plain SHA-256 is NOT acceptable — phone numbers are enumerable.

### 1d. Inject wrapped OpenAI client (refactor both LLM classes)

Both `LLMWaitingForMeAnalyzer` and `LLMTimeParser` currently create a new
`AsyncOpenAI` per call. Refactor both to accept an injected client.

- Define a narrow protocol:
  ```python
  class ChatCompletionClient(Protocol):
      chat: Any
  ```
- `LLMWaitingForMeAnalyzer.__init__`: accept `client: ChatCompletionClient`
  instead of `api_key`. Remove per-call `AsyncOpenAI` creation.
- `LLMTimeParser.__init__`: same change.
- Composition root (`main.py`):
  ```python
  raw_openai = AsyncOpenAI(api_key=openai_api_key)
  traced_openai = wrap_openai(raw_openai)
  analyzer = LLMWaitingForMeAnalyzer(client=traced_openai, model=model)
  time_parser = LLMTimeParser(client=traced_openai, model=model)
  ```
- Close `raw_openai` in FastAPI lifespan shutdown (`finally` block).
- Also close existing Green and 360dialog clients (`aclose()`) in shutdown.

**Refactor both LLM classes to receive the shared client, but add LangSmith
tracing only to `LLMWaitingForMeAnalyzer`. Defer the TimeParser span to a
later sprint.** This avoids retaining two different OpenAI lifecycles.

**Preserve missing-key behavior:**
```python
if chat_analysis_enabled and not openai_api_key:
    raise RuntimeError(
        "OPENAI_API_KEY is required when CHAT_ANALYSIS_ENABLED=true"
    )
```
Don't force an OpenAI key when analysis and LLM fallback are disabled.

### 1e. @traceable with process_inputs/process_outputs

Dynamic fields (decision, confidence, outcome) cannot be static metadata.
Use `process_inputs` and `process_outputs`:

```python
WFM_PROMPT_VERSION = "wfm-v1"  # change when prompt or output contract changes

def safe_analysis_inputs(inputs: dict) -> dict:
    conversation = inputs["conversation"]  # ConversationInput dataclass
    return {
        "user_id_hash": correlation_id(conversation.user_id),
        "chat_id_hash": correlation_id(conversation.chat_id),
        "target_version": conversation.target_version,
        "message_count": len(conversation.messages),
        "prompt_version": WFM_PROMPT_VERSION,
    }

def safe_analysis_output(output: WaitingForMeResult) -> dict:
    return {
        "decision": output.decision.value,
        "confidence": output.confidence,
        "target_version": output.target_version,
    }
```

`ConversationInput` is a dataclass, not a dict — use attributes, not `.get()`.

The analyzer does not yet know `result_id` — that ID only exists after
persistence. Do not include it in `safe_analysis_output`.

**Critical:** `process_inputs` must remove `self` — otherwise repositories,
clients, and settings may be serialized.

```python
@traceable(
    name="wfm.llm_analyze",
    process_inputs=safe_analysis_inputs,
    process_outputs=safe_analysis_output,
)
async def analyze(self, conversation: ConversationInput) -> WaitingForMeResult:
    ...
```

### 1f. Trace only the analyzer + per-chat analysis (Sprint 1 scope)

- `LLMWaitingForMeAnalyzer.analyze()` → `@traceable(name="wfm.llm_analyze")`
- `ChatAnalysisWorker._process_chat()` → `@traceable(name="wfm.analysis")`
  with `process_outputs` showing `status` (committed/stale/missing)
- NOT `run_once()` (the poll loop)
- `wrap_openai` auto-traces the OpenAI call as nested span

Expected trace:
```
wfm.analysis
└── wfm.llm_analyze
    └── openai.chat.completions.create
```

### 1g. Manual LangSmith verification
After Sprint 1, manually verify in LangSmith UI that traces appear with
correct nesting and no PII.

---

## Sprint 2: WfM operational path (one PR)

### 2a. Action methods (actual method names, not renamed)
- `WaitingForMeActionService.handled()` → `@traceable(name="wfm.action.handled")`
- `WaitingForMeActionService.done()` → `@traceable(name="wfm.action.done")`
  (mini-app path, distinct from `handled()` which is the WhatsApp callback path)
- `WaitingForMeActionService.snooze()` → `@traceable(name="wfm.action.snooze")`
- `WaitingForMeActionService.dismiss_not_waiting()` → `@traceable(name="wfm.action.dismiss_not_waiting")`
- `WaitingForMeActionService.dismiss_not_interested()` → `@traceable(name="wfm.action.dismiss_not_interested")`
- `WaitingForMeActionService.dismiss_with_reason()` → `@traceable(name="wfm.action.dismiss_with_reason")`
- `WaitingForMeFeedbackService.record()` → `@traceable(name="wfm.feedback.record")`
- Do NOT rename methods as part of observability PR.

### 2b. Feedback handler
- `FeedbackHandler.handle()` → `@traceable(name="wfm.callback")`
- Links to `wfm.action.*` as nested span

### 2c. Bot send (generic, not WfM-specific)
`SchedulingService._execute_bot_send()` handles every bot message, not
only WfM reminders. Decorating it as `wfm.reminder` would misclassify
other sends.

Use a generic span:
```python
@traceable(name="scheduling.bot_send", process_inputs=safe_bot_send_inputs)
async def _execute_bot_send(self, action: ScheduledAction) -> str:
    ...
```

Safe metadata:
```json
{
    "scheduled_action_id": "action.id",
    "kind": "safe_kind",
    "reminder_outcome": "sent"
}
```

Do NOT extract a separate `_execute_waiting_for_me_reminder()` — the
generic span with `kind` metadata is sufficient.

### 2d. Low-level HTTP boundaries (not every public method)
Instead of decorating every Dialog360/Green operation, decorate the actual
HTTP boundaries:
- `Dialog360Client._post_json()` → `@traceable(name="dialog360.http")`
- `GreenClient._request_json()` → `@traceable(name="green.http")`

Safe input processor discards: `self`, URL, headers, body, API token,
phone/chat ID. Keeps only:
```json
{
    "provider": "green",
    "operation": "sendMessage",
    "is_write": true,
    "connection_id_hash": "..."
}
```

Every attempt becomes exactly one span — retries visible as repeated spans.

### 2e. Webhook entry points
- `dialog360.py` webhook handler → `@traceable(name="dialog360.webhook")`
- `green.py` webhook handler → `@traceable(name="green.webhook")`
- `ChatEventDispatcher.dispatch()` (lives under Green webhook module,
  NOT `chat_ingestion.py`) → `@traceable(name="wfm.ingest")`
- Metadata: `provider`, `event_type`, `dedup`, `chat_id_hash`
- NO raw webhook payload

### 2f. Switch production EventSink
- `main.py`: switch `InMemoryEventSink` → `LoggingEventSink`
- Fix `LoggingEventSink` first:
  - Either call it "operational logging" (not structured logging), OR
    change it to JSON output
  - Drop or HMAC the idempotency key (currently logs raw user_id)
  - Do NOT log full attributes that contain PII

### 2g. Correlation tests
- Action and reminder carry the same `scheduled_action_id`
- Ingestion and analysis carry the same `chat_id_hash`

### Retry visibility (corrected)
`SEND_BOT_MESSAGE` currently bypasses `runtime.execute()` and
`EXTERNAL_WRITE` has `max_attempts=1`. So the trace shows:

```
scheduling.execute
└── green.http attempt 1
```

Repeated HTTP child spans will appear only for operations whose reliability
policy actually allows retries. Runtime logs still explain why a retry did
or did not occur.

---

## EventSink: Option B+ (final decision)

### Decision
- **Reject A** (LangSmithEventSink): `RuntimeEvent` is not a span.
  `operation.started` + `operation.succeeded` are lifecycle events for ONE
  operation. Converting each into a span produces misleading traces.
- **Reject C** (`@traceable` inside `runtime.execute()`): wrong dependency
  direction. Reliability layer stays pure, no vendor coupling.
- **Choose B+**: `@traceable` around the service operation. `@traceable`
  around every external HTTP attempt. `runtime.execute()` unchanged.
  `EventSink` → `LoggingEventSink` (operational logging, not tracing).

### Two complementary views
| Mechanism | Purpose |
|-----------|---------|
| LangSmith | Operation tree, LLM calls, HTTP attempts, latency |
| Operational logs (`LoggingEventSink`) | Retry decisions, idempotency hits, indeterminate outcomes |
| Metrics (later) | Counts, rates, queue lag, alerts |

### Optional runtime improvement (defer if it ripples)
If `runtime.execute()` doesn't return diagnostic info, consider
`ExecutionResult[T]` with `attempt_count` and `idempotency_outcome`. But
don't make this change merely for tracing — traced HTTP spans already
show retries.

---

## Later: Metrics (separate, after tracing works)

Do NOT create an unused `MetricsRecorder` interface yet. Select an actual
backend first (Prometheus/OTLP), then implement the first five metrics:
```
wfm_analysis_total{decision,prompt_version}
wfm_analysis_errors_total{stage}
wfm_queue_due_count
wfm_queue_lag_seconds
wfm_reminder_total{outcome}
```

After metrics, expand tracing to time parsing, onboarding, digest, and
general scheduling.

---

## Testing strategy

### Do NOT test in unit tests:
- Whether LangSmith actually received a trace (fragile, network calls)
- Whether LangSmith UI shows the right nesting

### Cannot do:
- Both disable tracing AND capture span metadata (contradiction)

### DO test:
1. **Sanitizer functions directly** — test `safe_analysis_inputs()`,
   `safe_analysis_output()`, `safe_http_inputs()` etc. as pure functions
2. **No PII in sanitized output** — phone, message text, headers, API keys
   do not appear in the sanitized dict
3. **Decorator is transparent** — function results and exceptions are
   unchanged whether `@traceable` is present or not (test with
   `tracing_context(enabled=False)`)
4. **Fake injected OpenAI clients** — use fake clients in tests, not real
   `wrap_openai` with network
5. **Mock LangSmith client/export boundary** — for one instrumentation test
6. **Prohibit real network access** in normal tests
7. **Correlation IDs** — `target_version`, `active_id`,
   `scheduled_action_id` present in sanitized metadata where expected
8. **One opt-in integration smoke test** — marked, not in CI, manual
9. **Startup guard** — `LANGSMITH_TRACING=true` without
   `OBSERVABILITY_HASH_KEY` raises `RuntimeError` at startup

---

## Files to modify

### Prerequisite PR (correctness — real standalone sprint)
- `src/echo_v2/domain/` — `PreparedAnalysis`, `AnalysisCommitOutcome` (new)
- `src/echo_v2/persistence/chat_repositories.py` — `AnalysisCommitRepository` protocol + in-memory
- `src/echo_v2/persistence/postgres_chat.py` — Postgres `commit_if_current`
- `src/echo_v2/services/chat_analysis_worker.py` — split analyze/commit, return outcome
- `src/echo_v2/persistence/orm.py` — add UNIQUE constraint
- `src/echo_v2/persistence/alembic/versions/0016_*` — migration with preflight
- `src/echo_v2/app/main.py` — composition wiring
- Tests: race, crash, duplicate version, stale, missing

### Sprint 1 (tracing foundation)
- `pyproject.toml` — add `langsmith==0.12.4` to prod + dev
- `.env.example` — LANGSMITH_* + OBSERVABILITY_HASH_KEY
- `src/echo_v2/observability/privacy.py` — HMAC correlation_id (new)
- `src/echo_v2/services/waiting_for_me_analyzer.py` — accept injected client + @traceable
- `src/echo_v2/services/time_parser.py` — accept injected client (no @traceable yet)
- `src/echo_v2/services/chat_analysis_worker.py` — @traceable on _process_chat
- `src/echo_v2/app/main.py` — create + wrap OpenAI client, inject, close on shutdown, startup guard
- `tests/observability/test_privacy.py` — HMAC + PII exclusion tests (new)
- `tests/observability/test_tracing.py` — sanitizer + transparency tests (new)

### Sprint 2 (operational path)
- `src/echo_v2/services/feedback_service.py` — @traceable on action methods + done + record
- `src/echo_v2/services/feedback_handler.py` — @traceable on handle
- `src/echo_v2/services/scheduling.py` — @traceable on _execute_bot_send (generic)
- `src/echo_v2/integrations/dialog360/client.py` — @traceable on _post_json
- `src/echo_v2/integrations/green/client.py` — @traceable on _request_json
- `src/echo_v2/app/webhooks/dialog360.py` — @traceable on webhook handler
- `src/echo_v2/app/webhooks/green.py` — @traceable on webhook handler
- `src/echo_v2/observability/sinks.py` — fix LoggingEventSink (HMAC/drop idempotency key)
- `src/echo_v2/app/main.py` — switch to LoggingEventSink
- Tests for correlation, sanitization, transparency

---

## What we do NOT trace
- Poll loops (`run_once`, scheduler poll, cleanup poll)
- Pure functions (digest_formatter, time_parser_regex, identity)
- ORM definitions, settings loading
- Credential encryption/decryption (security)
- Raw webhook payloads, HTTP headers, API keys, URLs
- Phone numbers, chat IDs (raw), contact names, message text
- `runtime.execute()` internals (reliability, not tracing)

---

## Risk / notes
- Instrumentation remains installed when `LANGSMITH_TRACING=false`, but
  trace submission is disabled (not a no-op — code still runs, just doesn't send)
- `wrap_openai` same — installed but doesn't send when disabled
- langsmith SDK uses contextvars for parent-child propagation — works with asyncio
- No function signature changes for `@traceable` (decorator)
- OpenAI client created once at composition root, injected, closed on shutdown
- Coverage should not drop significantly (tracing is additive)
