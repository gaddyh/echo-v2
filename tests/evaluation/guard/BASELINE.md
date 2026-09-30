# Guard Analyzer Baseline

This baseline is for the LLM Guard analyzer only. The analyzer detects what
is happening, severity, categories, cumulative signals, and cited evidence
message IDs. It does not decide whether a parent should be notified.

Notification behavior is defined separately by `DefaultAlertPolicy` and tested
with deterministic policy fixtures in `guard_policy_cases.py`.

## Analyzer baseline

- **Prompt version:** `v0.3-cumulative-signals-evidence-ids`
- **Cases:** 3
- **Snapshots:** 9
- **Errors:** 0
- **Command:** `pytest -m eval_guard -v -s`

## Three-run stability baseline

- **Run IDs:** `20260930_003611`, `20260930_003633`, `20260930_003651`
- **Runs:** 3
- **Run results:** 8/9 (88.9%), 9/9 (100.0%), 8/9 (88.9%)
- **Aggregate:** 25/27 (92.6%)
- **Errors:** 0

| Case | Snapshot | Passes | Signal pass | Category pass | Evidence observed | Observed behavior |
|---|---:|---:|---:|---:|---|---|
| `unknown_contact_escalation_001` | `m2` | 3/3 | 3/3 | 3/3 | `-`: 3/3 | `none` consistently |
| `unknown_contact_escalation_001` | `m3` | 3/3 | 3/3 | 3/3 | `m3`: 3/3 | `watch` + `offline_knowledge` |
| `unknown_contact_escalation_001` | `m5` | 3/3 | 3/3 | 3/3 | `m3,m4,m5`: 1/3; `m3,m5`: 2/3 | `concerning` + location/routine signals |
| `unknown_contact_escalation_001` | `m7` | 1/3 | 1/3 | 3/3 | `m3,m5,m7`: 3/3 | signal retention remains unstable |
| `unknown_contact_escalation_001` | `m8` | 3/3 | 3/3 | 3/3 | `m3,m5,m7,m8`: 3/3 | `urgent` consistently |
| `teasing_vs_bullying_001_negative` | `m5` | 3/3 | 3/3 | 3/3 | `-`: 3/3 | `none` consistently |
| `teasing_vs_bullying_002_positive` | `m2` | 3/3 | 3/3 | 3/3 | `m1,m2`: 3/3 | `watch` consistently |
| `teasing_vs_bullying_002_positive` | `m5` | 3/3 | 3/3 | 3/3 | `m1,m2,m3,m4,m5`: 3/3 | harassment detected consistently |
| `teasing_vs_bullying_002_positive` | `m7` | 3/3 | 3/3 | 3/3 | `m1,m2,m3,m4,m5,m6,m7`: 3/3 | exclusion detected consistently |

Evidence IDs are displayed in the terminal report, aggregate stability table,
JSON, and Markdown run reports. Full message text remains in the saved JSON
for debugging; evidence is not currently a separate pass/fail criterion.

## Policy verification

The deterministic policy cases are run without an LLM:

- `watch` → no alert
- developing bullying → no alert
- established bullying with exclusion → alert
- actionable suspicious contact → alert
- `urgent` → alert
- previously alerted concerning case → suppressed

The policy gold is maintained separately from the analyzer golden cases.
