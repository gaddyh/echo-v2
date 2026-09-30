# Guard Analysis and Evaluation Model

## Purpose

Echo Guard is a safety layer for a child's WhatsApp conversations. Its job is not to judge every awkward message, argument, or joke. Its job is to recognize when a conversation develops into a pattern that may require attention.

The analyzer answers four separate questions:

1. **What signals are visible in the conversation?**
2. **What kind of safety situation do those signals describe?**
3. **How serious is the situation at this point in time?**
4. **Which messages support that assessment?**

The analyzer does **not** decide whether a parent receives an alert. That is a separate product decision made by `AlertPolicy`.

```text
Conversation prefix
        ↓
GuardAnalyzer
        ↓
GuardAnalysis: decision + categories + signals + evidence
        ↓
AlertPolicy
        ↓
No action / save observation / parent alert
```

## Initial Product Scope

We begin with six situations: three that are most relevant in a private one-to-one chat, and three that are most relevant in a group chat.

### Private chats

| Product situation | Category | What it means |
| --- | --- | --- |
| Suspicious contact | `suspicious_contact` | An unfamiliar or concerning person attempts to learn about, isolate, or meet the child. |
| Harassment or coercion | `harassment_or_coercion` | The child is pressured, repeatedly contacted, threatened, or blackmailed. |
| Distress | `distress` | The child expresses fear, hopelessness, a need for help, or potential self-harm. |

### Group chats

| Product situation | Category | What it means |
| --- | --- | --- |
| Bullying | `bullying` | Repeated targeting, humiliation, or pile-on behavior toward the child. |
| Social exclusion | `social_exclusion` | Deliberate rejection or coordinated exclusion of the child. |
| Harmful sharing | `harmful_sharing` | Harmful, embarrassing, or intimate material is shared, or there is a threat to share it. |

These are product categories, not mutually exclusive diagnoses. A group conversation can contain both `bullying` and `social_exclusion`.

## Taxonomy

### Signals

Signals are concrete observations from the conversation. They are cumulative across the conversation prefix: a signal remains present in later snapshots unless later messages clearly disprove it.

| Category | Supporting signals |
| --- | --- |
| `suspicious_contact` | `offline_knowledge`, `personal_information_request`, `routine_probing`, `location_request`, `secrecy_request`, `meeting_request` |
| `harassment_or_coercion` | `repeated_unwanted_contact`, `boundary_violation`, `threat`, `coercive_demand`, `blackmail_or_extortion` |
| `distress` | `help_request`, `fear_expression`, `hopelessness`, `self_harm_expression` |
| `bullying` | `repeated_targeting`, `insult_or_humiliation`, `group_pile_on` |
| `social_exclusion` | `exclusion`, `coordinated_exclusion` |
| `harmful_sharing` | `harmful_content_sharing`, `threat_to_share` |

The table defines the primary relationship between categories and signals. It should be metadata in the domain code, alongside the enums, rather than duplicated in the database.

```python
CATEGORY_SIGNALS: dict[GuardCategory, frozenset[GuardSignal]] = {
    GuardCategory.SUSPICIOUS_CONTACT: frozenset({
        GuardSignal.OFFLINE_KNOWLEDGE,
        GuardSignal.PERSONAL_INFORMATION_REQUEST,
        GuardSignal.ROUTINE_PROBING,
        GuardSignal.LOCATION_REQUEST,
        GuardSignal.SECRECY_REQUEST,
        GuardSignal.MEETING_REQUEST,
    }),
    # ... remaining categories
}
```

The model returns both categories and signals. We do not derive the category deterministically from signals alone: the same signal can mean different things depending on who said it, the conversation type, and the surrounding pattern. The system can validate that returned categories are supported by the returned signals.

### Decisions

`decision` describes the seriousness of the conversation **at this snapshot**. It is not a fixed property of a category or a single signal.

| Decision | Meaning |
| --- | --- |
| `none` | No meaningful safety concern. |
| `watch` | One early, mild, or ambiguous signal exists. Monitor; the pattern is not established. |
| `concerning` | A meaningful pattern is established, but there is no clear immediate danger. |
| `urgent` | There is immediate or imminent safety risk. |

Examples of an urgent pattern include a credible immediate threat, active blackmail, an imminent meeting with a suspicious contact, or immediate self-harm risk. The exact decision always depends on the combined evidence and context.

### Analysis contract

```python
GuardAnalysis(
    decision="none | watch | concerning | urgent",
    categories=(...),
    signals=(...),
    evidence_message_ids=(...),
    confidence=...,
    reason=...,
)
```

Field contract:

- `categories`: zero or more applicable product categories.
- `signals`: the established signals across the full prefix, not only the most recent message.
- `evidence_message_ids`: only messages that substantiate the analysis.
- `confidence`: confidence in the analysis, not a severity score.
- `reason`: short internal explanation connecting evidence to the assessment; not parent-facing copy.

For `decision="none"`, all three evidence fields are empty:

```python
GuardAnalysis(
    decision="none",
    categories=(),
    signals=(),
    evidence_message_ids=(),
    confidence=0.95,
    reason="No meaningful safety concern in the conversation.",
)
```

Initial invariants:

1. `decision="none"` requires empty categories, signals, and evidence IDs.
2. Any non-`none` decision requires at least one evidence message ID.
3. Every returned category must be supported by at least one compatible signal.

## Alerting is a Separate Policy

The analyzer detects and assesses. `AlertPolicy` decides what to do with that assessment.

```text
decision + categories + signals + child/context settings
                         ↓
                    AlertPolicy
                         ↓
              save / alert / suppress / escalate
```

This separation is deliberate. A `concerning` assessment may be saved and monitored without immediately alerting a parent. Product policy can later account for prior alerts, sensitivity settings, quiet hours, relationship context, or escalation over time without changing the analyzer prompt.

## Dataset and Snapshot Evaluation

The unit of evaluation is not only a complete conversation. It is a **conversation prefix at a meaningful moment**.

Each evaluation case contains an ordered message list and expected snapshots:

```python
GuardEvalCase(
    case_id="...",
    family="...",
    messages=(...),
    snapshots=(
        ExpectedSnapshot(after_message_id="m2", ...),
        ExpectedSnapshot(after_message_id="m5", ...),
        ExpectedSnapshot(after_message_id="m8", ...),
    ),
)
```

At each snapshot, the evaluator sends the analyzer only the prefix up to `after_message_id`, then compares the result with the expected analysis.

### What each snapshot evaluates

Each expected snapshot should specify only stable semantic expectations:

```python
ExpectedSnapshot(
    after_message_id="m5",
    acceptable_decisions=("concerning",),
    required_categories=("suspicious_contact",),
    required_signals=("location_request", "routine_probing"),
    forbidden_signals=(),
)
```

We evaluate:

- decision is one of the acceptable decisions;
- required categories are present;
- required signals are present;
- forbidden signals are absent;
- all returned evidence IDs belong to the prefix;
- evidence exists for a non-`none` decision.

We intentionally do **not** evaluate exact confidence values, exact reason text, or exact category ordering. Those would make the evaluation brittle without improving product safety.

## State Across Snapshots

The production analyzer may be invoked repeatedly as new messages arrive. The evaluator therefore treats every snapshot as an independent analysis of the full prefix and compares it with the previous expected state.

The expected state is cumulative:

```text
Earlier established signal
        +
New message creates another signal
        ↓
Later snapshot contains both signals
```

The evaluator should record a run timeline for each case:

| Snapshot | Prefix ends at | Decision | Categories | Signals | Evidence IDs |
| --- | --- | --- | --- | --- | --- |
| 1 | `m2` | `none` | — | — | — |
| 2 | `m3` | `watch` | `suspicious_contact` | `offline_knowledge` | `m3` |
| 3 | `m5` | `concerning` | `suspicious_contact` | `offline_knowledge`, `location_request`, `routine_probing` | `m3`, `m5` |
| 4 | `m7` | `concerning` | `suspicious_contact` | previous signals + `secrecy_request` | `m3`, `m5`, `m7` |
| 5 | `m8` | `urgent` | `suspicious_contact` | previous signals + `meeting_request` | `m3`, `m5`, `m7`, `m8` |

This timeline shows whether the detector sees the pattern early enough and whether it retains established evidence rather than replacing it with the latest message.

## Escalation and Non-Escalation

An evaluation family should contain more than one case. It should include positive, negative, and borderline variants.

### Escalation example: suspicious contact

```text
unknown greeting
    → none

demonstrates offline knowledge
    → watch

probes the child's routine or location
    → concerning

asks for secrecy and a meeting
    → urgent
```

The key checks are:

- no alert-level interpretation at an ordinary greeting;
- early detection once the pattern starts;
- retention of earlier signals;
- escalation when the pattern becomes imminent.

### Escalation example: group bullying

```text
one insulting message, child participates playfully
    → none

repeated targeting after the child asks them to stop
    → watch

multiple participants join the humiliation
    → concerning

credible immediate threat or active harmful sharing
    → urgent
```

### Non-escalation example: teasing between friends

```text
mutual joking and reciprocal teasing
    → none

another joking message with the same reciprocal context
    → none
```

The system must not escalate merely because a conversation contains a rude word, an argument, a single insult, or a message that sounds concerning in isolation. It should escalate only when the full prefix establishes a safety pattern.

## Evaluation Principles

1. **Prefix over final label.** Measure the moment at which the system should recognize a developing pattern.
2. **Patterns over keywords.** A word alone is usually not enough; sequence, role, repetition, and response matter.
3. **Cumulative evidence.** Later outputs retain established signals unless clearly disproven.
4. **Contrast pairs.** Every positive family needs an ordinary or benign sibling that looks superficially similar.
5. **Separate detection from policy.** Analyzer evaluation measures understanding and severity; policy evaluation separately measures whether the product alerts.
6. **Version every run.** Record prompt version, taxonomy version, model, and evaluation dataset version with results.

## Initial Evaluation Families

The first six families should match the initial scope:

```text
private_unknown_contact_escalation
private_harassment_or_coercion
private_distress_vs_normal_sadness
group_teasing_vs_bullying
group_social_exclusion
group_harmful_sharing
```

Each family should grow into a set of contrast cases and snapshots before the next category is added.
