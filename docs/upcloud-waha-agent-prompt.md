# WAHA Lab — Agent Management-Protocol Implementation Prompt

Copy this prompt into the UpCloud WAHA lab repository (it is self-contained;
no Echo-repo context required). Echo-side support for this protocol is
complete and deployed behind the endpoints below — do not change them.

## Objective

Extend the existing per-VM Python agent (currently: batch message delivery +
worker registration) so it also executes WAHA **session management** on Echo's
behalf. All VM→Echo communication remains agent-initiated outbound HTTPS.
WAHA must stay private on `127.0.0.1:3000` — no public management endpoints,
no worker domains, no TLS on workers, no WAHA API credentials in Echo.

## Echo API contracts (final)

Base: `https://<echo-host>`. Fleet auth for the existing endpoints is
unchanged (`Authorization: Bearer $WAHA_AGENT_TOKEN`). The two new
management endpoints use a **per-worker management token** instead.

### Registration (existing endpoint, extended — backward compatible)

`POST /internal/waha/register` (fleet token) — same body as today, plus one
optional field:

```json
{
  "worker_id": "waha-01-8e3dfc4bab32",
  "region": "uk-lon1",
  "endpoint": "http://127.0.0.1:3000",
  "max_sessions": 4,
  "active_sessions": 1,
  "version": "0.4",
  "health": "ok",
  "last_heartbeat": 1795718136.42,
  "pending": 0,
  "management_protocol": 1
}
```

- `management_protocol` is an int `>= 0`, default `0`. Declare `1` once your
  agent supports everything in this document. Workers with `0` (or the field
  absent) never receive onboarding assignments.
- On the **first** registration with `management_protocol >= 1`, the response
  contains the worker's management token, issued exactly once:

```json
{"ok": true, "worker_token": "wt_<worker_id>_<secret>"}
```

- Persist the token on the VM (file, mode 0600, e.g. next to your other
  state). It is never re-issued; losing it requires deleting the worker row
  in Echo or ops intervention. Later heartbeats return `{"ok": true}` only.
- Never log the token.

### Command polling (new)

`POST /internal/waha/commands` — `Authorization: Bearer <worker_token>`,
body `{"worker_id": "<id>"}` (optional; must match the token when present).

Response:

```json
{"commands": [{"command_id": "<uuid>", "action": "create_and_start", "session": "echo-<uuid>"}]}
```

- Poll approximately every **2 seconds**, in a loop **independent** of the
  message-batch and heartbeat loops (a slow session creation or QR fetch
  must never block message delivery).
- Claiming is lease-based: an unacknowledged command comes back to you after
  the lease expires (~120 s), and Echo caps retries (~5 attempts). Commands
  are not removed by retrieval — you must report results (below).
- `worker_id` in the body is never trusted for authorization — it comes from
  your token.

### Event reporting (new)

`POST /internal/waha/events` — `Authorization: Bearer <worker_token>`. One
event per request. `event_id` is a UUID you generate; retry the **same**
event (same `event_id`) on non-2xx with bounded backoff. Echo processes
events idempotently; 200 means the required state is persisted / the side
effect completed; 503 means retry later; 403 means you addressed a session or
command that is not yours (do not retry); 400 means malformed.

**Command result** (always send after executing a command):

```json
{
  "event_id": "<uuid>", "worker_id": "<id>", "session": "echo-<uuid>",
  "type": "command_result", "command_id": "<uuid>",
  "success": true, "error": null, "timestamp": 1795718136.42
}
```

- For `get_qr`, a successful result includes the QR image:
  `"qr": {"mimetype": "image/png", "data": "<base64>"}` (plus
  `"status": "SCAN_QR_CODE"`).
- For `get_status`, include the observed raw status: `"status": "WORKING"`.
- `error` is a short string (`<= 500` chars) when `success` is false.
- `timestamp` (epoch seconds, float) must reflect when the observation
  happened; it orders events, so make sure a result's timestamp postdates
  any lifecycle events emitted during the same execution.

**Session lifecycle event** (from your local WAHA webhook listener):

```json
{
  "event_id": "<uuid>", "worker_id": "<id>", "session": "echo-<uuid>",
  "type": "session_status", "status": "WORKING", "timestamp": 1795718136.42
}
```

**QR event** (forward the actual image):

```json
{
  "event_id": "<uuid>", "worker_id": "<id>", "session": "echo-<uuid>",
  "type": "qr_ready", "status": "SCAN_QR_CODE",
  "qr": {"mimetype": "image/png", "data": "<base64>"},
  "timestamp": 1795718136.42
}
```

Never log QR contents or tokens. Do not send any other event types.

## Local WAHA operations

WAHA listens on `http://127.0.0.1:3000` (auth per its config, credentials
stay on the VM). Verify the exact schemas against the **pinned** WAHA GOWS
version deployed in the lab before coding against them — the official docs
may differ (e.g. the QR endpoint is documented as both GET and POST).

- **create_and_start**: `POST /api/sessions` with

  ```json
  {
    "name": "<session from the command>",
    "start": true,
    "config": {
      "gows": {"storage": {"messages": true, "messageSecrets": true,
                           "groups": true, "chats": true, "contacts": true,
                           "labels": false}},
      "webhooks": [{"url": "http://<agent-local-listener>/waha",
                    "events": ["session.status"]}]
    }
  }
  ```

  `messages=true` is mandatory (your SQLite delivery buffer depends on it).
  Subscribe **only** to `session.status`; no message-delivery webhooks (the
  batch loop owns message delivery). **Idempotency**: if the command is a
  retry and the session already exists, inspect it (`GET /api/sessions/{s}`)
  and reuse it instead of blindly re-creating.
- **start**: `POST /api/sessions/{session}/start` — existing sessions only;
  never create a replacement, never logout or delete credentials.
- **get_qr**: `GET /api/{session}/auth/qr` with `Accept: application/json`
  → `{"mimetype": "image/png", "data": "<base64>"}`. Forward the image.
- **get_status**: `GET /api/sessions/{session}` → `{"status": "..."}`.

## Local WAHA lifecycle callbacks

- Run a local HTTP listener for WAHA's `session.status` webhooks. It must be
  reachable from the WAHA container but not exposed publicly. **Careful**:
  Docker container localhost is not the VM host's localhost — bind the
  listener on the docker network / host interface WAHA can reach, and point
  the webhook URL at that address.
- On **SCAN_QR_CODE**: fetch the QR from the local WAHA API and forward it
  as a `qr_ready` event; forward every refresh (each callback). If the
  session is no longer in the QR state, skip. If delivery to Echo fails
  (non-2xx), retry while the QR is plausibly still valid (~60 s).
- On **STARTING / WORKING / STOPPED / FAILED**: forward as `session_status`.
- Do not modify the official WAHA Docker image or fork WAHA.

## Reconciliation

After an agent restart, reconcile the WAHA sessions you own (e.g.
`GET /api/sessions` filtered to `echo-*` names) against Echo by issuing
`get_status` where needed, so missed local callbacks do not leave Echo
permanently out of sync. On-demand reconciliation uses the `get_status`
command. Never poll Echo for anything other than the ~2 s command poll.

## Operational requirements

- The command poll and lifecycle forwarding must be independent of the
  message batch loop and of each other where practical.
- Bounded retries everywhere; no tight loops on failures.
- Secrets (fleet token, worker token, WAHA API key) are never logged.
- The existing batch delivery, exact-hash ACK semantics, and heartbeat
  behavior must not change.

## Tests (in the lab repo)

- Mock Echo HTTP server for both endpoints: auth (fleet vs worker token),
  claim → execute → report round-trips, event retries with the same
  `event_id`, 403/400/503 handling.
- Local WAHA mock: create idempotency (retry after created), start reuse,
  QR fetch + forwarding incl. refreshes, webhook listener address handling
  (container → host), reconciliation after restart.

## Out of scope

Public WAHA exposure, TLS on workers, WhatsApp message sending, cross-worker
migration, failover, VM provisioning, modifying the WAHA image.
