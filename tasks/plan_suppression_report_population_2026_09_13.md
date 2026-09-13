**New primitives introduced:** NONE

# Plan: suppression report population honesty

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Suppression health reporting | None verified; existing repo rollup already implements the analysis | Correct existing rollup rather than add a dashboard |
| Scheduling | Existing Hermes/Codex capabilities present | No scheduling or delivery changes |

Checked https://hermes-agent.nousresearch.com/docs/skills (catalog did not render)
and https://github.com/0xNyk/awesome-hermes-agent on Sept 13. Installed Hermes
Codex/kanban skill directories exist on prod. Ecosystem verdict: preserve
orchestration; this is a project-specific mismatch between two existing tables.

## Problem and bounded contract

Fresh production query at 19:22:24Z: 18,335 ledger rows / 13,512 decision events.
Fixed-window grouping proves the 4,823 excess is entirely chain_completed and
first_signal, whose suppression branches write ledger but no decision event.
The report prints fraction=1.357 as healthy. Worse, unrelated ledger-only
signals can conceal lost sampling on a signal that does have decision events.

Correct the read-only report, not the pipeline. Group both populations by signal.
Per-signal observed counts cannot prove event-level correspondence; label them
count ratios, not verified coverage. Report unknown comparability whenever a
lane has ledger rows with zero decision events, or more ledger than events.
For lanes with decision events, evaluate low counts per lane so another signal
cannot conceal a missing/underfilled lane. Keep thresholds/n-gate/notional,
cost-cohort selection, optional-send default and pipeline state unchanged.
Remove unconditional '#421 not deployed' and July-31 expected-read assertions.
Add explicit experimental/not-for-pruning scope to output; no paid calls.

## Checkable execution

- [x] Two parallel plan reviews: statistical/logic and operational safety.
- [x] Write exact design and get two parallel design reviews; fold findings.
- [x] Add regressions for ledger-only lane masking another lane's missing or
  low sampling, counts above denominator, zero-event/zero-ledger, balanced lanes,
  and no calendar/deployment assertion. Prove red before implementation.
- [x] Implement grouping + uncertainty output in existing script; cost math
  remains untouched. Open DB with read-only URI to enforce analysis boundary.
- [x] Focused report suites, syntax, read-only DB mutation check and diff check.
- [ ] Update existing PR #571; independently re-review new candidate for all
  affected vectors, record actual reviewed SHA, wait for CI before merge.
- [x] Run updated script against prod DB read-only via stdin import (no remote
  file changes), compare cost fields against old at a fixed timestamp.
- [ ] Record findings, folds, runtime evidence and final disposition.

Authorization: current production-push prompt allows observability/scripts;
no pipeline, policy, threshold, config, DB, secrets or sending change exercised.
Rollback: revert PR via normal checks; no DB restore needed.
