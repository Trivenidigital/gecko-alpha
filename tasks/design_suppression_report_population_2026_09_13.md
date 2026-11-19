**New primitives introduced:** NONE

# Design: per-signal suppression count diagnostics

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Count diagnostics | No replacement verified | Correct existing local rollup |
| Scheduling | Hermes/Codex skills already present | No deployment, scheduling or sends |

Checked https://hermes-agent.nousresearch.com/docs/skills (catalog unavailable)
and https://github.com/0xNyk/awesome-hermes-agent on Sept 13. Verdict: no new
dependency; reuse existing analysis and orchestration boundaries.

## Plan review folds

Parallel logic/statistical and ops/safety reviewers approved with these folds:
preserve aggregate rows/day threshold; define empty/missing/excess populations;
document exit-5 change for partial lane death; enforce read-only URI and test
missing/special-character paths; preserve cost fields and no-send default.

## Exact contract

Existing `analyze` signature/defaults and `cost` calculation remain unchanged.
Connect with `Path(db_path).resolve().as_uri() + '?mode=ro'`, `uri=True`.
Read decision counts GROUP BY signal_type and count already-selected ledger
window rows by surface. Preserve existing time-bound convention and cohort.

`health.population_counts` is a sorted list of objects with signal_type,
sampled_rows, decision_rows and count_ratio (null for zero denominator).
It describes counts, not event-level matching. `population_mismatch_signals`
contains signals with sampled_rows > decision_rows (including ledger-only).
`missing_sample_signals` contains signals with decision_rows > 0 and zero ledger.
`sampling_dead` is true if any such missing-sample signal exists; existing CLI
exit 5 therefore also covers a partial lane outage formerly hidden by another
lane. No new exit code; no cron/deploy changes in this PR.

`sampling_fraction` remains for callers but is null when any population mismatch
exists; otherwise it is aggregate ledger/event count ratio (null when no events).
Do not claim even a ratio of one proves event-level correspondence.
`degraded_reasons` includes `population_mismatch:<signal>` for unmatched/excess
lanes, and `fraction<threshold:<signal>` for each event-bearing nonempty lane
below existing min_sampling_fraction. Keep existing aggregate rows/day rule.
Zero/zero = NO OBSERVED ACTIVITY, not healthy sampling. Ledger-only = population
mismatch, not no activity. Missing lane is named even if other lanes have rows.

Output stays <=6 lines. First line marks EXPERIMENTAL/not-for-pruning.
Health line names missing/mismatch/degraded lanes and calls ratio a count ratio,
not sampling coverage. For mismatch, aggregate count ratio is UNKNOWN and
warning remains visible even when cost clears n-gate. Cost math/selection and
top movers remain exactly unchanged; counterfactual disclaimer remains.
Remove dated '#421 not deployed' and fixed July31 maturity expectations from
docstring and messages. Say verify runtime deployment/ingest/label state.

## Tests, runtime check and review barrier

Extend `_add_suppressed_block` fixture with signal_type (default unchanged).
Regress missing/low lane hidden by another signal; excess lane; empty DB;
balanced split lanes with aggregate rows/day unchanged. Existing malformed JSON,
distinct-token cost, horizon and no-send tests must pass. Test no DB creation on
missing path and successful read for a path containing '#' and '?'-safe URI
encoding where platform allows (use '#' filename on Windows).
Verify SQLite connection is read-only via an attempted write on the actual
connection intercepted in the test, which must fail, then let SELECTs finish.

Runtime: invoke old and new `analyze` with the same fixed timestamp and identical
parameters on production read-only data, compare entire `cost` object. Import
new source via stdin, no remote file writes, no --send. Publish population
diagnostics and exact limits. Re-review all affected vectors on new PR SHA;
record only actual terminal verdicts; exact-head CI before merge. Initial probes
do not deploy. After merge, the closeout report's script-refresh preflight and
no-send smoke apply under current production-push authorization; rollback is
revert via normal PR/CI, with no data restore or service restart.

Design review fold: fixed timestamps alone do not freeze mutable labels.
Runtime comparison used a shared SQLite read transaction for old/new analysis;
the complete cost objects matched. Existing lower-bound time convention is
preserved; this is not a replay with an upper cutoff.
