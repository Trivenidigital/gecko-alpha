**New primitives introduced:** NONE — pure dashboard view addition reading existing `paper_trades.would_be_live` column shipped via BL-NEW-LIVE-ELIGIBLE (PR #98, 2026-05-11). No new Settings, no schema migration, no new DB writes, no behavior change. Read-only frontend toggle + backend aggregation query.

# Plan: Dashboard `would_be_live=1` cohort comparison view

## Context

BL-NEW-LIVE-ELIGIBLE shipped the writer for `paper_trades.would_be_live` (`8a07662`, deployed 2026-05-11T13:22Z). Memory: `project_live_eligible_writer_shipped_2026_05_11.md`. The writer stamps each paper-trade open with the Tier 1/2 live-eligibility outcome — pure observability, no production behavior change.

The "Follow-up items (NOT in this PR)" list at the end of that BL entry (line 481) lists **"Dashboard surface for `would_be_live=1` cohort PnL"** as the natural next step. This plan executes that follow-up.

## Goal

Add a read-only cohort-toggle view to the dashboard so the operator can compare full-paper-cohort performance against the `would_be_live=1` subset (the cohort that would have actually traded under live-trading FCFS-20-slot capital constraints). This is the apples-to-apples answer to *"would live trading have worked?"* — which is a structurally different question than *"do paper signals make money?"*

## Discussion-derived framing

The conversation that produced this plan (2026-05-12) sharpened the framing into two distinct empirical questions:

- **Q1: Is the `would_be_live=1` cohort a better predictor of live performance than the full paper cohort?** Empirical, almost-certainly-yes-but-verify question. Capital constraints change which trades happen; slot competition changes timing; FCFS-20 changes the distribution of which signals get exercised.
- **Q2: Is `would_be_live=1` evaluation worth the statistical cost of smaller n?** Harder question; answer depends on decision-blast-radius of what the evaluation will be used for.

Dashboard view (this plan) is **read-only**, low-blast-radius, and reversible to zero cost — and it is also the *evidence-generation step* that lets Q1 be answered empirically over a 4-week window. Subsequent options (auto-suspend gates against =1 cohort, calibration weighting, alert routing) are deferred until this view produces the evidence base.

## Scope

### In scope

1. **Backend aggregation query** (`dashboard/api.py` or `dashboard/db.py`): return per-signal_type win-rate + net-PnL for two cohorts side-by-side:
   - Full paper cohort (`status LIKE 'closed%'`, all `would_be_live` values)
   - Live-eligible cohort (`status LIKE 'closed%' AND would_be_live = 1`)
   - Time window: configurable (7d default, last-30d available)

2. **Frontend toggle / side-by-side view** (`dashboard/frontend/`): operator can:
   - Default to full-cohort view (do NOT default to =1 view — see Anchoring note below)
   - Toggle to live-eligible-only view
   - See side-by-side comparison view with both metrics + delta

3. **Excluded signal_types display** (visibility-not-hiding): show which signal_types have structurally-empty eligible subsets and *why*. Example: `trending_catch: excluded, structural stack cap = 1, not in Tier 1a/2a/2b enumeration`. Same shape as §2.11's heartbeat-reports-healthy visibility rule — *visibility into why something isn't being measured matters as much as the measurement itself.*

4. **Small-n caveat inline**: dashboard surface notes the eligibility rate (~5-10% steady-state) and that signal-type breakdowns require 4+ weeks before confidence intervals tighten.

### Explicitly out of scope

- Any change to auto-suspend logic (the (2) question)
- Any change to calibration / combo_performance weighting (the (3) question)
- Any change to alert routing (the (4) question)
- Any change to the writer itself (`scout/trading/live_eligibility.py`)
- Any backfill of historical `would_be_live` values (existing 1223 NULL rows stay NULL — they're stamped only post-deploy)

These are deferred until the dashboard view's 4-week evidence answers Q1.

## Pre-registered success criterion (filed BEFORE building, per §11b discipline)

**Evaluation cohort:** the four signal_types eligible to produce `would_be_live=1` trades:
- `chain_completed` (Tier 1a)
- `volume_spike` (Tier 2a)
- `gainers_early` with mcap ≥ `PAPER_TIER2_GAINERS_MIN_MCAP_USD` AND chg24 ≥ `PAPER_TIER2_GAINERS_MIN_24H_PCT` (Tier 2b)
- Any signal_type that produces ≥10 closed trades with `conviction_locked_stack >= 3` (Tier 1b — empirical, may include narrative_prediction or losers_contrarian if they stack)

**Exclusion clause** (added 2026-05-12 post-Step-1 verification): signal_types with **structurally non-stackable** signal_data sources are excluded from the eligible-subset evaluation — their eligible subset is structurally empty, not a measurement we can usefully make. Confirmed instances from Step 1:
- `trending_catch` — fires alone from `trending_snapshot`; max stack = 1; not in Tier 1a/2a/2b
- `first_signal` — momentum_ratio + cg_trending_rank pair; max stack = 2; not in Tier 1a/2a/2b

Future signal_types with the same property (single-source, not tier-enumerated) are excluded by the same rule.

**Window:** 4 weeks from dashboard deployment.

**Metrics tracked separately** (per the refined-framing turn 2026-05-12):
- Win-rate (% closes with pnl_usd > 0)
- Net PnL (USD)

**Divergence classification per signal_type:** both metrics evaluated independently; **agreement required** before declaring strong divergence:
- **Strong divergence** — both win-rate gap >15pp AND PnL sign flip (eligible subset positive while full cohort negative, or vice versa). Justifies scoping (2) / (3) for that signal_type.
- **Moderate divergence** — either win-rate gap 5-15pp OR PnL sign flip alone. Record; do not act.
- **Win-rate flip alone, PnL agrees** — eligible subset changes *which* trades close green/red without changing economic outcome. Interesting; not actionable for signal evaluation. May be actionable for risk-management framing.
- **PnL flip alone, win-rate agrees** — eligible subset changes the tail shape (rare big winners absent or concentrated). More actionable for risk-management than for signal evaluation.
- **Tracking (<5pp win-rate AND PnL same-sign)** — full-cohort evaluation is fine for that signal_type. (2) / (3) is scope creep.

**Decision points at 4-week mark:**
- If ≥1 signal_type shows strong divergence (both metrics agree, same direction) → scope (2) and/or (3) for that signal_type, with explicit n caveats and recalibrated thresholds for small-n cohorts.
- If only moderate or split divergences → continue observation; do not act.
- If all signal_types track → full-cohort evaluation is empirically validated; close the Q1/Q2 thread; do not pursue (2) / (3).

## Anchoring discipline

The dashboard MUST default to the full-cohort view. Reasoning: a smaller-n view shown by default risks the operator casually reading numbers that have wide confidence intervals and treating them as decisive. The toggle to live-eligible-only is an *explicit operator choice* — the act of toggling acknowledges the small-n caveat.

This is the same anchoring concern as the §2.9 silent-rendering trap: defaults shape interpretation more than content does.

## Build plan

### B1 — Backend aggregation

`dashboard/api.py` (or `dashboard/db.py` if that's where the existing PnL aggregator lives — verify at build time):

- Endpoint: `GET /api/pnl-by-cohort?window={7d|30d}` returning:

  ```json
  {
    "window": "7d",
    "full_cohort": {
      "signal_types": [{"signal_type": "gainers_early", "n": 107, "wins": 76, "win_pct": 71.0, "total_pnl_usd": 1615.73, ...}, ...]
    },
    "eligible_cohort": {
      "signal_types": [{"signal_type": "gainers_early", "n": 12, "wins": 8, "win_pct": 66.7, "total_pnl_usd": 245.10, ...}, ...]
    },
    "excluded_signal_types": [
      {"signal_type": "trending_catch", "reason": "structural stack cap = 1; not in Tier 1a/2a/2b enumeration"},
      {"signal_type": "first_signal", "reason": "structural stack cap = 2; not in Tier 1a/2a/2b enumeration"}
    ]
  }
  ```

- Exclusion list is **derived**, not hardcoded — query identifies signal_types where:
  - `MAX(conviction_locked_stack) < 3` over all-time closed trades, AND
  - `signal_type NOT IN ('chain_completed', 'volume_spike', 'gainers_early')`

  This makes the list self-updating as new signals are added.

### B2 — Frontend toggle

`dashboard/frontend/` (Vite + JSX per the package.json layout):

- Three-tab view in the existing PnL section: `Full cohort` (default) | `Live-eligible only` | `Side-by-side`
- Each tab renders the existing PnL-by-signal_type table with cohort-appropriate data
- Side-by-side adds delta columns (win-rate delta, PnL delta)
- Below the table: collapsible "Excluded signal_types" section listing each excluded type + reason

### B3 — Small-n caveat + eligibility-rate counter

**Static text below the toggle:** "Live-eligible cohort is typically 5-10% of paper-trade volume; signal-type breakdowns require ≥4 weeks before win-rate confidence intervals tighten."

**Eligibility-rate counter (added 2026-05-12 post-BILL-verification):** when the live-eligible toggle is on, display an explicit count beside the table header: `Showing N of M trades (X% live-eligible)` where N = eligible-cohort row count, M = full-cohort row count, X = N/M × 100. Empirically the toggle hides ~95% of recent activity; making the missing trades explicit prevents an operator surprised by "where did all my trades go?" from misreading the smaller table.

Same anchoring concern as §B3's small-n caveat — defaults and visible counters shape interpretation more than content does. Operator UX risk is real: 264 trades collapsing to 12 on toggle without a count beside it reads as "view broke" rather than "filter applied."

Concrete shape: `Showing 12 of 264 trades (4.5% live-eligible) — toggle off to see full cohort`. Stub text is fine for B3; final wording can be refined post-deploy.

### B4 — Verification

- Backend: unit test for the aggregation query (mock paper_trades fixtures with mixed `would_be_live` values; assert both cohorts compute correctly + exclusion list is derived not hardcoded).
- Frontend: manual smoke test in browser; verify default tab is full cohort, toggle works, excluded section displays both `trending_catch` and `first_signal` with structural reasons.
- E2E: deploy to VPS, refresh dashboard, confirm toggles work against live `scout.db`.

## Implementation order

1. (~30 min) Locate existing PnL aggregation code in `dashboard/api.py` / `dashboard/db.py`. Read the existing pattern.
2. (~45 min) B1 — backend aggregation query + exclusion-list derivation + unit test.
3. (~45 min) B2 — frontend three-tab toggle + side-by-side delta columns + excluded-signal-types section.
4. (~15 min) B3 — small-n caveat text.
5. (~15 min) B4 — verification (unit + manual smoke).
6. (~15 min) PR + reviewer dispatch.
7. (~15 min) Deploy to VPS (the same path used for PR #98 deploys).

**Estimate: ~3 hours wall clock, including review + deploy.**

## Revert

Pure additive; revert is a file-level revert of the `dashboard/api.py` and `dashboard/frontend/` changes + frontend rebuild. No DB cleanup. No `.env` knobs needed.

## What this does NOT close

- (2) auto-suspend-against-=1-cohort scoping — explicitly deferred until 4-week dashboard evidence
- (3) calibration weighting against =1 cohort — same
- (4) alert routing against =1 cohort — same
- BL-NEW-LIVE-EVALUABLE-SIGNAL-AUDIT — separate backlog entry, fires at the next live-trading roadmap revisit
- The live-trading enablement decision itself — gated on BL-055 unlock per memory `project_bl055_deployed_2026_04_23.md`

## Forward-looking pre-registration recall

At the 4-week mark (~2026-06-09), pull this plan, run the divergence classification against accumulated data, and write findings doc `tasks/findings_dashboard_live_eligible_4week_eval_2026_06_09.md`. If any signal_type shows strong divergence, scope (2)/(3) at that point — *not before*.
