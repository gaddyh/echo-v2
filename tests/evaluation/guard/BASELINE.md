# Guard Evaluation Baseline

The Guard evaluation now separates detection from notification policy:

- **Analyzer:** the LLM identifies what is happening, severity, categories,
  and cumulative signals.
- **Policy:** deterministic `DefaultAlertPolicy` decides whether to notify.
- **End-to-end:** analyzer assertions plus policy assertions together.

## Previous baseline

The previous combined implementation scored 7/9 (77.8%) because the LLM
both detected the conversation and decided `should_alert`.

- `m7` dropped the already-established `location_request` signal.
- Bullying `m5` alerted before the notification threshold.

## Detection-only baseline

- **Run ID:** `20260929_233909`
- **Model:** `gpt-4.1`
- **Prompt version:** `v0.2-cumulative-signals-alert-threshold`
- **Cases:** 3
- **Snapshots:** 9
- **Errors:** 0
- **Analyzer:** 9/9 (100.0%)
- **Policy:** 9/9 (100.0%)
- **End-to-end:** 9/9 (100.0%)
- **Command:** `pytest -m eval_guard -v -s`

## Interpretation

The LLM correctly preserved `location_request` at `m7`. The bullying `m5`
case is no longer an analyzer alert failure: the analyzer detects the
emerging harassment pattern, and the deterministic policy correctly returns
`should_alert=false` until the pattern includes exclusion at `m7`.

The full raw LLM response and generated report remain in the ignored local
results folder:
`tests/evaluation/results/20260929_233909_guard_all/`.
