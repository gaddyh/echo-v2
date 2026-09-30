# Guard Evaluation Guide

This directory contains the Guard analyzer evaluation dataset, the
LLM-backed analyzer harness, deterministic alert-policy cases, and recorded
baselines.

The evaluation is intentionally split into layers:

```text
conversation snapshot
        |
        v
GuardAnalyzer (LLM)
        |
        v
GuardAnalysis          <- what is happening now?
        |
        v
GuardSignalLedger      <- cumulative state across snapshots
        |
        v
AlertPolicy            <- should a parent be notified?
```

The LLM interprets conversation language. Deterministic code owns cumulative
state and notification policy.

## Quick start

The Guard eval uses the real LLM API and is excluded from the normal test
suite.

```bash
set -a && . ./.env && set +a
.venv/bin/python -m pytest -m eval_guard -v -s
```

The harness runs the dataset three times by default. Override the number of
runs when needed:

```bash
GUARD_EVAL_RUNS=5 .venv/bin/python -m pytest -m eval_guard -v -s
```

The harness requires `OPENAI_API_KEY`. The model can be overridden with
`GUARD_LLM_MODEL` or `LLM_MODEL_NAME`.

## Current files

| File | Purpose |
|---|---|
| `guard_cases.py` | Conversation fixtures and analyzer gold snapshots |
| `guard_policy_cases.py` | Direct deterministic AlertPolicy fixtures |
| `test_guard_eval.py` | Three-run LLM analyzer harness and aggregate report |
| `guard_eval_results.py` | JSON and Markdown persistence for each run |
| `BASELINE.md` | Current frozen analyzer and policy baseline |
| `../../services/guard_analyzer.py` | LLM analyzer, schema, prompt, and parsing |
| `../../services/guard_signal_ledger.py` | Cumulative signal state |
| `../../services/guard_alert_policy.py` | Deterministic notification policy |
| `../../tests/services/test_guard_analyzer.py` | Analyzer contract tests |
| `../../tests/services/test_guard_signal_ledger.py` | Ledger state tests |
| `../../tests/services/test_guard_alert_policy.py` | Policy tests |

Generated run directories are written under `tests/evaluation/results/` and
are ignored by Git. They contain raw model responses and should be treated as
local debugging artifacts.

## Analyzer contract

`GuardAnalyzer` is detection-only. It answers:

> What is happening in the conversation snapshot, and how serious is it?

The structured result is:

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

It does **not** return `should_alert`. Alerting is owned by
`DefaultAlertPolicy`.

### Decisions

- `none`: No meaningful concern.
- `watch`: One early or mild signal; monitor only.
- `concerning`: A meaningful pattern exists.
- `urgent`: An immediate or imminent safety concern.

### Categories

The current taxonomy is intentionally small and frozen for the initial
families:

```text
suspicious_contact
bullying
sexual_harassment
threats
```

### Signals

Signals are canonical enum values:

```text
offline_knowledge
location_request
routine_probing
secrecy
meeting_request
repeated_harassment
exclusion
bullying
threat
grooming
```

Signals currently represent presence/absence. They do not have individual
strength scores. Overall `confidence` applies to the analysis as a whole.

### Evidence IDs

Every input message has a stable ID. The analyzer may cite supporting
messages through `evidence_message_ids`.

Rules:

- every cited ID must exist in the input snapshot;
- duplicate evidence IDs are rejected;
- evidence IDs are displayed in terminal, JSON, and Markdown reports;
- evidence is explanatory and is not currently a separate pass/fail gold
  criterion;
- raw message text is not printed in summary tables, but is retained in the
  ignored JSON run artifact for debugging.

## Snapshot-local analyzer semantics

Each analyzer call evaluates the supplied conversation prefix as a realtime
snapshot:

```text
m1..m3 -> analyze current prefix
m1..m5 -> analyze current prefix
m1..m7 -> analyze current prefix
```

The LLM should report what is supported by the current input. It should not
maintain cross-call state or be responsible for preserving signals from
previous analyzer results.

The system prompt explicitly delegates cumulative state to the caller:

```text
Do not maintain state across analyzer calls; the caller maintains cumulative
signal state separately.
```

This distinction matters:

- raw analyzer output shows what the model saw/detected at that snapshot;
- the ledger preserves signals across snapshots;
- policy consumes the ledger state.

A later raw output omitting a signal is therefore not a retraction.

## Dataset model

### `EvalMessage`

```python
@dataclass(frozen=True)
class EvalMessage:
    id: str
    sender: str
    text: str
```

Message IDs are stable within a case and are used for prefixes and evidence.

### `ExpectedSnapshot`

```python
@dataclass(frozen=True)
class ExpectedSnapshot:
    after_message_id: str
    acceptable_decisions: tuple[Decision, ...]
    required_categories: tuple[str, ...] = ()
    required_signals: tuple[str, ...] = ()
    forbidden_signals: tuple[str, ...] = ()
```

A snapshot is evaluated after the message identified by
`after_message_id`. The analyzer sees the entire prefix through that message.

The analyzer gold intentionally does **not** include:

- exact confidence;
- exact reason wording;
- summary wording;
- `should_alert`.

Those are brittle or belong to another layer.

## Current analyzer families

There are currently three conversation cases across two families. The first
family has five checkpoints; the second is a contrast pair.

### Family 1: `unknown_contact_escalation`

Case: `unknown_contact_escalation_001`

| Snapshot | Conversation development | Analyzer expectation |
|---|---|---|
| `m2` | Harmless greeting | `none` |
| `m3` | Unknown contact demonstrates offline knowledge | `watch`, `offline_knowledge` |
| `m5` | Contact probes where/how the child waits after school | `concerning`, `suspicious_contact`, `location_request`, `routine_probing` |
| `m7` | Contact asks for secrecy from parents | `concerning` or `urgent`, `secrecy` |
| `m8` | Contact proposes meeting at the location | `urgent`, `meeting_request`, `secrecy` |

The analyzer gold is snapshot-local. At `m7`, it does not require the LLM to
repeat `location_request`; the ledger is responsible for retaining the
previously observed signal.

This family tests early-vs-late detection and escalation timing:

```text
plain greeting
  -> offline knowledge
  -> routine/location probing
  -> secrecy
  -> meeting escalation
```

### Family 2: `teasing_vs_bullying`

This is a contrast pair designed to detect false positives.

#### Negative: `teasing_vs_bullying_001_negative`

Friends use reciprocal joking, emojis, and mutual engagement. Expected:

```text
none
no bullying signal
no threat signal
```

#### Positive: `teasing_vs_bullying_002_positive`

The conversation develops from insults to repeated harassment and exclusion:

| Snapshot | Conversation development | Analyzer expectation |
|---|---|---|
| `m2` | Child asks them to stop | `none` or `watch` |
| `m5` | Repeated harassment is established | `watch` or `concerning`, `repeated_harassment` |
| `m7` | Group exclusion is explicit | `concerning`, `bullying`, `repeated_harassment`, `exclusion` |

The contrast is more important than any isolated insult. Friendly mutual
banter should not be classified as bullying solely because it contains rude
words.

## What the analyzer eval checks

For every snapshot, the harness checks:

### Decision

The actual decision must belong to `acceptable_decisions`.

Multiple acceptable decisions are allowed for genuinely borderline points.

### Required categories

Every category in `required_categories` must be present in the actual
analysis.

### Required signals

Every signal in `required_signals` must be present in the actual analysis.

### Forbidden signals

No signal in `forbidden_signals` may be present.

### Evidence

Evidence IDs are validated structurally against the input. They are shown in
reports, but are intentionally not yet a semantic pass/fail assertion. This
lets us inspect evidence quality before freezing evidence-specific gold.

## Three-run evaluation and stability

A single LLM run is not considered a reliable quality measurement. The harness
runs every snapshot three times by default.

For each run it prints:

- case and snapshot;
- actual decision;
- pass/fail status;
- evidence IDs;
- observed signals.

The aggregate report prints:

- total run-snapshots;
- aggregate accuracy;
- pass count per snapshot, such as `3/3` or `1/3`;
- observed decision distribution;
- signal pass count;
- category pass count;
- evidence ID combinations and their frequencies;
- observed signal union.

Interpretation example:

```text
m5: 1/3
m7: 3/3
```

This means the raw analyzer is unstable at `m5`, while its snapshot-local
`m7` output is stable. It does not by itself say whether downstream alerting
is safe; the ledger and policy must be evaluated separately.

The latest frozen results are recorded in `BASELINE.md`.

## Signal ledger

`GuardSignalLedger` is deterministic and append-only by default.

```text
raw GuardAnalysis(m5)
  -> ledger.update(...)
  -> location_request active

raw GuardAnalysis(m7) omits location_request
  -> ledger.update(...)
  -> location_request remains active
```

The ledger stores:

- active signals;
- active categories;
- evidence IDs associated with each signal.

Absence from a later analyzer result is not treated as a contradiction. An
explicit signal-retraction contract may be added later if the product defines
what disproves a previously observed signal.

## Alert policy evaluation

Policy evaluation is deterministic and does not call the LLM. Its fixtures
live in `guard_policy_cases.py`.

Initial policy behavior:

```text
none -> no alert
watch -> no alert
urgent -> alert
developing bullying -> no alert
established bullying + exclusion -> alert
actionable suspicious contact -> alert
prior concerning alert -> suppress duplicate
```

The policy should consume the current analysis plus the derived ledger state,
not only the latest raw analyzer output.

## Malformed output and retry behavior

The analyzer uses Pydantic structured output through the OpenAI parse path.
The contract rejects:

- unknown fields;
- invalid enum values;
- invalid evidence IDs;
- duplicate evidence IDs;
- missing required fields;
- unexpected parsed output types.

The analyzer retries transient provider errors and invalid parsed output up to
`max_retries` (default: one retry). Retry behavior is covered by focused unit
tests without making API calls.

## How to add a case

1. Add stable `EvalMessage` IDs.
2. Add one or more snapshot checkpoints.
3. Keep analyzer gold limited to decision, categories, and snapshot-local
   signals.
4. Do not add `should_alert` to analyzer snapshots.
5. Add separate `GuardPolicyCase` fixtures if notification behavior needs
   testing.
6. Run the three-run eval and compare against `BASELINE.md`.
7. Include the model, prompt version, analyzer version, and run IDs in any
   baseline update.

Prefer scenario families and contrast pairs over isolated toxic messages.

## Current limitations

- Only two analyzer families are covered.
- Emotional distress and self-harm are not yet in the taxonomy or dataset.
- Group chats, media, voice, screenshots, slang, and mixed-language coverage
  are limited.
- Evidence IDs are validated and displayed but not semantically scored yet.
- The ledger has no explicit signal-retraction lifecycle yet.
- Signal strength is not modeled; signals are present or absent.
- The analyzer and policy are evaluated separately; a full end-to-end LLM →
  ledger → policy evaluation is deferred.
