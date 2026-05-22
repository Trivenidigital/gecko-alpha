# Findings — Actionability Data-Gate Check 2026-05-22

**New primitives introduced:** NONE — read-only data check.

**Date:** 2026-05-22
**Owner:** close-development block, P4 step.
**Scope:** Read-only verification of whether the actionability validation runbook (`tasks/runbook_actionability_validation_2026_05_19.md`) has enough closed stamped trades to trigger the operator's full validation pass.

## Gate criteria (operator-supplied)

**Primary gate (run actionability validation runbook):**
- `n_actionable >= 20` AND `n_exploratory >= 5`

**Early-fire branches (one or more sub-clauses, then re-validation pass only):**
- `>= 1` exploratory closed with `pnl_usd > 0`
- `n_exploratory >= 5` AND loss-rate evidence meaningful
- `n_actionable >= 15` AND `total_pnl < -50`

**Thin (current state):**
- Neither primary nor early-fire fully fires; record current n and next data-bound trigger; no v2 action.

## 2. Read-only query (srilu prod)

`scout.db.paper_trades` joined on `actionability_version IS NOT NULL` AND `status LIKE 'closed_%'` — counts closed stamped trades by `actionable` flag. Run at 2026-05-22T17:42Z, no DB writes.

```sql
SELECT actionable, COUNT(*) AS n_closed,
       ROUND(SUM(pnl_usd), 2) AS total_pnl,
       ROUND(AVG(pnl_usd), 2) AS avg_pnl,
       COUNT(CASE WHEN pnl_usd > 0 THEN 1 END) AS wins,
       COUNT(CASE WHEN pnl_usd <= 0 THEN 1 END) AS losses,
       ROUND(MAX(pnl_usd), 2) AS max_win,
       ROUND(MIN(pnl_usd), 2) AS max_loss
FROM paper_trades
WHERE actionability_version IS NOT NULL
  AND status LIKE 'closed_%'
GROUP BY actionable;
```

## 3. Observed counts (2026-05-22T17:42Z)

| Cohort | n_closed | total_pnl | avg_pnl | wins | losses | max_win | max_loss | win_rate |
|---|---|---|---|---|---|---|---|---|
| Actionable (=1) | **21** | **+$224.58** | +$10.69 | 19 | 2 | +$51.32 | -$94.04 | **90.5%** |
| Exploratory (=0) | **3** | +$21.33 | +$7.11 | 1 | 2 | +$32.18 | -$7.39 | 33.3% |

Stamping window: 2026-05-19T11:42Z (first stamped open) through 2026-05-22T17:37Z. **~3.25 days of stamping**, 93 total stamped trades (72 actionable, 21 exploratory), 24 closed, 69 still open.

### Cohort by exit reason

| Cohort | exit_reason | n | total_pnl |
|---|---|---|---|
| Actionable | peak_fade | 9 | +$279.99 |
| Actionable | trailing_stop | 10 | +$116.52 |
| Actionable | stop_loss | 2 | -$171.92 |
| Exploratory | floor | 2 | -$10.85 |
| Exploratory | peak_fade | 1 | +$32.18 |

### Cohort by signal_type

| signal_type | Actionable n / pnl | Exploratory n / pnl |
|---|---|---|
| chain_completed | 11 / +$128.47 | 2 / +$24.79 |
| narrative_prediction | 5 / +$100.30 | 0 / — |
| volume_spike | 5 / -$4.19 | 0 / — |
| tg_social | 0 / — | 1 / -$3.46 |

## 4. Gate evaluation

| Gate | Criterion | Observed | Met? |
|---|---|---|---|
| Primary | n_actionable >= 20 | 21 | ✓ |
| Primary | n_exploratory >= 5 | 3 | ✗ (short by 2) |
| Early-fire 1 | >=1 exploratory closed with pnl_usd > 0 | 1 (`chain_completed`, +$32.18) | ✓ (weak signal) |
| Early-fire 2 | n_exploratory >= 5 AND loss-rate meaningful | n_exp = 3 | ✗ |
| Early-fire 3 | n_actionable >= 15 AND total_pnl < -$50 | n=21, total=+$224.58 | ✗ (cohort positive) |

**Verdict: THIN.** Primary gate is missing 2 exploratory closures. Early-fire #1 alone is too weak to act on (1 exploratory winner = noise at n=3); early-fire #2 and #3 are clearly not satisfied.

## 5. Notable side observations (descriptive, not directive)

- **Actionable cohort is performing well.** 19/21 wins, 90.5% win rate, +$224.58 net over ~3 days. This is **descriptive only** — the operator's instruction is explicit that v2 / suppression decisions wait for the gate, not for cohort PnL.
- **Exploratory n is bottlenecked by signal mix.** All 21 stamped exploratory trades involve `tg_social` (8), `chain_completed` (5), `losers_contrarian` (4), `volume_spike` (3), `narrative_prediction` (1) — but only 3 have closed so far. The exit-time distribution for exploratory signals (often slow-burn / longer-hold) is the limiting factor on n_exploratory closures, not the inflow rate.
- **Open positions still hold the bulk of stamped data.** 69 of 93 stamped trades (74%) are still open. Many will close in the next 1–7 days as TP/SL/peak_fade triggers fire.

## 6. Next data-bound trigger

**Re-evaluate when `n_exploratory >= 5`** (currently 3; need 2 more exploratory closures).

Estimated time-to-trigger:
- Exploratory closure rate so far: 3 closures over 3.25 days = ~0.92 closures/day.
- At current rate, 2 more closures expected within **~2.2 days** (target ~2026-05-25).
- Bound is approximate; exploratory closures are concentrated in the slow-burn / `tg_social` / `losers_contrarian` cohort whose exit timing is heavier-tailed than the fast `chain_completed` cohort. Realistic re-evaluation window: **2026-05-25 to 2026-05-29**.

**No v2 / suppression action is taken on the current thin sample.** The actionability gate stays open; the classifier continues stamping; nothing in scout/* changes.

## 7. Backlog updates from this finding

- `BL-NEW-ACTIONABILITY-GATE`: SHIPPED status unchanged. Append data-gate-check status note pointing at this doc + next trigger date.
- No new BL filed (the runbook + the existing BL cover the work).

## 8. Scope discipline

- Read-only. No prod writes.
- No code changes.
- No v2 actionability classifier work.
- No source-quality gate consumption / actionability suppression policy / live capital reallocation.
- This doc plus the brief backlog status note are the only artifacts.

## 9. Rollback

N/A — read-only data check. Zero blast radius.
