# Echo MVP — High-Level Design & Roadmap v3

> **Status:** target architecture / product roadmap.  
> The existing Echo v2 implementation remains valid and is expanded, not replaced.

## 1. Product direction

Echo is evolving from a personal WhatsApp follow-up assistant into a **family WhatsApp assistant with a safety layer**.

The core idea is deliberately two-sided:

### For the child / family member

> **Echo helps you manage your WhatsApp.**

Echo keeps the existing personal utility:
- who is waiting for me;
- what I promised;
- reminders and snoozes;
- scheduled messages;
- daily/periodic digest;
- message/context assistance.

A child should receive enough direct value that they would want Echo connected even without the parental safety feature.

### For the parent / guardian

> **Echo watches for meaningful safety risks without becoming a chat-reading tool.**

The guardian receives derived safety alerts rather than a searchable copy of the child's conversations.

Initial safety families:
1. bullying / exclusion / harassment;
2. suspicious or unknown-contact / grooming patterns;
3. distress / self-harm signals.

The product is not positioned as spyware or a parental chat archive.

---

## 2. Expansion principle: preserve Echo

This is **not a rewrite** and not a deletion of the existing adult product.

Everything already implemented in Echo v2 remains useful:

- WhatsApp connection lifecycle;
- Green API ingestion;
- normalized provider events;
- immutable message persistence;
- quiet-period debounce;
- conversation snapshots;
- activity-version fencing;
- LLM analyzer infrastructure;
- LLM-as-judge;
- human annotation queues;
- false-positive feedback;
- transcription and media preparation;
- scheduling;
- digest infrastructure;
- mini-app actions;
- idempotent external execution;
- runtime error taxonomy;
- webhook authentication/dedup;
- observability and LangSmith dashboards.

The new product should be built **on top of these primitives**.

---

## 3. Product model

Echo now has three product surfaces.

### 3.1 Echo Adult

The current product.

Primary value:
- waiting-for-me detection;
- obligations / follow-ups;
- reminders;
- scheduled replies;
- digest;
- conversation management.

### 3.2 Echo Kids

The same personal-assistant value adapted to a younger user.

Initial features can reuse the current product almost unchanged:
- who is waiting for me;
- things I said I would do;
- snooze/remind;
- simple digest;
- scheduled message;
- important chats.

The UX and language may differ, but the underlying responsibility pipeline should be shared.

### 3.3 Parent Safety

A guardian-facing safety layer attached to a child account.

The guardian should receive:
- risk category;
- severity;
- concise explanation;
- time / persistence of the pattern;
- safe derived context;
- recommended next step when appropriate.

The guardian should **not** automatically receive:
- a complete chat history;
- a searchable message archive;
- access to unrelated conversations.

---

## 4. Trust model

The product only works if both parent and child understand the boundary.

### Child promise

- Echo is genuinely useful to the child.
- Monitoring is disclosed.
- The child can see what categories are monitored.
- Parent access is limited to defined safety alerts.
- Echo is not a hidden remote chat reader.

### Parent promise

- Echo monitors continuously while the connection is healthy.
- Important risk patterns are surfaced.
- False alarms can be reported.
- The system explains why an alert was raised.
- The product does not claim perfect detection.

### Product rule

> **Useful for the child. Reassuring for the parent.**

---

## 5. Identity and family model

The current `users` model should remain the identity anchor.

A parent and a child are both Echo users.

That is intentionally additive: the existing tables already key WhatsApp connections, messages, chats, scheduling, and analysis by `user_id`.

New family concepts:

```text
Family
  id
  created_at

FamilyMembership
  family_id
  user_id
  role              guardian | child
  status            invited | active | revoked
  consented_at
  created_at

GuardianChildPolicy
  guardian_user_id
  child_user_id
  alert_policy
  enabled_categories
  created_at
  updated_at
```

A child therefore keeps a normal Echo identity and normal WhatsApp connection.

No special "monitored phone" transport is required in the domain model.

---

## 6. High-level architecture

```text
                         Echo Business Bot
                                │
                     onboarding / commands
                                │
                    ┌───────────┴───────────┐
                    │                       │
               Parent User              Child User
                    │                       │
             WhatsApp connection     WhatsApp connection
                    │                       │
                    └───────────┬───────────┘
                                │
                       Provider adapters
                                │
                    normalize / auth / dedup
                                │
                                ▼
                        Message ingestion
                                │
                    persist + activity_version
                                │
                         quiet interval
                                │
                                ▼
                       Stable conversation
                             snapshot
                                │
                 ┌──────────────┴──────────────┐
                 │                             │
                 ▼                             ▼
       Responsibility analysis          Safety analysis
        adult + child utility              child only
                 │                             │
                 ▼                             ▼
        Responsibility state              Risk state
                 │                             │
        digest / mini-app              alert policy
        reminders / actions                  │
                                               ▼
                                         Guardian alert
                                               │
                                               ▼
                                      feedback / annotation
```

The critical rule is:

> **One ingestion stream, multiple derived analyses.**

We do not duplicate WhatsApp ingestion for the safety product.

---

## 7. Shared conversation pipeline

The existing pipeline remains the foundation:

```text
provider webhook
→ authenticate
→ normalize
→ deduplicate
→ persist message
→ increment chat.activity_version
→ move next_analysis_at forward
→ wait for quiet interval
→ load stable conversation snapshot
```

After that snapshot exists, multiple analyzers may operate on it.

### Analyzer types

Initial target:

```text
responsibility
safety
```

Future analyzers can be added without changing ingestion:

```text
importance
scam
school/task
relationship-health
other product features
```

---

## 8. Analysis cursor evolution

The current Echo v2 chat row has one `last_processed_version`.

That is sufficient for a single analyzer but becomes restrictive once a child may run both responsibility and safety analysis.

Target model:

```text
ChatAnalysisCursor

user_id
chat_id
analyzer_key
last_processed_version
last_attempted_version
updated_at
```

Example keys:

```text
responsibility.v4
safety.v1
```

The chat still owns:

```text
activity_version
next_analysis_at
```

Each analyzer owns its own processing cursor.

This allows:
- safety failure without blocking responsibility analysis;
- analyzer-specific retries;
- independent prompt/model versions;
- shadow analyzers;
- A/B evaluation.

### Migration strategy

Do not delete `chats.last_processed_version` immediately.

1. introduce per-analyzer cursors;
2. migrate responsibility worker to a cursor;
3. keep the legacy column during transition;
4. remove only after production proves the new path.

---

## 9. Responsibility domain

The existing responsibility / waiting-for-me product remains intact.

Longer-term target:

```text
Responsibility
  id
  user_id
  source_chat_id
  owner             me | them
  status            open | closed
  summary
  evidence_message_ids
  due_at
  resurface_at
  revision
```

The LLM emits an observation.

Deterministic domain reconciliation owns business state.

The same model can serve adults and children.

---

## 10. Safety domain

Safety needs a separate domain model. It must not be squeezed into WaitingForMe.

### 10.1 RiskObservation

Immutable output of one analyzer run.

```text
RiskObservation
  id
  subject_user_id
  chat_id
  target_version
  category
  severity
  confidence
  summary
  signals
  evidence_message_ids
  model
  prompt_version
  analyzer_version
  created_at
```

An observation is evidence from a specific conversation version, not long-lived truth.

### 10.2 RiskEpisode

Persistent domain state representing an ongoing risk pattern.

```text
RiskEpisode
  id
  subject_user_id
  source_chat_id
  category
  status              open | resolved
  severity
  summary
  first_observed_at
  last_observed_at
  last_observation_id
  revision
  resolved_at
```

A conversation may have multiple safety episodes because different risks can coexist.

### 10.3 SafetyAlert

What was actually surfaced to a guardian.

```text
SafetyAlert
  id
  risk_episode_id
  guardian_user_id
  severity
  summary
  reason
  delivered_at
  acknowledged_at
  feedback
```

The alert is a derived product artifact, not a copy of the conversation.

---

## 11. Safety categories — MVP

Start narrow.

### 11.1 Bullying / exclusion / harassment

Signals may include:
- repeated insults;
- coordinated exclusion;
- humiliation;
- threats;
- repeated unwanted contact;
- abrupt exclusion from a group when contextual evidence supports it.

### 11.2 Unknown-contact / grooming

Do not alert merely because a number is new.

Potential signals:
- new / unknown contact;
- repeated unanswered messages;
- secrecy request;
- personal-information request;
- location request;
- image request;
- move-to-another-platform request;
- meeting request;
- age / identity inconsistency;
- escalation over time.

The interesting unit is the **pattern**, not one keyword.

### 11.3 Distress / self-harm

Signals may include:
- persistent hopelessness;
- self-harm references;
- suicidal ideation;
- rapid escalation in distress language;
- repeated high-risk statements over time.

High-stakes categories require conservative claims and careful evaluation.

---

## 12. Safety analyzer architecture

The LLM should not directly decide "send a parent alert".

Target flow:

```text
conversation snapshot
        ↓
safety detector
        ↓
structured RiskObservation
        ↓
verifier / second-pass judge
        ↓
deterministic alert policy
        ↓
RiskEpisode reconciliation
        ↓
guardian alert
```

Possible structured detector output:

```json
{
  "category": "grooming",
  "severity": "high",
  "signals": [
    "unknown_contact",
    "request_for_secrecy",
    "request_for_location"
  ],
  "summary": "An unknown contact is escalating personal questions and asking for secrecy.",
  "confidence": 0.94
}
```

The alert policy owns thresholds and category-specific rules.

---

## 13. Longitudinal risk reasoning

The differentiator should not be "LLM moderation on each message".

Echo should maintain derived state across time.

Example:

```text
Message 1: "היי"
→ no alert

Message 2: "למה את לא עונה?"
→ weak persistence signal

Message 3: "איפה את גרה?"
→ personal/location request

Message 4: "אל תספרי להורים"
→ secrecy + escalation

Combined episode:
UNKNOWN_CONTACT + PERSISTENCE + LOCATION + SECRECY
→ high-risk grooming alert
```

No single message needs to be independently alarming.

This is where Echo's existing conversation-state architecture becomes useful.

---

## 14. Privacy architecture

Adult Echo and child safety should have different retention policies.

### Adult mode

Keep current Echo behavior initially.

### Child safety mode

Target architecture:

> **Process content, retain derived safety state.**

The system may need message content transiently to understand context, but raw child conversation data should not become a permanent parental archive.

Target flow:

```text
raw message
   ↓
short-lived processing / conversation window
   ↓
derived signals + observation + episode
   ↓
raw-content retention policy
```

Key principles:
- no parent chat-search feature;
- no "show me all my child's messages";
- minimal raw retention compatible with analysis/retry;
- explicit retention policy by account/profile mode;
- derived risk state retained longer than raw content;
- evidence access tightly controlled and auditable;
- no training on child content without an explicit future policy and consent model.

Exact retention durations should be decided with legal/privacy review before a real child pilot.

---

## 15. Consent and onboarding

The child must not be tricked into installing a hidden monitor.

Target flow:

```text
Guardian creates family
→ invites child
→ child meets Echo as a useful assistant
→ child sees exactly what Safety Mode monitors
→ child consents
→ child connects WhatsApp
→ personal Echo features activate
→ Safety Mode activates
→ guardian receives only allowed alerts
```

The connection step can reuse existing WhatsApp onboarding infrastructure.

The new work is product consent and relationship state, not a second connection stack.

---

## 16. Parent alert policy

Not every observation becomes an alert.

The policy can consider:

```text
category
severity
confidence
verifier result
episode history
signal combination
time persistence
previous alerts
guardian preferences
```

Example:

```text
unknown contact only
→ no alert

unknown contact + repeated contact
→ observe

unknown contact + secrecy request
→ alert

unknown contact + sexual request
→ urgent alert
```

This policy should be deterministic and testable.

---

## 17. Evaluation

Safety makes evaluation a first-class product subsystem.

Reuse all existing Echo evaluation infrastructure:
- versioned prompts;
- analyzer versions;
- real production snapshots;
- LLM judge;
- judge-disagreement annotation queue;
- user/guardian false-positive feedback;
- offline suites;
- hard contrast cases.

New safety datasets should be organized by **scenario families**, not isolated toxic messages.

Initial families:
- friendly unknown contact vs suspicious persistence;
- joke vs bullying;
- argument vs coordinated exclusion;
- normal intimacy vs coercion;
- ordinary sadness vs persistent distress;
- trusted adult vs identity inconsistency;
- legitimate photo request vs sexual escalation;
- normal move-to-platform vs secrecy + move-to-platform.

Metrics should be category-specific.

For high-severity categories track:
- precision;
- recall;
- false negatives;
- alert rate per child/week;
- repeated false-alert rate;
- time-to-detection.

---

## 18. Feedback flywheel

Guardian feedback:

```text
accurate
not accurate
not concerning
already handled
too late
```

Child feedback can also matter:

```text
this is harmless
this contact is known
this alert misunderstood context
```

Durable product feedback should remain in Postgres.

LangSmith annotation queues remain best-effort projections for review.

---

## 19. Existing Echo infrastructure to reuse

### Reuse unchanged initially

- provider-neutral event adapters;
- Green webhook authentication;
- message deduplication;
- message persistence;
- quiet-period scheduling;
- activity_version;
- conversation snapshot building;
- Modal transcription;
- media preparation;
- runtime executor;
- idempotency store;
- scheduler;
- 360dialog bot;
- webhook inbox;
- encrypted provider credentials;
- observability primitives.

### Extend

- user/family identity;
- analysis orchestration;
- analyzer-specific cursors;
- domain models;
- feedback;
- mini-app surfaces;
- dashboards;
- retention policies.

### Do not fork into a second backend

Adult Echo, Echo Kids, and Parent Safety should share one codebase and one foundation.

---

## 20. Roadmap

### Phase 0 — Freeze and document baseline

Goal: preserve the mature adult Echo system before expansion.

- archive/tag current main;
- document current architecture;
- introduce this v3 roadmap;
- no feature deletion.

### Phase 1 — Family identity + child onboarding

Goal: make parent and child first-class Echo identities.

- `families`;
- `family_memberships`;
- guardian/child relationship;
- invitation flow;
- consent state;
- child connects own WhatsApp;
- reuse current Echo utility features.

Exit criteria:
- parent and child are linked;
- child can use existing Echo features;
- no safety detection yet.

### Phase 2 — Safety MVP

Goal: detect three risk families from child conversations.

- RiskObservation;
- RiskEpisode;
- SafetyAlert;
- safety analyzer v1;
- verifier;
- deterministic alert policy;
- guardian delivery;
- accurate / false-alarm feedback.

Risk families:
1. bullying / harassment;
2. unknown-contact / grooming;
3. distress / self-harm.

Exit criteria:
- real end-to-end alert from a labeled test conversation;
- no raw parent chat browser;
- all alerts traceable to analyzer/model/version.

### Phase 3 — Multi-analyzer orchestration

Goal: run responsibility + safety reliably on the same child stream.

- per-analyzer cursors;
- analyzer registry/pipeline;
- independent retry;
- version-fenced commits;
- shadow analyzer support.

Exit criteria:
- responsibility failure does not block safety;
- safety failure does not block responsibility.

### Phase 4 — Privacy hardening

Goal: minimize child raw-content retention.

- profile-specific retention policy;
- derived safety memory;
- raw message cleanup;
- auditability;
- parent data-access boundaries;
- legal/privacy review.

### Phase 5 — Longitudinal behavioral signals

Goal: go beyond message moderation.

- unknown/new contact state;
- repeated unanswered contact;
- secrecy;
- personal-info/location requests;
- platform migration;
- meeting requests;
- group exclusion;
- deletion patterns where observable;
- risk escalation across time.

### Phase 6 — Pilot

Goal: 5–10 families.

Measure:
- onboarding completion;
- child consent completion;
- connection retention;
- alerts / child / week;
- false-positive rate;
- parent trust;
- child trust;
- disconnect rate;
- daily/weekly usage of Echo Kids utility.

### Phase 7 — Family plan / adult expansion

Goal: convert trust built through child safety into broader Echo adoption.

Potential packaging:

```text
Echo Kids
Parent Safety
Echo Adult
Family Plan
```

The parent can later activate the existing Adult Echo product on their own WhatsApp.

---

## 21. What we are explicitly not doing now

- rewriting the Green integration;
- deleting adult Echo;
- building a general parental-control suite;
- GPS/location tracking;
- screen-time management;
- app blocking;
- reading every child message in a parent dashboard;
- trying to support every risk category at launch;
- adding hardware;
- training custom models before we have data;
- creating a second backend for the child product.

---

## 22. Architectural north star

Echo should become a platform for **derived intelligence over private conversational streams**.

The provider stream is canonical input.

Product features are derived views:

```text
WhatsApp conversation stream
          ↓
stable snapshots + derived state
          ↓
 ┌────────┼────────┬─────────┐
 ▼        ▼        ▼         ▼
tasks   followup   safety   future
          ↓
actions / alerts / reminders
          ↓
feedback
          ↓
evaluation
```

The model interprets language.

Deterministic code owns state, policy, execution, and permissions.

That principle remains the same across Adult Echo, Echo Kids, and Parent Safety.
