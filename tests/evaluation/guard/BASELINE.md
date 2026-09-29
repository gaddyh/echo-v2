# Guard Evaluation Baseline

This file records the first reproducible Guard baseline after introducing
Pydantic structured output and enum-backed signals/categories.

## Run metadata

- **Run ID:** `20260929_222102`
- **Model:** `gpt-4.1`
- **Prompt version:** `v0.1-structured-output`
- **Cases:** 3
- **Snapshots:** 9
- **Errors:** 0
- **Passing snapshots:** 7/9
- **Snapshot accuracy:** 77.8%
- **Command:** `pytest -m eval_guard -v -s`

## Results

| Case | Snapshot | Expected decision | Actual decision | Signals | Categories | Alert | Status |
|---|---:|---|---|---|---|---|---|
| `unknown_contact_escalation_001` | `m2` | `none` | `none` | pass | pass | pass | PASS |
| `unknown_contact_escalation_001` | `m3` | `watch` | `watch` | pass | pass | pass | PASS |
| `unknown_contact_escalation_001` | `m5` | `concerning` | `concerning` | pass | pass | pass | PASS |
| `unknown_contact_escalation_001` | `m7` | `concerning\|urgent` | `concerning` | **missing `location_request`** | pass | pass | FAIL |
| `unknown_contact_escalation_001` | `m8` | `urgent` | `urgent` | pass | pass | pass | PASS |
| `teasing_vs_bullying_001_negative` | `m5` | `none` | `none` | pass | pass | pass | PASS |
| `teasing_vs_bullying_002_positive` | `m2` | `none\|watch` | `watch` | pass | pass | pass | PASS |
| `teasing_vs_bullying_002_positive` | `m5` | `watch\|concerning` | `concerning` | pass | pass | **expected false, got true** | FAIL |
| `teasing_vs_bullying_002_positive` | `m7` | `concerning` | `concerning` | pass | pass | pass | PASS |

## Known gaps

1. `location_request` is not preserved at `m7` after being detected at `m5`.
2. The bullying case alerts too early at `m5`; the expected policy is to monitor
   the emerging pattern without alerting until exclusion is established at `m7`.

This baseline is intentionally checked in as a compact semantic summary. The
full raw LLM response and generated report remain in the ignored local results
folder:
`tests/evaluation/results/20260929_222102_guard_all/`.
