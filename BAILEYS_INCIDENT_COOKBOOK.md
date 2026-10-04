# Baileys Incident Cookbook

Quick operational guide for `echo-whatsapp-connector`.

Use this when WhatsApp connections suddenly stop working, reconnect repeatedly, or users start becoming disconnected.

---

## Golden rule

**Do not clear auth and do not ask users to scan QR again unless WhatsApp explicitly logged the linked device out.**

Most Baileys incidents should be recoverable by:

1. keeping the existing Postgres auth state,
2. fixing the WhatsApp Web version or Baileys package,
3. redeploying,
4. letting sessions reconnect.

---

# 30-second decision tree

```text
Connections failing
       |
       v
Do logs show 401 / logged_out / device_removed?
       |
       +-- YES --> user must re-pair
       |
       +-- NO
            |
            v
Are many users failing at the same time?
            |
            +-- YES --> probably compatibility / WhatsApp / Baileys incident
            |
            +-- NO --> probably account/network/session-specific
```

For a widespread incident:

```text
1. Check WhatsApp Web version
2. Check Baileys GitHub issues / PRs / releases
3. Test fix on one account
4. Deploy connector
5. Watch sessions reconnect
```

---

# Case 1 — Many users suddenly disconnect

Typical symptoms:

```text
degraded
disconnect:405
disconnect:408
handshake errors
connection failures
many accounts affected together
```

Do **not**:

```text
delete auth
unpair users
delete connection rows
ask everyone to scan QR
```

First check whether this looks like a WhatsApp Web version problem.

The connector normally calls:

```text
fetchLatestWaWebVersion()
```

automatically.

If upstream reports a known-good version, temporarily set:

```env
WA_WEB_VERSION_OVERRIDE=2.xxxx.xxxxx
```

Then redeploy the connector.

After the incident:

```env
WA_WEB_VERSION_OVERRIDE=
```

and go back to automatic version resolution.

---

# Case 2 — Baileys itself needs an update

Check:

```text
https://github.com/WhiskeySockets/Baileys/issues
https://github.com/WhiskeySockets/Baileys/pulls
https://github.com/WhiskeySockets/Baileys/releases
```

Look for reports matching our symptoms.

Examples:

```text
405
Noise handshake failure
connection.update regression
messages.upsert regression
pairing broken
LID problems
random logout
```

If a fixed Baileys version is released:

```bash
git checkout -b baileys-hotfix

npm install @whiskeysockets/baileys@<VERSION>

npm test
npm run build
```

Do not use:

```bash
npm install @whiskeysockets/baileys@latest
```

blindly.

Always install an explicit version.

Example:

```bash
npm install @whiskeysockets/baileys@7.0.0-rc15
```

---

# Test before production

Before deploying the fix to all users, test one real account.

Minimum smoke test:

```text
1. Connector starts
2. Existing account reconnects WITHOUT QR
3. Receive one private message
4. Receive one group message
5. sender identity looks correct
6. group name looks correct
7. restart connector
8. account reconnects again WITHOUT QR
```

If those work, deploy.

---

# Deploy on Render

If Render auto-deploy is enabled:

```bash
git add package.json package-lock.json
git commit -m "fix: update Baileys for WhatsApp compatibility"
git push origin main
```

Render should:

```text
build Docker image
→ start new connector
→ restore sessions from Postgres
→ reconnect WhatsApp sockets
```

Watch Render logs while it starts.

Healthy pattern:

```text
connecting
connected
```

Temporary pattern that may be fine:

```text
connecting
degraded
connecting
connected
```

Bad pattern:

```text
connecting
degraded
connecting
degraded
connecting
degraded
...
```

---

# Prefer manual deploys for Baileys hotfixes

For this service, manual deploy is safer than immediately auto-deploying dependency changes.

Recommended:

```text
commit fix
→ push
→ test
→ Render: Deploy latest commit
```

This gives us control over when all WhatsApp sessions restart.

---

# Case 3 — One user shows 401 / logged_out

Example:

```text
provider_raw_status=logged_out:401
status=pairing_required
```

This is different.

The linked device is no longer authorized.

A package update normally will **not** restore that session.

Correct flow:

```text
PAIRING_REQUIRED
↓
Echo tells user WhatsApp needs reconnecting
↓
show QR / pairing code
↓
user links device
↓
CONNECTED
```

This is one of the few times we should ask for QR again.

---

# Case 4 — Connector is down but WhatsApp auth is intact

Example:

```text
Render crashed
deploy failed
server restarted
container restarted
```

Usually do nothing to user auth.

Auth state lives in Postgres:

```text
whatsapp_connector.auth_credentials
whatsapp_connector.auth_keys
```

Restart/deploy the connector.

Expected:

```text
connector starts
↓
loads Postgres auth
↓
opens socket
↓
CONNECTED
```

No QR should be required.

---

# Case 5 — Pairing works for existing users but new users cannot pair

This can happen during WhatsApp protocol changes.

Important distinction:

```text
existing users → may continue working
new onboarding → broken
```

Do not disconnect existing users trying to fix onboarding.

Check Baileys upstream for:

```text
QR pairing
requestPairingCode()
companion registration
Linked Devices changes
```

We can temporarily stop new onboarding while existing sessions continue working.

---

# Case 6 — Messages stop arriving but sessions say CONNECTED

First test with one account:

```text
send DM to account
send group message
```

Check connector logs.

Then check:

```sql
SELECT *
FROM whatsapp_connector.event_inbox
ORDER BY received_at DESC
LIMIT 20;
```

Possible cases:

### No event appears

Likely problem:

```text
Baileys socket / message event
```

Check:

```text
messages.upsert
Baileys regression
WhatsApp protocol change
```

### Event appears but Echo does not process it

Then Baileys is probably fine.

Problem is likely:

```text
Echo inbox consumer
event claim
event parsing
connection mapping
WFM ingestion
```

Do not update Baileys unnecessarily.

---

# Case 7 — Group messages break but private chats still work

Check:

```text
participant
participantAlt
LID
groupMetadata()
groups.update
```

Do not change canonical identity back to phone numbers.

Our rule remains:

```text
LID = preferred canonical identity
phone JID = optional metadata
phone number = optional metadata
```

A Baileys fix may change how these fields are populated, but Echo should continue consuming the normalized connector event.

---

# Reconnect storms

After a deployment, many sessions may reconnect.

The connector already staggers restored sessions using:

```env
SESSION_START_STAGGER_MS=5000
```

Do not set this to zero in production.

For example:

```text
20 accounts
5 second stagger
≈ 100 seconds to start all sessions
```

That is intentional.

We prefer a slow healthy reconnect over 20 simultaneous WhatsApp handshakes.

---

# Things NEVER to do during an incident

Do not:

```text
DROP auth tables
DELETE auth credentials
DELETE Signal keys
regenerate AUTH_ENCRYPTION_KEY
mass-unpair users
mass-delete connections
upgrade several dependencies at once
change Baileys + Node + database code together
```

Especially:

## Never change `AUTH_ENCRYPTION_KEY`

unless performing a planned re-encryption migration.

Changing it means existing encrypted WhatsApp auth can no longer be decrypted.

---

# Useful Render checks

Check:

```text
Deploy logs
Runtime logs
Memory
Restarts
Health check
```

Health endpoint:

```text
GET /health
```

A healthy HTTP service does **not** necessarily mean WhatsApp sessions are healthy.

Also inspect connection states.

Useful statuses:

```text
connected
connecting
degraded
pairing_required
```

---

# Incident checklist

Copy/paste this into an incident note:

```text
[ ] Is this one user or many users?
[ ] What disconnect/status code do we see?
[ ] Are existing sessions affected?
[ ] Is only new pairing affected?
[ ] Did WhatsApp Web version change?
[ ] Is there a matching Baileys issue?
[ ] Is there a matching Baileys PR?
[ ] Is there a new Baileys release?
[ ] Can one test account reproduce it?
[ ] Can one test account reconnect without QR after the fix?
[ ] Did private DM work?
[ ] Did group message work?
[ ] Did connector restart work?
[ ] Deploy production
[ ] Watch reconnects
```

---

# Severity guide

## SEV 3 — One account

```text
one user disconnected
others healthy
```

Investigate account/session.

Do not upgrade Baileys globally unless evidence points there.

---

## SEV 2 — New onboarding broken

```text
existing sessions healthy
new QR/pairing fails
```

Pause onboarding if needed.

Existing users can continue.

Check upstream pairing issues.

---

## SEV 1 — Many active sessions broken

```text
many users degraded/disconnected together
messages stopped
```

Likely compatibility incident.

Immediately:

```text
1. inspect common error
2. check WA Web version
3. check Baileys upstream
4. test one-account fix
5. deploy
```

---

# After an incident

Record:

```text
date
duration
affected users
symptom
status/error code
root cause
fix
Baileys version
WA Web version
whether re-pair was required
```

Example:

```text
2026-XX-XX

Impact:
18/20 sessions disconnected.

Symptom:
HTTP 405 during connection handshake.

Cause:
WhatsApp Web compatibility change.

Fix:
Updated WA Web version.

Baileys:
7.0.0-rcXX

User action:
None.

Recovery:
All sessions reconnected from stored Postgres auth.
```

This will become extremely useful after a few incidents.

---

# The mental model

Remember:

```text
WhatsApp changed something
        ↓
Baileys may temporarily break
        ↓
our users' auth is STILL in Postgres
        ↓
wait/find fix
        ↓
deploy fix
        ↓
sessions reconnect
```

Only:

```text
401 / logged_out / device_removed
```

means:

```text
user must pair again
```

Everything else should first be treated as a recoverable connector incident.