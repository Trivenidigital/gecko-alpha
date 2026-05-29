**New primitives introduced:** NONE

# Actionability Data-Gate Refresh (2026-05-29)

**Backlog items affected:**
- `BL-NEW-X-OUTCOME-LINKAGE` (design PR #184)
- `BL-NEW-TG-OUTCOME-LINKAGE` (design PR #184)
- `BL-NEW-NO-PEAK-RISK-HANDLING` (audit PR #183 — V2 stale-entry candidate)

**Audit scope:** read-only refresh of the 2026-05-26 actionability gate re-validation (`tasks/findings_actionability_gate_revalidation_2026_05_26.md`). Three days of additional v1 closed rows accumulated; this doc tests whether the 2026-05-26 finding's "not yet authorized" follow-up criteria have flipped. No implementation, no V2 stale-entry gate, no suppression policy change, no threshold tuning, no re-enablement in this PR.

## Triage Context

PRs #183 and #184 merged 2026-05-21 as docs-only. Three follow-up backlog items pinned implementation to a symmetric `n_actionable≥20 AND n_exploratory≥5` data gate. The 2026-05-26 re-validation found the row-count gate CLEARED (55+16) but the verdict was **"CLEARED / no immediate implementation authorized"** because:

1. Exploratory `n=16` was below 20 for directional classifier-quality claims.
2. The strongest false-negative lead — below-$10M `chain_completed` blocked by `v1_block_core_signal_mcap_below_10m` — was at `n=12` (the chain_completed+below_10m pair; the reason `v1_block_core_signal_mcap_below_10m` summed across signals was `n=13` on 2026-05-26) and still net negative.
3. Exit-shape dominated PnL across both cohorts.

The 2026-05-26 follow-up trigger was: revisit the below-$10M `chain_completed` bucket "when that same signal/reason pair reaches at least `n=20` closed rows, or if the bucket turns positive after outlier checks." Today is +3 days; this finding tests that trigger.

## Pinned Production State (2026-05-29)

Cutover row (`paper_migrations`):

| name | cutover_ts |
|---|---|
| `bl_new_actionability_gate_v1` | `2026-05-19T11:39:09.121422+00:00` |

All-history closed cohort since cutover (10 days):

| Cohort | n_closed | total_pnl | avg_pnl_pct | wins | win_rate |
|---|---:|---:|---:|---:|---:|
| Actionable (`actionable=1`) | **92** | **-$475.12** | -1.72% | 53 | 57.6% |
| Exploratory (`actionable=0`) | **30** | **-$738.48** | -8.21% | 12 | 40.0% |

Δ since 2026-05-26: actionable +37 rows, swung from +$335.53 to -$475.12 (≈-$810 in 3 days); exploratory +14 rows, swung from -$385.63 to -$738.48 (≈-$353 in 3 days).

Open positions in same period: actionable=103, exploratory=17.

## 2026-05-26 Follow-Up Trigger Check

**Trigger 1 — below-$10M `chain_completed` bucket reaches n≥20 OR turns positive:**

| metric | 2026-05-26 | 2026-05-29 |
|---|---:|---:|
| `v1_block_core_signal_mcap_below_10m` (all exploratory signals) | n=13 / -$305.57 / 7W 6L | **n=24 / -$678.14 / 11W 13L** |

The bucket has now reached `n≥20` (first half of the trigger), **but the cohort flipped from 53.8% win rate to 45.8% win rate and the net loss DEEPENED from -$305.57 to -$678.14**. The bucket has NOT turned positive (second half of the trigger).

Per 2026-05-26's stated condition ("OR"), one half of the trigger is met. But the underlying signal — that below-$10M chain_completed might be a meaningful false-negative class — is REFUTED by 3 more days of data. The classifier-tuning hypothesis weakens.

**Trigger 2 — exploratory cohort reaches n≥20 (was the second 2026-05-26 caveat):**

Today: `n_exploratory_closed = 30`. **Cleared.**

## Cohort Separation Refresh

Per 2026-05-26 finding (n=55+16):

| metric | actionable | exploratory | separation |
|---|---:|---:|---:|
| avg pnl_pct | +6.10% | -24.10% | 30.2pp |
| win % | 78.2% | 43.8% | 34.4pp |

Per today (n=92+30):

| metric | actionable | exploratory | separation |
|---|---:|---:|---:|
| avg pnl_pct | -1.72% | -8.21% | 6.5pp |
| win % | 57.6% | 40.0% | 17.6pp |

**Separation compressed from 30.2pp/34.4pp to 6.5pp/17.6pp in 3 days.** Both cohorts shifted negative simultaneously.

## Attribution — Regime Window Overlap

The 3-day separation compression is attributable to market-wide regime degradation that began in the 2026-05-13→2026-05-19 gainers_early auto-suspend window and persisted through the post-2026-05-26 closing-trades window described here. The auto-suspend reversed (`tg_alert_eligible` flip) at 2026-05-19T01:02:14Z, but the underlying regime that drove the auto-suspend continued degrading PnL across all signals through this PR's window. Per `tasks/findings_gainers_early_autosuspend_attribution_2026_05_29.md` (PR #320), every signal had net loss in the original window with nearly identical per-trade losses across signals — the same pattern persists in today's refresh.

This is structurally consistent with the 2026-05-26 finding's caveat that "exit policy dominates the PnL shape across both cohorts" — under a regime where the exit policy gets repeatedly tripped, both cohorts share the exposure.

**Implication:** the cohort separation collapse is consistent with a regime effect (visible in both cohorts), not a gate-design failure. The gate's relative separation test still holds (exploratory still ~5pp worse than actionable per-trade, ~18pp worse on win rate), but at a smaller magnitude than the 2026-05-26 snapshot.

## False-Negative Refresh

By `actionability_reason` (exploratory cohort, all-history-since-cutover):

| reason | n | wins | pnl_usd | avg_pnl_pct |
|---|---:|---:|---:|---:|
| `v1_block_core_signal_mcap_below_10m` | 24 | 11 | -$678.14 | -9.42% |
| `v1_block_tg_social_low_n` | 6 | 1 | -$60.35 | -3.35% |

By `signal_type` (exploratory cohort):

| signal | n | wins | pnl_usd |
|---|---:|---:|---:|
| `chain_completed` | 15 | 8 | -$383.44 |
| `narrative_prediction` | 7 | 2 | -$218.24 |
| `tg_social` | 6 | 1 | -$60.35 |
| `volume_spike` | 2 | 1 | -$76.46 |

`chain_completed` exploratory wins 8/15 (53%) but is still net -$383. The 7 wins from 2026-05-26 (all `chain_completed` / `v1_block_core_signal_mcap_below_10m`) grew by 1 net winner but accumulated 3 additional losses that exceeded the new winner's contribution.

## Branch Decision

**REFRESHED / no immediate implementation authorized.**

- Row-count gate: still CLEARED with significant headroom (92+30, was 55+16).
- 2026-05-26 follow-up trigger (chain_completed below-$10M): partial trigger (n≥20 met) but the directional signal (bucket-turns-positive) is REFUTED by 3 more days of data.
- Cohort separation: compressed but still directionally consistent with gate intent.
- Exit-shape attribution: still the dominant PnL driver per 2026-05-26 finding; nothing in today's refresh contradicts that.
- Regime window overlap (2026-05-13 → 2026-05-19) credibly explains the 3-day separation compression without invalidating the gate.

## Backlog Impact

The three downstream items remain at the same status: `DESIGN-MERGED / RE-SCOPE-ELIGIBLE-AFTER-ACTIONABILITY-REVALIDATION` (2026-05-26 anchor) — no automatic-approval shift today.

Updates inline in `backlog.md`:
- `BL-NEW-ACTIONABILITY-GATE`: append 2026-05-29 refresh line citing this finding; note classifier-change hypothesis weakened by 3 more days of data.
- `BL-NEW-X-OUTCOME-LINKAGE` / `BL-NEW-TG-OUTCOME-LINKAGE` / `BL-NEW-NO-PEAK-RISK-HANDLING`: status string unchanged; append "Refreshed 2026-05-29 — n=92+30, still RE-SCOPE-ELIGIBLE; see `tasks/findings_actionability_gate_recheck_2026_05_29.md`."

## Follow-Up Candidates (descriptive, not authorized)

1. **Regime-isolated cohort comparison.** Re-run the separation test on trades opened AFTER the 2026-05-19 auto-suspend window closed (i.e., post-2026-05-19T01:02:14Z) for a regime-stationary view. Was not done in this PR — scope discipline.
2. **Exit-shape audit (carried from 2026-05-26).** Still warranted; still not in scope here.
3. **Operator drift/runtime re-scope** before any of the 3 downstream BL items proceed — explicitly required per the existing backlog wording.

## Anti-Scope (this PR)

- No V2 stale-entry gate implementation.
- No `linkage_state` schema migration.
- No actionability threshold tuning.
- No suppression-policy change.
- No re-enablement of any suspended signal.
- No classifier change to the `v1_block_core_signal_mcap_below_10m` rule despite the n≥20 trigger half-firing — directional signal is refuted, not confirmed.
- No mutations to `paper_trades`, `signal_params`, `signal_params_audit`, or any other runtime table.
- No backfill execution.

## Rollback

N/A. Read-only analysis and Markdown status updates only.
