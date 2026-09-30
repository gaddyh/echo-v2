# Guard Analyzer Baseline

This baseline is for the LLM Guard analyzer only. The analyzer detects what
is happening, severity, categories, and cumulative signals. It does not decide
whether a parent should be notified.

Notification behavior is defined separately by `DefaultAlertPolicy` and tested
with deterministic policy fixtures in `guard_policy_cases.py`.

## Analyzer baseline

- **Run ID:** `20260929_235627`
- **Model:** `gpt-4.1`
- **Prompt version:** `v0.2-cumulative-signals-alert-threshold`
- **Cases:** 3
- **Snapshots:** 9
- **Errors:** 0
- **Analyzer:** 8/9 (88.9%)
- **Command:** `pytest -m eval_guard -v -s`

The one miss was a stochastic signal omission at `m5` of
`unknown_contact_escalation_001`: the model returned `routine_probing` but
omitted `location_request`. A subsequent run should be compared against this
baseline; the analyzer eval is intentionally not coupled to policy behavior.

## Three-run stability baseline

- **Run IDs:** `20260930_000617`, `20260930_000635`, `20260930_000652`
- **Runs:** 3
- **Run results:** 7/9 (77.8%), 8/9 (88.9%), 7/9 (77.8%)
- **Aggregate:** 22/27 (81.5%)
- **Errors:** 0
- **Command:** `pytest -m eval_guard -v -s`

Per-snapshot stability:

| Case | Snapshot | Passes | Signal pass | Category pass | Observed behavior |
|---|---:|---:|---:|---:|---|
| `unknown_contact_escalation_001` | `m2` | 3/3 | 3/3 | 3/3 | `none` consistently |
| `unknown_contact_escalation_001` | `m3` | 3/3 | 3/3 | 3/3 | `watch` + `offline_knowledge` consistently |
| `unknown_contact_escalation_001` | `m5` | 0/3 | 0/3 | 3/3 | `location_request` missed in all runs |
| `unknown_contact_escalation_001` | `m7` | 1/3 | 1/3 | 3/3 | `location_request` retained once |
| `unknown_contact_escalation_001` | `m8` | 3/3 | 3/3 | 3/3 | `urgent` consistently |
| `teasing_vs_bullying_001_negative` | `m5` | 3/3 | 3/3 | 3/3 | `none` consistently |
| `teasing_vs_bullying_002_positive` | `m2` | 3/3 | 3/3 | 3/3 | `watch` consistently |
| `teasing_vs_bullying_002_positive` | `m5` | 3/3 | 3/3 | 3/3 | `concerning` + harassment consistently |
| `teasing_vs_bullying_002_positive` | `m7` | 3/3 | 3/3 | 3/3 | `concerning` + exclusion consistently |

The instability is concentrated in the unknown-contact escalation sequence,
while the teasing-versus-bullying contrast is stable. The analyzer eval is
intentionally not coupled to policy behavior.

## Policy verification

The deterministic policy cases are run without an LLM:

- `watch` → no alert
- developing bullying → no alert
- established bullying with exclusion → alert
- actionable suspicious contact → alert
- `urgent` → alert
- previously alerted concerning case → suppressed

The policy gold is maintained separately from the analyzer golden cases.
