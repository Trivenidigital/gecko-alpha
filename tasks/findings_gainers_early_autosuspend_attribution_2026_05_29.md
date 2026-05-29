# gainers_early Auto-Suspend Attribution Findings (2026-05-29)

**Backlog item:** `BL-NEW-GAINERS-EARLY-2026-05-19-AUTOSUSPEND-ATTRIBUTION`
**Audit scope:** read-only diagnostic. No re-enable, no policy change, no code change in this PR.

## Pinned Production Cutoffs (NOT merge dates)

Both events come directly from `signal_params_audit` (not git log, not memory):

| Event | Timestamp (audit row) | applied_by |
|---|---|---|
| KEEP_ON soak verdict (n=128) | `2026-05-13T04:05:02.142Z` (audit id=24) | `operator` |
| hard_loss auto-suspend (n=251) | `2026-05-19T01:02:14.744149+00:00` (audit id=28) | `auto_suspend` |
| joint `tg_alert_eligible` flip | `2026-05-19T01:02:14.744149+00:00` (audit id=29) | `auto_suspend` |

Elapsed: ~5 days 21 hours. New trades closed in window: 123.

## Audit Reasons (verbatim from signal_params_audit)

- **id=24 (KEEP_ON)**: `7d post-BL-NEW-AUTOSUSPEND-FIX soak closure. Baseline 2026-05-06T02:15:23Z. n=128 net +894.37 per_trade +4.80 win 72.7pct. Gate: >=+00 net OR +/trade — both cleared. Auto-suspend fires during soak: 0. conviction_lock_enabled=1 stays armed.`
- **id=28 (hard_loss)**: `hard_loss: net $-368, drawdown $-2509 (n=251)`

Net swing across the window (audit-cumulative): +$894 → -$368 = **-$1,262 net loss accumulated**, with peak-to-trough drawdown -$2,509 reported.

**Reconciliation note:** The window-only query (123 closed trades, net -$2,263) and the cumulative-delta number (-$1,262) differ by ~$1,000 because the n=128→n=251 cohort math is over *trade-close* events. Trades opened pre-window but closed inside the window count toward window-close (123); trades opened inside the window but not yet closed are not included. The cumulative net peaked above +$894 (~+$2,141) before crashing — explaining how a -$2,263 window-only net produces a -$1,262 cumulative-delta with a -$2,509 drawdown.

## Cause-by-Cause Investigation

### Cause 1 — Real regime shift

**Verdict: DOMINANT CAUSE — strong evidence.**

Trades closed in window `2026-05-13T04:05:02 → 2026-05-19T01:02:14`, all signals:

| signal_type | n | net | avg | win% |
|---|---|---|---|---|
| gainers_early | 123 | -$2,262.70 | -$18.40 | 43.1% |
| losers_contrarian | 89 | -$1,671.57 | -$18.78 | 44.9% |
| narrative_prediction | 13 | -$95.55 | -$7.35 | 53.8% |
| chain_completed | 5 | -$281.32 | -$56.26 | 20.0% |
| volume_spike | 4 | -$219.38 | -$54.84 | 25.0% |
| trending_catch | 4 | -$263.08 | -$65.77 | 0.0% |
| tg_social | 1 | -$1.88 | -$1.88 | 0.0% |

**Every signal had net loss in the same window.** `losers_contrarian` per-trade loss (-$18.78) was nearly identical to `gainers_early` per-trade (-$18.40). Regime-wide effect, not gainers_early-specific.

Why did `losers_contrarian` (similar bad outcomes) NOT trigger hard_loss? Cumulative-pre-window state likely differed: `losers_contrarian` had built up a larger positive cushion before the regime turn, so the gate's net-floor check did not fire. The audit reason on id=28 references cumulative metrics (`net $-368, drawdown $-2509 (n=251)`), not window-only metrics.

### Cause 2 — Hard-loss gate overfire

**Verdict: NOT supported.**

The gate fired on `net $-368 cumulative` with `drawdown $-2509` at `n=251`. Both numbers represent real signal degradation:
- The -$368 cumulative net means the entire +$894 KEEP_ON baseline plus another -$1,262 of new loss was absorbed.
- The -$2509 peak-to-trough drawdown implies the cumulative net peaked higher than +$894 during the window (around +$2,141) before crashing — i.e., the gate fired AFTER a sustained drop, not on a single bad day.

Per memory `project_bl_autosuspend_fix_2026_05_06.md`, the combined-gate hard_loss + drawdown_baseline_at was shipped 2026-05-06 (PR #79) with explicit revival helper. The gate did exactly what its design specifies on data this magnitude.

### Cause 3 — Code / config change between 2026-05-13 and 2026-05-19

**Verdict: RULED OUT for negative-pnl causation.**

`git log --since=2026-05-13 --until=2026-05-19T01:02:14Z -- scout/trading scout/signals scout/auto_suspend scout/scoring scout/main.py` returned no commit touching:
- gainers_early signal logic (entry criteria, scoring weights)
- stop_loss / trailing_stop / peak_fade / floor exit logic
- hard_loss gate thresholds or trigger conditions

The mid-window code changes that DID land:
- **PR #170** (`fix(coingecko): reorder lanes so held_position runs first`) — deployed mid-window. Fixes stale-price exits (would IMPROVE outcomes post-fix).
- **PR #150** (`feat(revival-criteria): tighten LC revival decision machinery`) — touches revival path (post-suspend re-enable logic), NOT suspend trigger logic.
- **PR #141** (cohort digest), **PR #140** (SQLite WAL probe), **PR #129** (rate limiter), prune work — observability + infrastructure, not trade behavior.

A grep for `gainers_early|hard_loss|auto_suspend|stop_loss` commit messages between 2026-05-06 and 2026-05-19 returned `e8758b57 fix(auto-suspend): §2.9 silent-rendering parse_mode + §12b CLAUDE.md rule (#106)` — this fixed the **Telegram-render** of auto-suspend alerts (parse_mode escaping), NOT the gate's trigger logic.

No code/config change credibly explains the negative outcomes.

### Cause 4 — Price / exit bug

**Verdict: NOT a bug. Stop-loss policy correctly triggered.**

Exit-reason breakdown in the window (gainers_early, 123 closed):

| exit_reason | n | net | avg/trade |
|---|---|---|---|
| stop_loss | 32 | -$2,632.15 | -$82.25 |
| peak_fade | 18 | +$1,004.01 | +$55.78 |
| trailing_stop | 33 | +$607.76 | +$18.42 |
| expired | 19 | -$871.43 | -$45.86 |
| expired_stale_price | 12 | -$326.98 | -$27.25 |
| floor | 9 | -$43.92 | -$4.88 |

- **Positive exits** (peak_fade + trailing_stop): 51 trades, +$1,612 net — the exit logic was working as designed.
- **stop_loss dominates negative outcomes**: 32 trades at -$82.25 avg = -$2,632 net. This is the cumulative effect of adverse price moves hitting SL thresholds. Not a bug — it's the SL policy correctly cutting losing trades. The bug would be if stops did NOT fire, leaving losers open.
- `expired` (19 / -$871) is `max_duration` reached without TP/SL — these are trades that ran their course without resolving.

### Cause 5 — Stale-price issue

**Verdict: MINOR contributor (~14% of negative outcomes), and the fix landed mid-window.**

12 trades exited via `expired_stale_price` for -$327 net (-$27.25/trade) — these are trades where the held-position price_cache went stale past the staleness threshold, causing forced exits.

Held-position-refresh enable (per `tasks/plan_held_position_price_freshness.md` / `HELD_POSITION_PRICE_REFRESH_ENABLED`, operator-flipped per prior-session audit) was active starting **2026-05-18T16:16:07Z**, which falls INSIDE the window (~9 hours before the auto-suspend fire). PR #170 (lane-order fix) also landed mid-window.

Implication: trades opened before 2026-05-18T16:16Z carry the stale-price exposure; trades opened after benefit from the fix. The 12 `expired_stale_price` exits are concentrated in the pre-fix portion of the window. This cause was structurally closing as the window progressed — it does not call for a NEW fix in this PR.

## Attribution Summary

| Cause | Verdict | Magnitude |
|---|---|---|
| Real regime shift | **DOMINANT** | Every signal had negative net; losers_contrarian -$18.78/trade ~ gainers_early -$18.40/trade |
| Hard-loss gate overfire | **NOT SUPPORTED** | Gate fired on real cumulative degradation, not phantom |
| Code/config change | **RULED OUT** | No commit touching gainers_early entry/exit/gate logic in window |
| Price/exit bug | **NOT A BUG** | stop_loss policy correctly triggered; exit logic working |
| Stale-price | **MINOR (~14%)** | 12 trades / -$327; fix already enabled mid-window (2026-05-18T16:16Z) |

**Headline:** The 2026-05-19T01:02:14Z auto-suspend was JUSTIFIED. gainers_early genuinely degraded across the window due to a regime shift that hit all signals. The hard_loss gate did its job correctly. No bug, no overfire, no missed code regression.

## Build Follow-up Decision

**No build follow-up to "fix" the auto-suspend is warranted.** The gate fired correctly on real signal degradation; the underlying cause (regime shift) is not a code defect.

Three related items that are NOT scope for this PR but worth flagging:

1. **Re-enablement path is OPERATOR DECISION + revival criteria.** Per PR #150 (revival evaluator) and `project_lc_revival_criteria_shipped_2026_05_17.md`, the revival path is data-bound (Wilson LB + bootstrap + cohort stratification). Today's signal_params still shows `enabled=0, suspended_reason=hard_loss, updated_at=2026-05-19T01:02:14.744149+00:00`. Re-enablement is operator-authorized via the revival evaluator, NOT a fix-PR — outside this PR's scope per the "no re-enable" constraint.

2. **Inverse-attribution worth filing as backlog**: why did losers_contrarian (-$1,672 net in window) NOT trigger hard_loss when its per-trade loss magnitude was nearly identical to gainers_early? Likely cumulative-pre-window cushion differential, but worth measuring to validate the gate's per-signal calibration. Suggested: `BL-NEW-AUTOSUSPEND-CROSS-SIGNAL-CALIBRATION-AUDIT` (read-only, n=251 vs n=89 cumulative comparison).

3. **Regime-shift telemetry**: in a future cycle, a per-cohort regime indicator (already pinned NOT to be the Today's Focus market-context strip per PR-D anti-scope) could give the operator earlier signal of regime-wide degradation. This would be a separate proposal, not a fix follow-up.

## Backlog Status Updates

- `BL-NEW-GAINERS-EARLY-2026-05-19-AUTOSUSPEND-ATTRIBUTION` → **SHIPPED-WITH-VERDICT-AUTOSUSPEND-JUSTIFIED** (this PR's deliverable).
- Filed: `BL-NEW-AUTOSUSPEND-CROSS-SIGNAL-CALIBRATION-AUDIT` (proposed, low priority — investigates per-signal gate calibration differential, no build implication today).

## Anti-Scope (this PR)

- No code changes (read-only diagnostic).
- No re-enablement of gainers_early.
- No threshold tuning on the hard_loss gate.
- No new auto-suspend logic.
- No removal of any audit row.
- No mutations to `signal_params` or `signal_params_audit`.
- Re-enablement is operator-authorized via the existing revival evaluator (PR #150), not via a fix-PR.
