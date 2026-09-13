# Product-readiness audit: suppression denominator gap — 2026-09-13

## Outcome

DASH-11 is a real dashboard/API residual, but its existing suppression-cost
analyzer compares different populations. In the observed 7-day window it reports
18,348 sampled rows / 13,524 suppression events = **135.67%**, while both
`sampling_dead` and `sampling_degraded` are false. Do not present that ratio as
complete or healthy sampling coverage. The next useful slice is a read-only,
per-signal denominator reconciliation and explicit cohort-mismatch state before
exposing suppression-cost estimates. This PR files findings; it changes no runtime
behavior and does not deliver DASH-08 or DASH-11.

## State checked

- Assigned worktree refreshed from origin/master at
  `6c56186e31db24ebf6fe769a798cc9cd65e73015`; new branch
  `docs/autonomous-state-20260913`.
- `AGENTS.md` absent; prompt-provided rules and repository `CLAUDE.md` consulted.
  Read `tasks/lessons.md`, `tasks/todo.md`, top Current Final Backlog Snapshot,
  and the newer reconciliation in `backlog.md:3`.
- Reconciliation names `tasks/backlog_fable_analysis_2026_07_10.md` authoritative.
  It is absent from origin/master and production, but exists **untracked** at
  `C:/projects/gecko-alpha/tasks/backlog_fable_analysis_2026_07_10.md`.
  Its DASH entries are useful scoping evidence, not proof of current runtime or
  a versioned completion tracker. The source file was read, not imported/modified.
- SSH to `srilu-vps` succeeded. At 2026-09-13 19:16–19:18 UTC,
  gecko-dashboard and gecko-pipeline were active, both using `/root/gecko-alpha`.
  Production checkout matched the baseline SHA. Untracked production artifacts
  exist; matching SHA does not mean clean checkout or attest loaded module SHAs.
- Dashboard `/api/status` returned HTTP 200. Other GET responses parsed as JSON.
  Runtime observations below are snapshots, not a soak or a production health audit.

## Selection and drift

| Item | Source evidence at baseline | Decision |
|---|---|---|
| Cockpit parent | `backlog.md:2888`, archived parent | Child work only |
| DASH-01 funnel | `dashboard/api.py:370`, `dashboard/db.py:976`, `dashboard/frontend/components/DispatchFunnelPanel.jsx:50`, `dashboard/frontend/App.jsx:277`; PR #443 | Already implemented; do not rebuild |
| DASH-08 live status | reusable `dashboard/db.py:3615` and `SignalTrustTab.jsx:13`; Inbox surfaces at `dashboard/db.py:1834`, Focus projection at `:2530` omits live status | Trust-tab primitive exists; Focus/Inbox integration is absent |
| DASH-11 cost visibility | `scripts/suppression_cost_rollup.py:117`; dashboard lacks suppression-cost endpoint/component | Genuine residual, selected for readiness audit |
| Historical-pool probe | `backlog.md:108` and `:447` | Closed with negative result; no repeat vendor calls |

The 2026-07-10 local Fable DASH-08 and DASH-11 entries are at lines 131 and 134.
DASH-11 was selected after runtime showed no paper-backed Inbox candidates in the
current snapshot. That reduces observed immediate benefit of paper-signal badges;
it neither closes DASH-08 nor forbids its additive implementation. Any future
badge must be per signal/provenance, preserve grouping/order, and avoid inferring
whole-token suspension from one historical source.

## Current trader surface

`/api/trade_inbox`, generated 2026-09-13T19:18:28.987542+00:00:
83 tracker rows considered, 0 paper rows, 0 open trades scanned; 30 rows returned
under the default per-group cap. Full group counts: act_now=0, watch=27,
already_ran=40, blocked=16. `/api/todays_focus`, generated
2026-09-13T19:18:28.999836+00:00, returned 5 rows, all tracker corpus.
The endpoints select different capped sets; 30 and 5 are not total universe sizes.
No inference is made about why paper rows are absent.

`/api/signal_trust_registry`, generated 2026-09-13T19:17:43Z, reports
`signal_params_joined=true` and `registry_stale=true`. Observed live join includes
chain_completed, volume_spike and first_signal disabled with suspension timestamps;
narrative_prediction has enabled=1 and no suspension timestamp. This is **not**
proof that narrative_prediction can dispatch: upstream/downstream gates were not
traced for that claim. Static maturity labels and current live state are distinct.

The 1-day dispatch funnel at 19:17:41 UTC returned 5,585 events, all blocked:
signal_disabled 1,944; suppressed 1,482; below_min_market_cap 740; late_pump 690;
above_max_market_cap 603; junk_candidate 126. These are counts in that table,
not a complete count of every pipeline block (see denominator finding).

## Suppression cohort and runtime finding

The deployed existing analyzer was invoked at
2026-09-13T19:21:24.303417+00:00 with window_days=7, lookback_days=30,
min_sample=10, min_sampling_fraction=0.5 and min_rows_per_day=1. Its normal default
lookback is 120 days; this audit deliberately uses 30 and does not claim the same
maturity result for a 120-day earliest-anchor cohort.

The helper was imported without running its CLI/main/send path. Its
`aiosqlite.connect` was wrapped in-process to open only
`file:/root/gecko-alpha/scout.db?mode=ro`, `uri=True`, `timeout=10`.
A shell `timeout 45` bounded the invocation. No production files or DB rows changed.

| Measure | Observed |
|---|---:|
| 7d dispatcher suppression sampled rows | 18,348 |
| 7d suppressed decision-event rows | 13,524 |
| Reported sampling fraction | 1.3566992 |
| 7d distinct sampled tokens | 891 |
| 30d dispatcher suppression rows | 152,130 |
| 30d distinct tokens | 2,649 |
| 30d r7d-labeled rows | 31,057 |
| Distinct tokens with resolved r7d at earliest 30d emission | 39 |
| Malformed verdict JSON rows counted by analyzer | 0 |

The 39-token count clears the script's n>=10 diagnostic, but does not establish
representativeness, exit realizability, or valid price provenance. Earliest-token
selection precedes checking r7d resolution. Broad raw ledger totals (188,251
30d gated_out_sample rows, 31,727 with r7d) include other reasons and are not the
suppression cohort. No dollar estimate or pruning/ranking recommendation is made.

Independent read-only SQL with cutoff `2026-09-06T19:22:14.574381+00:00`:

| Signal/surface | Dispatcher-suppression ledger rows | Suppressed decision rows |
|---|---:|---:|
| losers_contrarian | 13,524 | 13,524 |
| chain_completed | 4,490 | 0 |
| first_signal | 334 | 0 |
| Total | 18,348 | 13,524 |

The 4,824-row excess is exactly the two surfaces absent from the denominator.
Both queries used one SQLite read-only connection; the table counts were obtained
sequentially, not under a pinned transaction. Source inspection supports the
structural explanation: first_signal suppression at `scout/trading/signals.py:852`
and chain_completed at `:1408` record a ledger emission then continue without
emitting a trade decision. losers_contrarian at `:718` emits the decision and at
`:722` records the ledger emission. The analyzed numerator spans all dispatcher
surfaces (`scripts/suppression_cost_rollup.py:165`), while its denominator is
suppression rows available in trade_decision_events (`:143`). Its degraded check
at `:212` only tests low fraction/row rate and does not flag excess fraction.
This explains a population mismatch; it is not evidence of healthy full coverage.

Reproduction queries (bind `:cutoff` to the same UTC ISO value for both):

```sql
SELECT surface, COUNT(*)
FROM signal_outcome_ledger
WHERE kind='gated_out_sample' AND emitted_at >= :cutoff
  AND json_valid(gate_verdicts)
  AND json_extract(gate_verdicts,'$.reason')='suppressed'
  AND json_extract(gate_verdicts,'$.source_layer')='dispatcher'
GROUP BY surface;

SELECT signal_type, COUNT(*) FROM trade_decision_events
WHERE reason='suppressed' AND created_at >= :cutoff
GROUP BY signal_type;
```

For subsequent attribution, repeat with one pinned read transaction, trace all
suppression writers, and verify retention windows, timestamps, and price-label
provenance. These checks are not completed by this snapshot.

## Next gate

Advance DASH-11 with a bounded read-only cohort-health contract before a cost
panel. Reuse the existing analyzer's per-token anchor semantics. Make population
mismatch and unknown denominator explicit; do not hide it by clamping the ratio
to 100% or dropping uncovered surfaces. Reconcile producer coverage versus query
cohort before claiming a sampling percentage. A regression fixture should reproduce
matched losers rows plus unmatched chain/first rows. Upstream event-writer changes
would be a separately reviewed additive observability slice, with freshness checks.
This is the next engineering gate, not an operator approval request.

DASH-08 remains a separate per-signal status annotation follow-up; mixed sources
and tracker-only unknown status need fixtures. Paid historical coverage and NAR-04
activation remain subject to their recorded operator gates. Commit/reconcile the
local authoritative tracker in a dedicated pass rather than silently importing its
stale entries into this PR.

## Reviews, verification and authorization

Two parallel source-drift audits completed. Two parallel plan reviews approved;
folds: exact timestamps/SHA, considered vs returned counts, separate sampling and
maturity windows. Two parallel design reviews approved; folds: absent Focus/Inbox
integration stated precisely, explicit read-only analyzer connection, and no
readiness claim from n or checkout SHA. PR reviews occur after opening this PR;
the final session report records their results.

Existing analyzer tests: `python -m pytest -q
 tests/test_suppression_cost_rollup_script.py --tb=short` — **8 passed** on
2026-09-13. They validate existing behavior, not a fix for this discovered mismatch.
`git diff --check` and changed-file scope checks are required before publication.
No feature code, flags, schema, thresholds, or production files changed; no deploy.

| Action | Class | Approval record | Run date |
|---|---|---|---|
| Read-only repo/runtime audit | inspection | Autonomous work-loop request | 2026-09-13 |
| Feature branch, docs commits, findings PR | reversible repo publication | Request explicitly permits branches/commits/PRs and findings outcome | 2026-09-13 |
| Merge/deploy/runtime writes | not performed | Not applicable to this report | 2026-09-13 |

Hermes-first assessment and its catalog-access limitation are recorded in
`tasks/plan_autonomous_product_readiness_2026_09_13.md` and
`tasks/design_autonomous_product_readiness_2026_09_13.md`.
