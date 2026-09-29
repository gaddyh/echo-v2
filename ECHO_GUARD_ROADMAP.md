# Echo Guard — Implementation Roadmap

> **Status:** target implementation plan  
> **Strategy:** Echo Guard first, Echo Kids second. Expand Echo v2; do not rewrite or fork the backend.

## 0. Product definition

### Echo Guard
Echo Guard is the parent-facing WhatsApp safety product.

> **If something dangerous starts in a child's WhatsApp, it is better to know before it escalates.**

Initial risk families:
1. suspicious / unknown-contact / grooming-like patterns;
2. bullying / harassment / exclusion;
3. emotional distress / self-harm signals.

### Echo Kids
Echo Kids comes **after Guard is proven**. It gives the child direct value from the same WhatsApp connection:
- who is waiting for me;
- things I promised;
- reminders / snooze;
- important chats;
- scheduled replies;
- digest / follow-up utility.

> **Echo Guard is the reason the parent buys. Echo Kids is the reason the child wants to keep it connected.**

---

# 1. Architectural invariants

- Adult Echo remains working.
- Only the child needs a linked WhatsApp connection for Guard.
- The guardian needs an Echo account / alert destination, not a linked-device connection.
- Reuse one WhatsApp ingestion stack.
- The LLM interprets language; deterministic code owns alert policy, state, delivery, permissions, retention and visibility.
- No parent raw-chat browser.
- Guard results must be versioned and historically inspectable.
- Do not build multi-analyzer infrastructure until Echo Kids needs to run beside Guard.

---

# 2. Existing foundation to reuse

The repository already has most of the platform plumbing:

- Green API provisioning, QR pairing, one-time-code fallback, re-pairing and disconnect;
- secure webhook authentication and provider-event normalization;
- message deduplication and persistence;
- `activity_version`, chat analysis scheduling and version-fenced commits;
- background analysis worker and retry behavior;
- conversation snapshots;
- structured OpenAI analysis with prompt/model/analyzer versions;
- LangSmith tracing;
- transcription and media summarization;
- LLM judge/evaluation primitives;
- runtime/idempotency infrastructure;
- operational health alerts.

The current missing layer is the **Guard product layer**, not the WhatsApp plumbing.

---

# 3. Public product / acquisition

## Already done
- [x] Echo Guard branding
- [x] Guard landing page
- [x] `/guard` route
- [x] Echo Guard OG image
- [x] first-50 pilot positioning
- [x] waitlist form
- [x] child count + child ages in UI
- [x] connection explanation
- [x] Echo Kids reveal
- [x] existing waitlist storage infrastructure
- [x] owner waitlist notification infrastructure

## Follow-up
- [ ] persist `children_count`
- [ ] persist `children_ages`
- [ ] add signup source (`guard`)
- [ ] verify Guard signup triggers owner notification
- [ ] track landing-view → signup conversion
- [ ] add privacy / terms links before external pilot

These are not blockers for the first Guard analysis slice.

---

# 4. Roadmap overview

| Phase | Goal | Exit criterion |
|---|---|---|
| 0 | Freeze baseline | Guard flags off = Adult Echo unchanged |
| 1 | Guardian ↔ child identity | one guardian resolves to one monitored child |
| 2 | Guard analyzer | snapshot → structured Guard observation |
| 3 | Guard persistence | every observation is durable/versioned |
| 4 | Guard scheduling | active chats analyzed with bounded latency |
| 5 | Shadow mode | real traffic analyzed, no parent notifications |
| 6 | Verifier + alert policy | deterministic alert/no-alert decision |
| 7 | Internal alert delivery | guardian receives one reliable Guard alert |
| 8 | Parent feedback surface | alert can be acknowledged/corrected |
| 9 | Consent + onboarding | family can connect without DB edits |
| 10 | Privacy + retention | child-data lifecycle is explicit/bounded |
| 11 | Guard evaluation | safety quality measured by category |
| 12 | Groups + media | real WhatsApp usage covered |
| 13 | External pilot | 5–10 families |
| 14 | Longitudinal state | risk accumulates across windows |
| 15 | Echo Kids | child gets direct product value |
| 16 | Multi-analyzer architecture | Guard + Echo Kids run independently |
| 17 | Scale/reliability | ready beyond small pilot |

---

# 5. Phase 0 — freeze mature Echo

**Goal:** Guard work must not destabilize Adult Echo.

- [x] preserve pre-Guard baseline
- [x] document Guard expansion
- [ ] create Guard implementation branch
- [ ] add feature flags:
  - `GUARD_ENABLED`
  - `GUARD_ANALYSIS_ENABLED`
  - `GUARD_ALERTS_ENABLED`

**Exit:** disabling Guard leaves current Echo behavior unchanged.

---

# 6. Phase 1 — minimal guardian / child identity

Do **not** build a full `Family` aggregate yet.

```text
GuardianChildLink

id
guardian_user_id
child_user_id
status              pending | active | revoked
child_consented_at
safety_enabled_at
created_at
updated_at
```

Rules:
- child remains a normal Echo `user`;
- child's WhatsApp remains a normal connection;
- guardian can exist without a linked-device connection;
- an active link makes the child eligible for Guard analysis.

Tasks:
- [ ] migration: `guardian_child_links`
- [ ] repository
- [ ] create / activate / revoke
- [ ] guardians-for-child lookup
- [ ] children-for-guardian lookup
- [ ] simple admin/debug seed path

For the first three internal children, manual seeding is acceptable.

**Exit:** `guardian → active link → child → child connection` resolves reliably.

---

# 7. Phase 2 — Guard analysis contract

Build a **separate Guard analyzer**. Never mix this into `WaitingForMeAnalyzer`.

Suggested structured result:

```text
GuardObservation

decision:
  none
  watch
  concerning
  urgent

categories:
  suspicious_contact
  personal_information_request
  location_request
  secrecy
  meeting_request
  platform_migration
  harassment
  bullying
  threats
  exclusion
  emotional_distress
  self_harm

signals:
  [structured signal keys]

confidence:
  0.0 .. 1.0

summary:
  short safe explanation

evidence_message_ids:
  [message IDs]

target_version
model
prompt_version
analyzer_version
created_at
```

Start with **one analyzer + one schema**, not separate Grooming/Bullying/Distress analyzers.

Tasks:
- [ ] `GuardAnalyzer` protocol
- [ ] `LLMGuardAnalyzer`
- [ ] Guard system prompt
- [ ] strict structured-output validation
- [ ] model/prompt/analyzer version constants
- [ ] evidence IDs, not arbitrary copied transcript
- [ ] malformed-output and retry tests

**Exit:** a labeled conversation fixture produces a valid GuardObservation.

No parent alert yet.

---

# 8. Phase 3 — Guard result persistence

Store historical results, not just "latest".

```text
guard_analysis_results

id
subject_user_id
connection_id
chat_id
target_version

decision
categories
signals
confidence
summary
evidence_message_ids

model
prompt_version
analyzer_version
created_at
```

Recommended:
- append-only;
- unique identity by child/chat/version/analyzer;
- JSON fields for categories/signals where appropriate;
- no `RiskEpisode` yet.

Tasks:
- [ ] ORM model
- [ ] migration
- [ ] repository
- [ ] append-only insert
- [ ] query by child/chat/date
- [ ] simple debug view/script
- [ ] include result ID in traces

**Exit:** every Guard run can be inspected later.

---

# 9. Phase 4 — Guard scheduling semantics

The current quiet-period debounce is good for Waiting For Me, but safety must not wait forever for a chat to become silent.

Target:

```text
new message
   ↓
chat becomes dirty
   ↓
schedule Guard analysis no later than +2 minutes
   ↓
more messages arrive
   ↓
do not keep pushing the deadline forever
   ↓
analyze newest snapshot
```

After analysis:

```text
if activity_version advanced while processing:
    schedule another run
else:
    clean
```

Treat 2 minutes as a **maximum batching latency**, not "2 minutes of silence".

For Guard v1 we can avoid per-analyzer cursors because:
- adult account → WaitingForMe
- Guard child → GuardAnalyzer

Tasks:
- [ ] identify Guard-enabled child chats
- [ ] route those chats to Guard processor
- [ ] bounded-latency scheduling
- [ ] preserve activity-version fencing
- [ ] duplicate-run protection
- [ ] configurable Guard cadence (default e.g. 120s)
- [ ] analysis-latency metric
- [ ] tokens/cost per child/day metric

**Exit:** a continuously active chat still gets periodic safety analysis.

---

# 10. Phase 5 — shadow mode

Mandatory before alerts.

## Stage A — synthetic/offline

Create scenario families:
- harmless new contact;
- friendly teasing;
- normal argument;
- suspicious persistence;
- personal-info request;
- location probing;
- secrecy request;
- meeting request;
- repeated harassment;
- exclusion;
- ordinary sadness;
- persistent distress;
- explicit high-risk distress;
- Hebrew slang / typos;
- mixed Hebrew/English.

## Stage B — internal dogfooding

With explicit consent, enable Guard on the three internal child accounts.

```text
conversation
   ↓
GuardObservation
   ↓
DB + trace
   ↓
NO parent alert
```

Track:
- observations / child / day;
- watch / concerning / urgent rate;
- obvious false positives;
- obvious false negatives;
- analysis latency;
- tokens and cost per child/day.

**Exit:** we understand real Guard output before notifications exist.

---

# 11. Phase 6 — verifier + deterministic alert policy

The model must not directly decide "send parent alert".

```text
conversation snapshot
        ↓
GuardAnalyzer
        ↓
candidate GuardObservation
        ↓
GuardVerifier
        ↓
GuardAlertPolicy
        ↓
alert / no alert
```

Verifier checks:
- is the claimed risk supported?
- are the signals actually present?
- is benign context plausible?
- is severity justified?

Starting policy:

```text
none        → persist
watch       → persist
concerning  → notify once if verified
urgent      → notify immediately if verified
```

Policy inputs may include:
- decision;
- categories;
- confidence;
- verifier result;
- signal combination;
- prior alert;
- cooldown.

Tasks:
- [ ] `GuardVerifier`
- [ ] structured verifier result
- [ ] deterministic `GuardAlertPolicy`
- [ ] policy-table unit tests
- [ ] cooldown
- [ ] dedup key
- [ ] explicit no-alert reason for debugging

**Exit:** same observation + verifier result always produces the same deterministic action.

---

# 12. Phase 7 — internal guardian alert delivery

New domain object:

```text
GuardAlert

id
guard_analysis_result_id
guardian_user_id
subject_user_id
chat_id
severity
categories
summary
reason
created_at
acknowledged_at
dismissed_at
feedback
```

Recommended delivery record:

```text
GuardAlertDelivery

id
guard_alert_id
provider
destination
status
attempt_count
provider_message_id
last_error
created_at
delivered_at
```

New service:

```text
GuardAlertNotifier
```

Responsibilities:
- child → guardian resolution;
- safe alert formatting;
- delivery;
- idempotency;
- retries;
- delivery status.

Do not reuse the waitlist notifier as the Guard domain notifier.

For proactive WhatsApp alerts outside an open session, prepare an approved Guard template. For the internal pilot, use the simplest permitted path available.

**Exit:** one qualifying event creates one reliable guardian alert, not one alert per message.

---

# 13. Phase 8 — parent alert UI + feedback

Build a Guard-specific parent surface.

Minimum alert view:
- child;
- timestamp;
- severity;
- category;
- concise summary;
- derived signals;
- acknowledgement state.

Actions:
```text
ראיתי
מדויק
לא מדויק
לא מדאיג מבחינתי
כבר טיפלתי
```

Important:
- no browse-all-chats;
- no search-child-messages;
- no automatic full transcript.

Tasks:
- [ ] authenticated Guard parent page
- [ ] recent/active alerts
- [ ] alert detail
- [ ] acknowledge
- [ ] false-positive feedback
- [ ] useful/not-useful feedback
- [ ] durable feedback persistence
- [ ] LangSmith annotation projection

**Exit:** every alert can be reviewed and labeled by the guardian.

---

# 14. Phase 9 — onboarding and consent

Target flow:

```text
guardian joins
   ↓
creates/invites child
   ↓
child sees what Guard does
   ↓
consent recorded
   ↓
child connects WhatsApp
   ↓
QR / one-time code
   ↓
Guard enabled
```

Capture explicitly:
- guardian identity;
- child identity;
- relationship;
- consent;
- safety enabled;
- connection health;
- revocation.

Reuse the current linked-device infrastructure.

Disconnect rule:

```text
child removes linked device
   ↓
connection unauthorized
   ↓
Guard disabled
   ↓
guardian informed monitoring stopped
```

**Exit:** a new family can onboard without developer/DB intervention.

---

# 15. Phase 10 — privacy, retention and deletion

Before a real external pilot, define retention for:

- raw text;
- media URLs;
- audio transcripts;
- media summaries;
- Guard observations;
- Guard alerts;
- feedback.

Principle:

> **Process content; retain the minimum useful safety state.**

Tasks:
- [ ] retention policy
- [ ] scheduled cleanup
- [ ] raw-message deletion
- [ ] media URL cleanup
- [ ] transcript/summary cleanup
- [ ] account deletion
- [ ] disconnect cleanup rules
- [ ] deletion audit events
- [ ] no raw child content in logs
- [ ] no raw child content in analytics metadata
- [ ] privacy policy
- [ ] provider/data-processing disclosure

**Exit:** for every stored child-data type we can answer why it exists, who can access it, how long it lives, and how it is deleted.

---

# 16. Phase 11 — Guard evaluation

Reuse Echo's evaluation machinery, but build Guard-specific datasets and rubrics.

Use **scenario families**, not isolated toxic messages.

### Suspicious contact
```text
friendly unknown contact
vs persistent unknown contact
vs location + secrecy
vs meeting escalation
```

### Bullying
```text
joke
vs one-off insult
vs repeated humiliation
vs group exclusion
```

### Distress
```text
ordinary frustration
vs temporary sadness
vs persistent hopelessness
vs high-risk self-harm language
```

Required coverage:
- Hebrew;
- English;
- mixed language;
- slang;
- typos;
- sarcasm;
- emojis;
- short/long context;
- voice when supported;
- screenshots/images when supported.

Track per category:
```text
precision
recall
false positives
false negatives
alert rate / child / day
time to alert
verifier disagreement
parent feedback
```

Always version:
```text
model
prompt
analyzer
dataset
```

Do not collapse safety quality into one overall score.

**Exit:** every Guard change can be compared against a frozen baseline before release.

---

# 17. Phase 12 — group chats + media coverage

## Group chats

Guard eventually needs groups for:
- bullying;
- coordinated exclusion;
- pile-ons;
- repeated humiliation;
- threats.

Tasks:
- [ ] enable Guard-relevant group ingestion
- [ ] preserve sender identity inside group messages
- [ ] participant-aware snapshot
- [ ] group-specific eval suite
- [ ] noise-control for normal group banter
- [ ] cost limits by group size

## Voice

Reuse transcription:
- [ ] voice → transcript → Guard snapshot
- [ ] failure handling
- [ ] Hebrew slang evaluation

## Images/screenshots/video

Use existing media summarization only after text + voice are stable.

**Exit:** supported chat/message types are explicit and tested.

---

# 18. Phase 13 — external pilot (5–10 families)

Do not jump straight from internal dogfooding to all 50 waitlist users.

Measure:

### Funnel
```text
landing visits
signup
onboarding start
onboarding complete
```

### Connection
```text
pairing success
time to connect
disconnect rate
re-pair rate
```

### Safety
```text
alerts / child / week
useful alerts
false alarms
missed events
time to alert
alerts by category
```

### Trust
Guardian:
- Would you keep this connected?
- Would you pay?
- Was any alert genuinely useful?
- Did it feel too invasive?

Child:
- Did you understand what was monitored?
- Did you want to disconnect it?
- Did it create friction?

During this small pilot, manually review concerning/urgent alerts when practical.

**Exit:** we repeatedly hear:

> **"I am glad I knew about this."**

without alert fatigue or unacceptable privacy friction.

---

# 19. Phase 14 — longitudinal risk state

Only after v1 observations are useful.

```text
RiskEpisode

id
subject_user_id
chat_id
category
status
severity
first_observed_at
last_observed_at
last_observation_id
summary
revision
resolved_at
```

Example:

```text
new contact             → no alert
persistent contact      → watch
asks location           → concerning
asks secrecy            → escalation
requests meeting        → urgent
```

Possible derived signals:
- first-seen contact;
- repeated unanswered contact;
- unusual-hour messaging;
- secrecy;
- personal-info request;
- location probing;
- photo request;
- platform migration;
- meeting request;
- age/identity inconsistency;
- tone shift;
- group exclusion;
- deletion behavior where observable.

**Exit:** Guard can explain that an interaction **escalated over time**, not merely that one message looked risky.

---

# 20. Phase 15 — Echo Kids

Only after Guard works.

Reuse current Echo utility first:
- Waiting For Me;
- reminders;
- snooze;
- important chats;
- digest;
- scheduled reply.

Adapt UX/language for younger users, but avoid rebuilding the responsibility engine.

Product test:

> Would the child voluntarily keep Echo Kids connected even without thinking about Guard?

**Exit:** at least some pilot children use it voluntarily and report direct value.

---

# 21. Phase 16 — multi-analyzer architecture

This is when the single `last_processed_version` becomes restrictive.

Target:

```text
ChatAnalysisCursor

user_id
chat_id
analyzer_key
last_processed_version
last_attempted_version
updated_at
```

Examples:
```text
responsibility.v4
guard.v1
```

Migration:
1. add per-analyzer cursor;
2. migrate responsibility;
3. migrate Guard;
4. keep legacy cursor temporarily;
5. remove legacy path only after production proof.

Benefits:
- Guard failure does not block Echo Kids;
- Echo Kids failure does not block Guard;
- independent retry;
- independent versions;
- shadow analyzers;
- A/B tests.

**Exit:** one child stream powers both products independently.

---

# 22. Phase 17 — scale and reliability

Only after pilot quality + demand justify it.

Add as needed:
- queue leases/claiming;
- multiple workers;
- per-child isolation;
- retry queue;
- dead-letter handling;
- alert-delivery idempotency;
- provider backpressure;
- LLM budget limits;
- per-child cost metrics;
- analysis-latency SLO;
- connection-health monitoring;
- delivery monitoring;
- DB/index scaling.

---

# 23. Exact first implementation slice

Implement this first:

```text
1. GuardianChildLink
2. mark one internal child Guard-enabled
3. reuse existing WhatsApp ingestion
4. route child's due chat to GuardAnalyzer
5. analyze conversation snapshot
6. persist GuardObservation
7. inspect observations manually
```

Do **not** add yet:
- parent alerts;
- Echo Kids;
- RiskEpisode;
- parent dashboard;
- retention rewrite;
- multi-analyzer cursors.

Then:

### Slice 2 — shadow quality
```text
offline fixtures
→ internal child traffic
→ manual review
→ prompt/schema iteration
```

### Slice 3 — first real alert
```text
GuardObservation
→ GuardVerifier
→ GuardAlertPolicy
→ GuardAlert
→ GuardAlertNotifier
→ guardian WhatsApp
```

At that point Echo Guard is end-to-end.

---

# 24. Suggested code-artifact order

```text
01 migration_guardian_child_links
02 guardian_child_link_repository.py
03 guard_models.py
04 guard_analyzer.py
05 llm_guard_analyzer.py
06 migration_guard_analysis_results
07 guard_analysis_repository.py
08 guard_analysis_processor.py
09 guard scheduling/routing
10 guard debug view/script
11 guard eval fixtures
12 guard_verifier.py
13 guard_alert_policy.py
14 migration_guard_alerts
15 guard_alert_repository.py
16 guard_alert_notifier.py
17 Guard parent feedback API/UI
18 onboarding/consent
19 retention cleanup
20 group/media coverage
21 RiskEpisode / longitudinal state
22 Echo Kids
23 per-analyzer cursors
```

Use existing repository naming/conventions when a nearby abstraction already exists.

---

# 25. Explicitly not doing now

Do not let these delay the first Guard observation:

- full family billing;
- `Family` aggregate;
- multiple guardian roles;
- custom safety model training;
- hardware;
- GPS;
- screen-time control;
- app blocking;
- parent raw-chat browser;
- every safety category;
- full longitudinal risk engine;
- Echo Kids redesign;
- multi-worker scale architecture.

---

# 26. North star

```text
private conversational stream
        ↓
secure ingestion
        ↓
contextual understanding
        ↓
derived intelligence
        ↓
Echo Guard / Echo Kids / Adult Echo
        ↓
alerts + actions
        ↓
feedback
        ↓
evaluation
```

The moat should not be:

> "We send WhatsApp messages to an LLM."

It should become:

> **Reliable, longitudinal, evaluated intelligence over private conversation streams, with strong state, policy, privacy and feedback boundaries.**

---

# 27. Immediate next task

> **GuardianChildLink + GuardAnalyzer + GuardObservation persistence + shadow execution for one internal child.**

Do not build alert delivery until we have inspected real Guard observations.
