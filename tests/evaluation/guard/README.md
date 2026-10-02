# Guard Evaluation Guide

This directory contains the Guard analyzer evaluation dataset, the
LLM-backed analyzer harness, deterministic decision- and alert-policy cases,
and recorded baselines.

The evaluation is intentionally split into layers:

```text
conversation snapshot
        |
        v
GuardAnalyzer (LLM)
        |
        v
GuardAnalysis          <- semantic evidence: what signals are present?
        |
        v
GuardSignalLedger      <- cumulative state across snapshots
        |
        v
DefaultDecisionPolicy  <- what severity follows from the evidence?
        |
        v
AlertPolicy            <- should a parent be notified?
```

The LLM interprets conversation language and extracts semantic evidence.
Deterministic code owns cumulative state, severity decisions, and notification
policy.

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

The first run after changing gold or matcher semantics should be report-only:

```bash
GUARD_EVAL_REPORT_ONLY=1 GUARD_EVAL_SUITE=comprehensive \
  GUARD_EVAL_RUNS=3 .venv/bin/python -m pytest -m eval_guard -v -s
```

Review the corrected results before choosing a new `GUARD_MIN_ACCURACY` value and
freezing `BASELINE.md`. Until that run is completed, `BASELINE.md` remains the
previous frozen baseline and must not be interpreted as a result for the new
matcher or taxonomy. The harness requires `OPENAI_API_KEY`. The model can be overridden with
`GUARD_LLM_MODEL` or `LLM_MODEL_NAME`.

## Current files

| File | Purpose |
|---|---|
| `guard_cases.py` | Conversation fixtures, snapshot gold, and fixture validation |
| `guard_mvp_eval_cases.py` | Development cases used for prompt iteration |
| `guard_comprehensive_baseline.py` | Held-out regression cases and combined suites |
| `guard_policy_cases.py` | Direct deterministic decision- and alert-policy fixtures |
| `test_guard_eval.py` | Multi-run LLM analyzer harness and aggregate report |
| `test_guard_eval_contract.py` | API-free matcher and fixture-integrity tests |
| `guard_eval_results.py` | JSON and Markdown persistence for each run |
| `BASELINE.md` | Current frozen analyzer and policy baseline |
| `../../services/guard_analyzer.py` | LLM analyzer, schema, prompt, and parsing |
| `../../services/guard_signal_ledger.py` | Cumulative signal state |
| `../../services/guard_decision_policy.py` | Deterministic severity decision policy |
| `../../services/guard_alert_policy.py` | Deterministic notification policy |
| `../../tests/services/test_guard_analyzer.py` | Analyzer contract tests |
| `../../tests/services/test_guard_signal_ledger.py` | Ledger state tests |
| `../../tests/services/test_guard_alert_policy.py` | Decision- and notification-policy tests |

Generated run directories are written under `tests/evaluation/results/` and
are ignored by Git. They contain raw model responses and should be treated as
local debugging artifacts.

## Analyzer contract

`GuardAnalyzer` is detection-only. It answers:

> What semantic signals and categories are supported by this conversation snapshot?

The structured result contains evidence for downstream deterministic policies:

```python
GuardAnalysis(
    categories=(...),
    signals=(...),
    evidence_message_ids=(...),
    confidence=...,
    reason=...,
)
```

It does **not** return a severity `decision` or `should_alert`. The analyzer
reports evidence only; `DefaultDecisionPolicy` derives the severity decision,
and `DefaultAlertPolicy` decides whether a parent should be notified.

### Decision policy

`DefaultDecisionPolicy` derives one of these decisions from the current
analysis, or from the cumulative `GuardSignalState` when one is supplied:

- `none`: No active signals.
- `watch`: One early, mild, or ambiguous signal.
- `concerning`: At least two active signals or an active category establishes a
  meaningful pattern.
- `urgent`: Immediate or imminent risk, including self-harm, blackmail, or the
  combination of secrecy and a meeting request.

The decision is policy output, not LLM output and not a field in
`GuardAnalysis`. This keeps severity thresholds deterministic and lets them
evolve independently from the analyzer prompt.

### Categories

The current taxonomy is intentionally small for the initial families:

```text
suspicious_contact
child_sexual_exploitation
harassment_or_coercion
distress
bullying
social_exclusion
harmful_sharing
```

### Signals

Signals are canonical enum values:

```text
offline_knowledge
personal_information_request
routine_probing
location_request
secrecy_request
meeting_request
age_deception
sexual_solicitation
intimate_image_request
sexual_coercion
off_platform_migration
repeated_unwanted_contact
boundary_violation
threat
coercive_demand
blackmail_or_extortion
help_request
fear_expression
hopelessness
self_harm_expression
repeated_targeting
insult_or_humiliation
group_pile_on
exclusion
coordinated_exclusion
harmful_content_sharing
threat_to_share
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
- non-clean analyses must include at least one valid evidence ID;
- evidence is checked for presence and required IDs, while semantic evidence
  quality is reviewed in the report;
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
    required_categories: tuple[str, ...] = ()
    required_signals: tuple[str, ...] = ()
    required_signal_any_of: tuple[str, ...] = ()
    forbidden_categories: tuple[str, ...] = ()
    forbidden_signals: tuple[str, ...] = ()
    required_evidence_message_ids: tuple[str, ...] = ()
    expected_decision: str | None = None
    expect_clean: bool = False
```

A snapshot is evaluated after the message identified by
`after_message_id`. The analyzer sees the entire prefix through that message.
The gold checks analyzer semantics and metadata quality; severity is derived
separately by `DefaultDecisionPolicy` from the same analysis or ledger state.
The LLM is never graded on a generated decision field.

The analyzer gold intentionally does **not** include an LLM-generated severity
decision. The harness derives a decision deterministically with
`DefaultDecisionPolicy` from the same analysis and can assert
`expected_decision` separately.

The harness checks that confidence is finite and within the schema range, that
reason is non-empty, and that non-clean analyses cite evidence. It does not pin
exact confidence values or exact reason wording. `expect_clean=True` is strict:
the derived decision must be `none`, categories and signals must both be empty,
and evidence IDs must be empty.

## Current analyzer families

The evaluation contains MVP development cases and held-out regression cases
covering suspicious contact, harassment/coercion, distress, bullying,
social exclusion, harmful sharing, and child sexual exploitation. Cases use
snapshot checkpoints and benign/adversarial contrast pairs.

### Family 1: `unknown_contact_escalation`

Case: `unknown_contact_escalation_001`

| Snapshot | Conversation development | Analyzer evidence expectation |
|---|---|---|
| `m2` | Harmless greeting | no signals or categories |
| `m3` | Unknown contact demonstrates offline knowledge | `offline_knowledge` |
| `m5` | Contact probes where/how the child waits after school | `suspicious_contact`, `offline_knowledge`, `routine_probing` |
| `m7` | Contact asks for secrecy from parents | `secrecy_request` |
| `m8` | Contact proposes meeting at the location | `meeting_request`, `secrecy_request` |

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
no bullying signal
no threat signal
```

#### Positive: `teasing_vs_bullying_002_positive`

The conversation develops from insults to repeated harassment and exclusion:

| Snapshot | Conversation development | Analyzer evidence expectation |
|---|---|---|
| `m2` | Child asks them to stop | no required signal |
| `m5` | Repeated harassment is established | `insult_or_humiliation` plus `repeated_targeting` or `group_pile_on` |
| `m7` | Group exclusion is explicit | `bullying`, `insult_or_humiliation`, `exclusion`, plus `repeated_targeting` or `group_pile_on` |

The contrast is more important than any isolated insult. Friendly mutual
banter should not be classified as bullying solely because it contains rude
words.

### Family 3: `child_sexual_exploitation`

The child-exploitation cases cover two private-chat trajectories:

- explicit age contradiction, off-platform migration, intimate-image request,
  sexual solicitation, and meeting escalation;
- intimate-image request, refusal, sexual coercion, threat to share, and
  blackmail/sextortion escalation.

`age_deception` is required only when the transcript itself contains a
contradictory age claim. The fixtures do not assume that the analyzer knows a
participant's age from metadata. The held-out set also includes group intimate
content sharing, mention-based pile-on, coordinated side-group exclusion, and
an adversarial instruction that attempts to force `none` during a real
sextortion pattern.

## What the analyzer eval checks

For every snapshot, the harness checks analyzer output and deterministic
policy derivation:

### Required categories

Every category in `required_categories` must be present in the actual
analysis.

### Required signals

Every signal in `required_signals` must be present in the actual analysis.

### Forbidden categories and signals

No category in `forbidden_categories` or signal in `forbidden_signals` may be
present.

### Clean snapshots

When `expect_clean=True`, the result must have `decision=none`, no categories,
no signals, and no evidence IDs. This is the required representation for
strict benign/OOS checkpoints.

### Evidence, confidence, and reason

Evidence IDs are validated against the input. Non-clean analyses must cite at
least one evidence ID, confidence must be finite and within `0.0..1.0`, and
reason must be non-empty. Exact confidence and reason wording are not frozen;
reports retain them for review.

## Three-run evaluation and stability

A single LLM run is not considered a reliable quality measurement. The harness
runs every snapshot three times by default.

For each run it prints:

- case and snapshot;
- pass/fail status;
- derived deterministic decision;
- confidence and evidence IDs;
- observed signals and categories.

The aggregate report prints:

- total run-snapshots;
- aggregate analyzer accuracy;
- pass count per snapshot, such as `3/3` or `1/3`;
- signal and any-of pass counts;
- category and clean-result pass counts;
- evidence and deterministic-decision pass counts;
- evidence ID combinations and their frequencies;
- observed signal union.

The report includes the deterministic decision derived from each analysis, but
that decision is not an LLM output. Parent notification remains separate and is
evaluated with the deterministic alert-policy fixtures.

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

## Decision and alert-policy evaluation

Decision and alert-policy evaluation is deterministic and does not call the
LLM. Its fixtures live in `guard_policy_cases.py`.

`DefaultDecisionPolicy` first derives severity from semantic evidence:

```text
no active signals -> none
one active signal -> watch
two or more active signals, or any active category -> concerning
self-harm, blackmail, or secrecy + meeting request -> urgent
```

`DefaultAlertPolicy` then decides whether to notify a parent:

```text
none -> no alert
watch -> no alert
urgent -> alert
developing bullying -> no alert
established bullying (repeated targeting + group pile-on) -> alert
actionable suspicious contact (location or meeting request) -> alert
explicit threat in harassment/coercion -> alert
prior concerning alert or quiet hours -> suppress duplicate
```

Urgent decisions alert immediately; prior-alert and quiet-hours suppression
apply to concerning decisions. `DefaultDecisionPolicy` should consume the
current analysis plus the derived ledger state, not only the latest raw
analyzer output. `DefaultAlertPolicy` owns notification context and should
not be confused with severity decision policy.

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
2. Add one or more chronological snapshot checkpoints.
3. Use `expect_clean=True` for strict benign/OOS snapshots.
4. Keep analyzer gold on categories, signals, evidence, confidence validity,
   and reason presence; do not add an LLM severity decision.
5. Use `expected_decision` only for the deterministic `DefaultDecisionPolicy`
   result derived from the same analysis.
6. Add separate `GuardPolicyCase` fixtures when notification behavior needs
   testing.
7. Run corrected evaluations in report-only mode first, inspect failures, then
   choose the threshold and update `BASELINE.md`.
8. Include the model, prompt version, analyzer version, and run IDs in any
   baseline update.

Prefer scenario families and contrast pairs over isolated toxic messages.

## Current limitations

- Cases are still synthetic Hebrew adaptations rather than a production-labeled
  WhatsApp corpus.
- Group, media, voice, screenshots, timestamps, replies, forwarding, slang, and
  mixed-language coverage remain limited; `EvalMessage` currently models only
  sender, stable ID, and text.
- Trusted private/group/contact metadata is stored on fixtures but is not
  rendered into the analyzer prompt; no gold may rely on that metadata.
- Evidence is checked for valid/present IDs, while semantic evidence quality is
  still reviewed manually from reports.
- The ledger has no explicit signal-retraction lifecycle yet.
- Signal strength is not modeled; signals are present or absent.
- The analyzer and policy are evaluated separately; a full end-to-end LLM →
  ledger → decision → alert evaluation is deferred.
