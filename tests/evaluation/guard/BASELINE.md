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

## Policy verification

The deterministic policy cases are run without an LLM:

- `watch` → no alert
- developing bullying → no alert
- established bullying with exclusion → alert
- actionable suspicious contact → alert
- `urgent` → alert
- previously alerted concerning case → suppressed

The policy gold is maintained separately from the analyzer golden cases.
