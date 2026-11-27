# WAHA agent task: account identity reporting

Implementation instructions for the `upcloud-waha-lab` repository (agent
`exp/waha_mgmt.py`). Echo side is implemented in echo-v2 migration
`0068_waha_account_identity` — this document is the finalized contract.

## Background

Echo verifies that a child linked the *correct* WhatsApp account before
activating Guard (child-phone verification). The verification compares the
child's registered phone against the **provider-reported** account phone.
Today the agent reports only lifecycle statuses, so WAHA verification can
never complete. The GOWS session API exposes the identity (verified live
on WAHA 2026.10.1):

```
GET /api/sessions/{s}  →  { ..., "me": {"id": "972546610653@c.us",
                                         "jid": "972546610653:70@s.whatsapp.net",
                                         "lid": "21758421798914@lid",
                                         "pushName": "..." } }
```

Facts verified live on 2026-11-27:

- A **paired** WORKING session has `me.id` = `<digits>@c.us` (phone-number
  JID) and `me.jid` = `<digits>:<device>@s.whatsapp.net`.
- An **unpaired/just-created** session reports status WORKING with `me`
  entirely empty (`id: ""`, `jid: null`, `lid: ""`) and
  `engine.gows.connected: false` — empty `me` means *identity
  unavailable*, never an error.
- `me` is NOT part of `session.status` webhook payloads — the agent must
  fetch it from the local REST API.

## Contract extension (backward compatible)

Add an optional `me` object to two existing event shapes. Echo ignores
unknown fields on old versions; old agents simply never send it.

### 1. `command_result` for `get_status`

Extend `_get_status` in `exp/waha_mgmt.py`: the WAHA `GET
/api/sessions/{s}` response already contains `me` — pass it through:

```python
def _get_status(self, session):
    ...
    s = self._waha_call(self.waha.get_session, session)
    result = {"status": s.get("status")}
    me = s.get("me")
    if isinstance(me, dict):
        result["me"] = me
    return True, None, result
```

`command_result` events for `get_status` then carry:

```jsonc
{
  "type": "command_result", "command_id": "...", "session": "echo-...",
  "success": true, "status": "WORKING",
  "me": {"id": "9725...@c.us", "jid": "9725...:70@s.whatsapp.net",
         "lid": "...", "pushName": "..."}
}
```

### 2. `session_status` events reporting WORKING

When the lifecycle listener (or `reconcile()`) forwards a status of
`WORKING`, fetch the session once (`GET /api/sessions/{s}`) and attach
`me` to the event payload. Only for WORKING — identity is meaningless in
any other state, and Echo clears stored identity on non-WORKING
transitions. If the fetch fails (session missing, WAHA restarting), send
the event **without** `me` rather than failing the event — lifecycle
reporting must never depend on the identity fetch.

```jsonc
{
  "type": "session_status", "session": "echo-...", "status": "WORKING",
  "timestamp": ..., "me": {"id": "9725...@c.us", ...}
}
```

Attach the `me` object **verbatim** as returned by WAHA — Echo does the
extraction/validation. Do not synthesize or transform fields.

## Echo-side semantics (already implemented — for reference)

- Echo extracts the phone **only** from `me.id` (`@c.us`) or `me.jid`
  (`@s.whatsapp.net`) user part, device suffix stripped, digits only.
- `lid`-only or empty identity → treated as *unavailable*: verification
  stays pending, never passes, never fails as a mismatch.
- Identity is persisted atomically with the lifecycle transition and
  **cleared on any applied non-WORKING transition** (SCAN_QR_CODE /
  STOPPED / FAILED) so a stale identity can never survive re-pairing.
  Because every WORKING event re-reports `me`, the identity self-heals
  after reconnects.
- Duplicate/stale events are fenced exactly as before; an *identical*
  replayed WORKING event still persists a newly-present identity.
- Every fresh WORKING application re-runs child-phone verification, so
  the first event carrying the identity completes verification
  automatically (match → activate; mismatch → guardian rejection).
- Ops Center surfaces four separate states per user: WhatsApp connected /
  account identity available / child verified / Guard monitoring active,
  plus `awaiting_account_identity` and `awaiting_account_verification`
  warnings.

## Testing

- Extend `exp/test_agent.py` FakeWaha: `GET /api/sessions/{s}` returns a
  configurable `me`; cover (a) get_status passes `me` through, (b)
  WORKING lifecycle events carry `me`, (c) non-WORKING events never do,
  (d) identity-fetch failure still delivers the status event without
  `me`, (e) empty `me` (`{"id": "", "jid": null}`) is passed through
  verbatim — filtering is Echo's job.
- Verify against the live worker before deploying (AGENTS.md rule): the
  current production session `gaddy-test` returns populated `me`; a fresh
  unpaired session returns empty `me`.
- Mock Echo (`exp/mock_echo.py`) needs no changes — unknown fields pass
  through.
