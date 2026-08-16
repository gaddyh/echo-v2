# Echo MVP — High-Level Design & Roadmap

## 1. Product

### Echo

**If WhatsApp is your office, Echo is your secretary.**

Echo is a WhatsApp-based assistant for people whose work happens inside WhatsApp.

It connects to the user's existing WhatsApp account and helps make sure important communication does not fall through the cracks.

The user communicates with Echo through a dedicated Echo WhatsApp Business number. Echo observes and acts on the user's own WhatsApp account through Green API.

The MVP is **not** a general-purpose AI assistant, CRM, calendar assistant, or task manager.

The product promise is narrower:

> **Echo remembers what needs to happen next in your WhatsApp conversations.**

---

## 2. MVP Goal

The MVP should prove that Echo can become a trusted memory and follow-up layer above WhatsApp.

The three product capabilities are:

1. **Waiting for me** — detect conversations where someone is waiting for the user to reply.
2. **Waiting for them** — track conversations where the user expects a reply and surface them if no reply arrives.
3. **Scheduled** — send WhatsApp messages later, reliably, from the user's own number.

These three capabilities should form the product's core mental model:

```text
מחכים לי
אני מחכה להם
מתוזמן
```

The primary product question is:

> **Will users trust and repeatedly use Echo because it prevents important WhatsApp conversations and follow-ups from being forgotten?**

A strong MVP should create recurring value even when the user does not explicitly ask Echo to do something.

---

## 3. Target User

Initial target:

**Israeli solo professionals who conduct a meaningful part of their work through WhatsApp and do not have another person managing follow-up for them.**

Examples:

- freelancers
- therapists
- consultants
- real-estate agents
- beauty professionals
- technicians
- coaches
- lawyers
- mortgage advisers
- photographers
- independent service providers

The common factor is not profession.

The common factor is:

> **The business lives in WhatsApp, and the owner must personally remember who needs a reply, who owes them a reply, and what needs to be sent later.**

The architecture should remain horizontal and business-agnostic so the same product can later support regular consumers.

---

# 4. MVP Product Features

## 4.1 Waiting for Me

Echo automatically detects conversations where another person sent the latest relevant message and the user has not replied within a configured period.

Example:

```text
10:03 Dana → User
"יש מצב לחמישי?"

12:03
No outgoing reply from User
```

Conversation state:

```text
WAITING_FOR_ME
waiting_since = 10:03
```

Echo may notify the user through the Echo Business Bot:

> 🔴 דנה מחכה לתשובה כבר שעתיים  
> "יש מצב לחמישי?"

When the user replies to Dana from normal WhatsApp, Green API sees the outgoing event and the conversation is no longer waiting for the user.

### MVP behavior

Initial detection should be deterministic:

```text
latest relevant message = inbound
AND age > configured threshold
→ WAITING_FOR_ME
```

No LLM is required for the basic state transition.

The MVP should support:

- incoming/outgoing message tracking;
- configurable waiting threshold;
- quiet hours;
- notification deduplication;
- snooze / ignore;
- automatic resolution when the user replies.

Later versions may add importance classification and smarter filtering.

---

## 4.2 Waiting for Them

Echo tracks conversations where the user expects something back from another person.

The first MVP version should be **explicit**, not inferred from every outgoing message.

Examples:

> "אם דני לא חוזר אליי עד מחר בצהריים תזכיר לי."

> "אני מחכה לתשובה מרונית על ההצעה."

Echo creates a follow-up:

```text
FollowUp

user_id
chat_id
created_at
deadline
condition = NO_REPLY
action = REMIND_ME
status = ACTIVE
```

If the other person replies before the deadline:

```text
incoming message
→ FollowUp satisfied
→ scheduled reminder cancelled
```

If no reply arrives:

```text
deadline reached
→ Echo Business Bot
→ "רונית עדיין לא חזרה אליך"
```

### MVP behavior

Support:

- create an explicit "waiting for them" follow-up;
- tie it to a specific WhatsApp conversation;
- define deadline / reminder time;
- cancel automatically when a qualifying incoming reply arrives;
- list active follow-ups;
- cancel manually.

### Not in initial MVP

Do **not** assume:

```text
latest message = outgoing
→ WAITING_FOR_THEM
```

Many outgoing messages do not expect a response.

Automatic `expects_reply` detection should be added later, once we have real data and an evaluation set.

---

## 4.3 Scheduled Messages

The user can tell Echo now what should be sent later.

Examples:

> Send Daniel tomorrow morning: "Did you get the proposal?"

> Send Sarah at 9 AM New York time: "Good luck today!"

> שלח ליוסי ביום ראשון ב-10: "תזכורת לגבי הפגישה שלנו"

The system resolves:

- recipient;
- message;
- date;
- local time;
- timezone.

All scheduled times are persisted internally in UTC together with the source timezone.

### Execution

```text
User command
    ↓
command interpretation
    ↓
recipient resolution
    ↓
confirmation if needed
    ↓
ScheduledAction
    ↓
persistent scheduler
    ↓
runtime.execute()
    ↓
Green API
    ↓
recipient
```

A scheduled message must be idempotent.

A retry, webhook duplicate, or worker restart must **never cause the same WhatsApp message to be sent twice**.

The MVP should support:

- natural-language scheduling;
- absolute and relative times;
- timezone handling;
- contact resolution;
- confirmation when recipient/time/message is ambiguous;
- list pending scheduled messages;
- cancel scheduled messages;
- reliable delivery tracking.

---

# 5. Supporting Capabilities

These are important, but they are **not separate top-level MVP product pillars**.

They exist to make the three core features work well.

## 5.1 Direct Send

Echo can send an immediate message from the user's own WhatsApp account.

Example:

> Send Mom: "We are a bit late, sorry."

This is a supporting primitive for scheduled messages and future follow-up actions.

Flow:

```text
User → Echo Business Bot
    ↓
interpret command
    ↓
resolve recipient
    ↓
confirm if needed
    ↓
runtime.execute()
    ↓
Green API
    ↓
recipient
```

The MVP may expose direct send, but it should not become the product's primary positioning.

---

## 5.2 Self Reminders

Echo can create reminders to the user.

Example:

> Remind me tomorrow at 10 to check whether Moshe replied.

A reminder is delivered through the Echo Business Bot:

```text
Echo → User
```

A scheduled WhatsApp message is delivered from the user's own account:

```text
User's WhatsApp → Recipient
```

Both should use the same persistent scheduling infrastructure.

Self-reminders are primarily a building block for **Waiting for Them**.

---

## 5.3 Voice Support

Voice is a first-class input modality and should be added early.

Example:

> "אקו, אם רונית לא חוזרת אליי עד מחר בערב תזכיר לי."

Flow:

```text
voice message
    ↓
media download
    ↓
TranscriptionService
    ↓
text
    ↓
same command pipeline as typed input
```

There should be no separate business logic for voice.

Both:

```text
typed text
```

and:

```text
voice → transcription
```

enter the same command-processing pipeline.

---

## 5.4 Contact Resolution

Echo must maintain a reliable mapping between natural-language names and WhatsApp chat IDs.

Example:

```text
"דני"
 ↓
ContactService
 ↓
Danny Cohen
Danny Levi
```

Echo asks the user which one.

No AI component may invent or guess a WhatsApp recipient.

---

# 6. Post-MVP Features

These remain valuable, but should not delay validation of the core three.

## 6.1 Chat Search and Summarization

Examples:

> What did Yossi say about the price?

> Summarize my conversation with Ron from this week.

> סכם את קבוצת ועד הבית מהיום.

Search should be scoped before content is sent to an LLM:

```text
specific chat
→ relevant time range
→ relevant messages
→ AI
```

rather than:

```text
entire WhatsApp history
→ AI
```

This improves privacy, cost, latency, and quality.

---

## 6.2 Google Calendar

The existing Echo Calendar integration should be preserved during migration, but it is not required for the first MVP.

Potential future commands:

> What's on my calendar tomorrow?

> Remind me an hour before my meeting with Avi.

> Schedule a meeting with Ron for Thursday at 10.

Calendar actions should use the same structured-command, runtime, idempotency, and observability principles as WhatsApp operations.

---

## 6.3 Automatic "Waiting for Them" Detection

Later, Echo may infer when an outgoing message expects a response.

Example:

> "שלחתי לך את ההצעה, אשמח לשמוע מה דעתך."

Potential structured result:

```text
expects_reply = true
reason = proposal
```

This should only be introduced after we have:

- real production examples;
- a labeled dataset;
- offline evaluation;
- acceptable false-positive rates.

False positives here directly create notification fatigue, so precision matters more than coverage.

---

# 7. High-Level Architecture

```text
                             USER
                               │
                  ┌────────────┴────────────┐
                  │                         │
                  ▼                         ▼
         Echo Business Bot          User's WhatsApp
             360dialog                 Green API
                  │                         │
                  │                         │
                  └────────────┬────────────┘
                               │
                               ▼
                          Echo Backend
                               │
            ┌──────────────────┼──────────────────┐
            │                  │                  │
            ▼                  ▼                  ▼
          Domain            Services            Runtime
                                                   │
                                      retry / timeout
                                      idempotency
                                      events / errors
                                                   │
                         ┌─────────────────────────┼──────────────┐
                         ▼                         ▼              ▼
                     Green API                 360dialog      Google
                                                              Calendar*
```

`*` Calendar is preserved as an integration but is post-MVP.

The architecture is built around two WhatsApp identities:

```text
Echo Business Bot
= communication between Echo and the user

Green API
= observation and actions on the user's own WhatsApp
```

---

# 8. Core Domain

The domain should contain business concepts only and should not depend on Green API, 360dialog, OpenAI, databases, or FastAPI.

Initial domain objects:

```text
User
Contact
Conversation
MessageEvent
ConversationState
ScheduledAction
FollowUp
Reminder
```

---

## 8.1 MessageEvent

Normalized representation of a WhatsApp message.

```text
MessageEvent

id
user_id
chat_id
direction        inbound | outbound
sender_id
timestamp
message_type
text
provider_message_id
```

Green API-specific webhook payloads should be converted into this format at the integration boundary.

The rest of Echo should never need to understand Green API webhook JSON.

---

## 8.2 ConversationState

Represents the operational state of a WhatsApp conversation.

Initial states:

```text
IDLE
WAITING_FOR_ME
WAITING_FOR_THEM
```

Possible fields:

```text
ConversationState

user_id
chat_id
state
last_inbound_at
last_outbound_at
waiting_since
active_followup_id
updated_at
```

Basic `WAITING_FOR_ME` state transitions should be deterministic.

`WAITING_FOR_THEM` is driven by an explicit active FollowUp in the initial MVP.

---

## 8.3 FollowUp

Represents something the user expects from another conversation participant.

```text
FollowUp

id
user_id
chat_id
created_at
deadline
condition
action
status
resolved_at
```

Initial condition:

```text
NO_REPLY
```

Initial action:

```text
REMIND_ME
```

Later:

```text
SEND_MESSAGE
ASK_USER
ESCALATE
```

---

## 8.4 ScheduledAction

Generic persistent future action:

```text
ScheduledAction

id
user_id
type
execute_at_utc
timezone
status
payload
created_at
executed_at
```

Initial action types:

```text
SEND_WHATSAPP_MESSAGE
SEND_REMINDER
```

Later:

```text
FOLLOW_UP_ACTION
CALENDAR_ACTION
CONDITIONAL_ACTION
```

---

# 9. Services

## 9.1 ConversationService

Owns conversation state transitions.

Responsibilities:

- ingest normalized MessageEvents;
- update `last_inbound_at` / `last_outbound_at`;
- determine `WAITING_FOR_ME`;
- resolve `WAITING_FOR_ME` after user reply;
- connect incoming replies to active FollowUps;
- expose current state for summaries and notifications.

This service should be deterministic wherever possible.

---

## 9.2 FollowUpService

Responsibilities:

- create explicit follow-ups;
- bind them to a chat;
- define deadline and condition;
- resolve them when a qualifying reply arrives;
- trigger reminder action if deadline is reached;
- list/cancel active follow-ups.

---

## 9.3 ContactService

Responsibilities:

- retrieve WhatsApp contacts;
- maintain a local searchable contact index;
- resolve natural-language names;
- return ambiguity rather than guessing.

---

## 9.4 MessagingService

Provider-neutral interface:

```text
send_message()
get_messages()
```

Initial implementation:

```text
GreenMessagingService
```

This allows Green API to be replaced later without changing the domain.

Potential future implementations:

```text
MetaCoexistenceMessagingService
MetaPersonalMessagingService
```

---

## 9.5 SchedulingService

Responsibilities:

- create scheduled actions;
- query due actions;
- execute them;
- update state;
- recover after restart;
- prevent duplicate execution.

The scheduler must use persistent storage.

An in-memory timer is not sufficient.

---

## 9.6 ReminderService

Creates and manages self-reminders.

It uses the same scheduling infrastructure but delivers through the Echo Business Bot.

---

## 9.7 TranscriptionService

Interface:

```text
transcribe(audio) → text
```

The initial implementation can reuse working transcription code from the existing Echo project.

The provider should remain replaceable.

---

# 10. AI Layer

AI should **interpret language**, not own execution or conversation state.

Example:

```text
User:
"אם רונית לא חוזרת אליי עד מחר בערב תזכיר לי"
```

AI produces structured output:

```text
intent: create_followup
recipient_name: "רונית"
condition: no_reply
deadline: tomorrow evening
action: remind_me
```

Another example:

```text
User:
"שלח לאבי מחר בבוקר שאני אגיע ב-11"
```

AI produces:

```text
intent: schedule_message
recipient_name: "אבי"
message: "אני אגיע ב-11"
date: tomorrow
time: morning
timezone: user_default
```

Then deterministic services take over:

```text
AI
 ↓
structured command
 ↓
validation
 ↓
domain/service
 ↓
runtime
 ↓
external API
```

The LLM should never directly:

- send a message;
- execute an external operation;
- choose an ambiguous contact;
- determine whether an operation already ran;
- own `WAITING_FOR_ME` state;
- cancel a follow-up without deterministic evidence.

---

# 11. Runtime Foundation

Reuse the generic design developed in `naot-poc`'s `foundation-v0.1.0`.

Every important external operation should execute through:

```text
runtime.execute()
```

Runtime responsibilities:

```text
timeout
retry
error classification
run_id
event emission
idempotency
duration
attempt tracking
```

Examples:

```text
Green API send
360dialog notification
transcription call
calendar call
```

For scheduled sends:

```text
execute(
    operation=green_api.send_message,
    input=message,
    context=RunContext(...),
    policy=GREEN_SEND_POLICY,
    idempotency_key=scheduled_action.id
)
```

### Required guarantee

If Echo is uncertain whether a scheduled message has already been executed:

> **Never blindly send again.**

Duplicate WhatsApp messages are worse than delayed messages.

---

# 12. Integrations

## 12.1 Green API

Used as a linked device for the user's WhatsApp account.

MVP usage:

- onboarding / QR;
- contact retrieval;
- message sending;
- incoming/outgoing message events;
- enough chat metadata/history to maintain conversation state.

Green API must remain behind an interface because it is a replaceable transport and represents platform risk.

---

## 12.2 360dialog / Meta Cloud API

Used for the permanent Echo Business Bot.

Responsibilities:

```text
User → Echo
Echo → User
voice messages
commands
notifications
reminders
confirmation flows
```

This is separate from the user's Green API connection.

---

## 12.3 Google Calendar

Preserve the existing integration, but do not make it part of the first MVP milestone.

---

# 13. Persistence

Initial logical collections/tables:

```text
users
whatsapp_connections
contacts
conversations
message_events
conversation_states
followups
scheduled_actions
deliveries
reminders
```

Important identifiers:

```text
user_id
provider_message_id
chat_id
followup_id
scheduled_action_id
idempotency_key
```

Raw provider payloads may optionally be retained for debugging with a limited retention policy.

---

# 14. Observability

Reuse the event-based observability model from the Naot foundation.

Examples:

```text
whatsapp.message.received
conversation.waiting_for_me.started
conversation.waiting_for_me.resolved

followup.created
followup.resolved_by_reply
followup.deadline_reached

scheduled_action.created
scheduled_action.started
scheduled_action.completed
scheduled_action.failed

message.send.started
message.send.retrying
message.send.completed

notification.sent

command.interpreted

transcription.started
transcription.completed
```

Each operation should include appropriate metadata such as:

```text
run_id
user_id
chat_id
operation_name
duration_ms
attempts
status
```

Sensitive message contents should not be included in normal operational logs.

### Product metric to design for

A useful future metric:

> **How many important things did Echo prevent from being forgotten for this user this week?**

This can become a core retention and value metric.

---

# 15. LangSmith / Evaluation

Evaluation should exist from the beginning, but only where nondeterministic behavior exists.

## 15.1 Deterministic tests

Conversation-state behavior should use normal unit/regression tests.

Examples:

```text
incoming message
→ WAITING_FOR_ME
```

```text
incoming message
→ WAITING_FOR_ME
→ user replies
→ IDLE
```

```text
active FollowUp
→ incoming reply
→ FollowUp resolved
```

```text
scheduled action executed
→ worker sees it again
→ no duplicate send
```

---

## 15.2 AI evaluation

Use the Naot-style model:

```text
dataset
 ↓
target
 ↓
evaluator
 ↓
experiment / regression
```

Initial datasets:

### Scheduled-message interpretation

```text
input:
"תשלח לרותי מחר בשמונה שאני אגיע מאוחר"

expected:
intent = schedule_message
recipient = רותי
date/time correctly extracted
message correctly separated
```

### Follow-up interpretation

```text
input:
"אם יוסי לא חוזר אליי עד יום רביעי בצהריים תזכיר לי"

expected:
intent = create_followup
recipient = יוסי
condition = no_reply
deadline = Wednesday 12:00
action = remind_me
```

### Contact ambiguity

Echo must not guess between two similar contacts.

### Voice

Transcribed commands should result in the same structured command as equivalent typed input.

### Later: expects-reply classification

Only when automatic `WAITING_FOR_THEM` inference is introduced.

---

# 16. Privacy and Security

Echo has unusually sensitive access.

The MVP should follow these principles:

### Minimize

Process only the message data required for the product behavior.

### Separate

Green API credentials for each user must be isolated.

### Don't log chats

Application logs should contain metadata, not conversation bodies.

### Explicit execution

Sending messages should use confirmation where recipient, time, or content is ambiguous.

### Credentials

Tokens and API credentials must never be stored in source control or normal logs.

### Avoid unnecessary LLM exposure

Deterministic conversation-state tracking should not send chat content to an LLM.

---

# 17. MVP Deployment Model

Initial deployment can remain simple:

```text
FastAPI application
        │
        ├── Echo bot webhook
        ├── Green API webhook
        ├── command processing
        ├── conversation-state processing
        └── scheduler worker

Persistent DB
        │
        ├── users
        ├── conversations
        ├── followups
        ├── scheduled actions
        └── delivery state

External:
Green API
360dialog
LLM
Transcription provider
```

The scheduler may initially run inside the same deployment, provided scheduling is persistent and idempotent.

It can later move to a separate worker without changing the domain model.

---

# 18. Clean Implementation Roadmap

## Step 0 — Foundation / Plumbing

Goal:

> Connect both WhatsApp identities and create a reliable event/execution foundation.

Build:

```text
Echo v2 package
configuration
users
persistent DB
runtime
observability
Green API connection
Green API webhook
Echo Business Bot
normalized MessageEvent
contact sync
```

Required proof:

- connect one user with Green API QR;
- receive incoming messages;
- receive outgoing messages sent normally from the phone;
- normalize both into `MessageEvent`;
- send a notification from Echo Business Bot to the user;
- send a message from the user's WhatsApp through Green API;
- protect external writes with runtime/idempotency.

At the end of Step 0, the platform works but the product does not yet make decisions.

---

## Step 1 — Scheduled

Goal:

> Prove Echo can reliably act later on behalf of the user.

Build:

```text
structured schedule command
recipient resolution
confirmation
ScheduledAction
persistent scheduler
timezone handling
delivery tracking
idempotent Green send
list pending
cancel
```

Primary vertical slice:

```text
User → Echo:
"שלח לדנה מחר ב-8:
אני מגיע ב-10"

        ↓
interpret
        ↓
resolve Dana
        ↓
confirm
        ↓
persist ScheduledAction
        ↓
scheduler
        ↓
runtime.execute()
        ↓
Green API
        ↓
Dana receives message
```

Add voice input near the end of this step:

```text
voice
→ transcription
→ same command path
```

This validates trust-critical execution.

---

## Step 2 — Waiting for Me

Goal:

> Create the first proactive retention feature.

Build:

```text
Conversation
ConversationState
MessageEvent ingestion
last inbound/outbound timestamps
WAITING_FOR_ME state
threshold policy
quiet hours
notification dedup
snooze / ignore
automatic resolution on reply
```

Core rule:

```text
latest relevant message = inbound
AND waiting time > threshold
→ WAITING_FOR_ME
```

User experience:

> 🔴 דנה מחכה לתשובה כבר שעתיים

This step should require no LLM for state detection.

---

## Step 3 — Waiting for Them

Goal:

> Let the user explicitly hand off follow-up memory to Echo.

Build:

```text
FollowUp
create follow-up command
NO_REPLY condition
deadline
REMIND_ME action
automatic resolution by incoming reply
manual cancel
list active follow-ups
```

Example:

```text
User → Echo:
"אם רונית לא חוזרת אליי עד מחר בצהריים תזכיר לי"

        ↓
structured FollowUp
        ↓
watch chat
      /      \
 reply        no reply
   ↓             ↓
resolve       deadline
                 ↓
             Echo reminder
```

Do **not** infer "waiting for them" automatically yet.

---

## Step 4 — Unified MVP Experience

Goal:

> Make the three features feel like one product.

Echo can show or send a digest such as:

```text
בוקר טוב 👋

🔴 4 אנשים מחכים לך
🟡 3 אנשים שאתה מחכה להם
🕐 2 הודעות מתוזמנות להיום
```

The product mental model is now complete:

```text
מחכים לי
אני מחכה להם
מתוזמן
```

This is the first version to validate with a broader set of real solo professionals.

Measure:

- activation;
- scheduled actions created;
- waiting-for-me items surfaced;
- follow-ups created;
- follow-ups resolved;
- notifications acted on;
- weekly retained users;
- number of "saved" conversations/actions per user.

---

## Step 5 — Intelligence and Expansion

Only after usage validates the core loop:

```text
automatic expects_reply detection
priority / importance classification
chat search
chat summarization
calendar integration
conditional auto-send
richer proactive suggestions
```

This is where AI can become more proactive.

It should not be required for the MVP's deterministic core.

---

# 19. Explicit Non-Goals for the First MVP

Not required before validating the three core product capabilities:

- CRM;
- sales pipeline;
- automatic AI replies to customers;
- autonomous agents;
- bulk marketing;
- WhatsApp campaigns;
- team inbox;
- generic task manager;
- complex LangGraph workflows;
- full calendar assistant;
- chat search/summarization;
- automatic `expects_reply` inference;
- automatic conditional follow-up messages.

---

# 20. Code Strategy

Use the existing `gaddyh/echo` repository as the home of Echo v2.

Create a clean new package inside the repository:

```text
src/
  echo/
```

Do not continue extending the old structure directly.

Reuse selectively:

### From old Echo

```text
360dialog plumbing
Green API onboarding
Green API send
contacts
instance management
QR flow
transcription
scheduled-message concepts
calendar integration for later
```

### From `naot-poc` `foundation-v0.1.0`

Reuse or adapt the engineering foundation:

```text
runtime.execute()
execution policies
timeouts
retry model
idempotency
runtime events
event sinks
evaluation structure
```

Do not copy scanner-specific workflow/domain code.

Do not introduce LangGraph only because Naot uses workflows.

Echo should stay deterministic unless orchestration complexity later justifies a graph.

---

# 21. Summary

Echo MVP is **not** a list of assistant features.

It is one coherent product loop:

```text
                 WhatsApp activity
                        │
                        ▼
               Echo conversation memory
                        │
          ┌─────────────┼─────────────┐
          ▼             ▼             ▼
     WAITING FOR ME  WAITING FOR THEM  SCHEDULED
          │             │             │
          └─────────────┼─────────────┘
                        ▼
                 Echo Business Bot
```

Supporting capabilities:

```text
direct send
self reminders
voice
contact resolution
runtime reliability
```

Post-MVP:

```text
search
summarization
calendar
automatic follow-up inference
conditional auto-send
```

The engineering sequence is:

```text
0. Foundation
1. Scheduled
2. Waiting for Me
3. Waiting for Them
4. Unified MVP
5. Intelligence / Expansion
```

The product promise remains:

> **If WhatsApp is your office, Echo is your secretary.**

And the operational value is:

> **Echo remembers what needs to happen next in your WhatsApp conversations.**
