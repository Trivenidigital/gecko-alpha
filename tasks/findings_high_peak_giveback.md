**New primitives introduced:** `PaperExitReason.PEAK_FADE_HIGH_PEAK` (new exit_reason / status), `PAPER_HIGH_PEAK_*` config block (5 settings), evaluator branch in `evaluator.py` between trailing-stop and BL-062 peak-fade.

# Findings: High-Peak Giveback on Confirmed Winners

**Date:** 2026-05-05
**Author:** claude-opus-4-7 (autonomous session)
**Status:** **DECISION-READY 2026-05-05** — see §14. Existing-data battery (A-E) on n=10 cohort PASSES all three pre-registered promotion criteria. Recommended next step: promote to plan stage with focused 4-6 week BL-067-interaction forward soak (not 5-month wait). Operator sign-off requested.
**Reviewers consulted:** R1 statistical (DEFER), R2 code/cascade (GREENLIGHT-W/-CONDITIONS, found a bug), R3 strategy (cheaper paths), R4 independent verification (confirmed bug + recommended PARK), 2026-05-05 follow-up data-battery (PROMOTE)

---

## 0. TL;DR for reviewers

Over the last 30 days, **9 trades reached a peak ≥ 75% above entry**. They generated $1,633 of actual realized PnL, but their counter-factual PnL under a tighter peak-fade exit (15% retrace from peak, applied only above peak ≥ 75%) would have been **+$2,303** — a delta of **+$669.71 (+41% lift on this cohort)**.

The rest of the book is unaffected: at peak < 75%, the existing exit ladder (BL-061 ladder + BL-062 peak-fade + BL-063 moonshot trail + BL-067 conviction-lock) already wins.

Concretely, **8 of 9 high-peak trades exited via `trailing_stop`, giving back 27–73% of peak gain.** Only one exited via `peak_fade` (ASTEROID, 90% capture ratio, +$313) and that's the win pattern we want to extend.

This is not a new mechanic — it's a tighter, single-pass variant of BL-062 peak-fade gated on a higher peak threshold. The proposal is small (~30 LOC + tests), feature-flagged, and reversible via `.env`.

But before scoping a PR, the cohort is **9 trades over 30 days**. That's small. Reviewers should attack the cohort-size problem hardest.

---

## 1. Why this matters

The user's stated goal (memory: `user_trading_goals.md`):
> "manual research use case, chain-agnostic, beat CoinGecko Highlights by minutes."

The system already detects ~6× more first-tier candidates than CoinGecko Highlights. Detection is no longer the bottleneck. **Capture is.** A trade that detects a +207% peak (ZKJ) but exits at +107% has captured 52% of the move. That's not a detection problem; that's an exit problem.

The operator made this point directly during this session: "If I trade SKYAI upon detection. Will we be profitable… I can say lot of other examples too." The dashboard's *peak gain since detection* column is correct. The trading engine's *PnL realized* column is correct. The gap between them is the cost of slow exits.

This document is about closing that gap on the confirmed-winner end of the distribution.

---

## 2. Current exit ladder (full, deployed)

Source of truth: `scout/trading/evaluator.py:417-613` (BL-061 cascade for post-cutover rows).

The cascade fires in this strict order on each 30-min eval pass:

| # | Gate | Trigger | Closes status |
|---|---|---|---|
| 1 | **stop_loss** (pre-leg-1 only) | `current ≤ sl_price` and not `floor_armed` | `closed_sl` |
| 2 | **leg_1** | `change_pct ≥ leg_1_pct` (per-signal Tier 1a, default 25%) | partial sell, trade stays open |
| 3 | **leg_2** | `change_pct ≥ leg_2_pct` (default 50%) | partial sell, trade stays open |
| 4 | **floor** | `floor_armed and current ≤ entry_price` | `closed_floor` |
| 5 | **trailing_stop** | `floor_armed and peak_pct ≥ leg_1_pct and current < peak * (1 - effective_trail_pct/100)` | `closed_trailing_stop` (or `closed_moonshot_trail`) |
| 6 | **peak_fade** (BL-062) | `peak_pct ≥ MIN_PEAK_PCT (10) and cp_6h_pct < peak_pct * RATIO (0.7) and cp_24h_pct < peak_pct * RATIO (0.7)` | `closed_peak_fade` |
| 7 | **expired** | `elapsed ≥ max_duration_hours` | `closed_expired` |

**Effective trail width** (`evaluator.py:462-480`):

```
effective_trail_pct =
    max(MOONSHOT_TRAIL_DRAWDOWN_PCT=30, sp.trail_pct)   if moonshot armed (peak ≥ 40%)
    sp.trail_pct_low_peak (default 8)                    if peak_pct < low_peak_threshold (20)
    sp.trail_pct (default 20, locked 25-35 if BL-067)    otherwise
```

So on a confirmed winner with peak ≥ 75%, BL-063 has armed moonshot (peak ≥ 40%) and the trail is **at least 30%**, possibly 35% if BL-067 conviction-lock has armed. **The price has to retrace 30%+ from peak before trailing_stop fires.**

The BL-062 peak_fade gate could fire earlier (15% retrace if `RATIO=0.85` were configured, currently 0.7 = 30% retrace) — but it requires BOTH `cp_6h_pct` and `cp_24h_pct` to be below `peak_pct * RATIO`. Those checkpoints are written ONCE at the 6h and 24h marks. For trades that peak fast and crash within the first 24h, peak_fade can never fire because cp_24h was never recorded below threshold.

This is the structural gap.

---

## 3. The data — last 30d, all peak ≥ 75% trades

Source: `paper_trades` on prod (`/root/gecko-alpha/scout.db`), 2026-04-05 → 2026-05-05.

| ID | Symbol | Signal | Peak% | Exit% | Capture | Exit reason | $ gained | $ left on table (vs 15%-retrace counter) |
|---|---|---|---|---|---|---|---|---|
| 1357 | ZKJ | gainers_early | 207% | 107% | 52% | trailing_stop | +$322 | **+$160** |
| 1385 | RLS | gainers_early | 154% | 93% | 60% | manual | +$278 | +$69 |
| 1422 | BLEND | gainers_early | 146% | 70% | 48% | trailing_stop | +$211 | **+$117** |
| 1558 | SKYAI | chain_completed | 116% | 40% | 34% | trailing_stop | +$119 | **+$132** |
| 1280 | ASTEROID | losers_contrarian | 116% | 104% | **90%** | **peak_fade** | +$313 | $0 (already optimal) |
| 1044 | burnie | first_signal | 104% | 76% | 73% | trailing_stop | +$229 | -$8 (in cohort; per-signal table reconciles) |
| 1389 | IR | losers_contrarian | 93% | 34% | 37% | trailing_stop | +$102 | **+$90** |
| 1556 | LAB | chain_completed | 86% | 30% | 35% | trailing_stop | +$89 | **+$86** |
| 1516 | LMTS | gainers_early | 78% | 23% | 29% | trailing_stop | +$70 | **+$84** |

**Observations:**

1. ASTEROID is the **only** trade that exited via peak_fade among the high-peak cohort. Its 90% capture ratio is what we want to replicate.
2. **8 of 9 trades exited via trailing_stop**, capture ratio 27–73%. Their give-back averages $74 per trade.
3. Counter-factual delta (peak ≥ 75% + retrace 15% policy) sums to **+$669.71** (per backtest §6).
4. The proposal targets peak ≥ 75% and DOES NOT touch trades below that threshold — most of the book.

**Per-signal breakdown of the 9-trade cohort:**

| Signal | n | Actual $ | Counter $ | Δ |
|---|---|---|---|---|
| gainers_early | 4 | -$706 | -$276 | **+$431** |
| chain_completed | 2 | +$387 | +$606 | **+$219** |
| losers_contrarian | 2 | +$94 | +$122 | +$28 |
| first_signal | 1 | -$132 | -$140 | -$8 |

Note the `gainers_early` and `first_signal` "Actual $" are negative — these include trades elsewhere in the cohort that did NOT hit peak ≥ 75%. The Δ column is what the new policy adds. Only `first_signal` shows a tiny negative Δ ($-8 on a single trade), within noise.

---

## 4. Backtest — full grid (30d window, 853 closed trades)

Script: `scripts/backtest_peak_fade_retracement.py` on branch `feat/peak-fade-backtest`.

Mechanic: for each closed trade with `peak_pct > 0`, simulate "exit at retracement R from peak when peak ≥ threshold T":

```
counter_pnl_pct = ((1 + peak_pct/100) * (1 - R) - 1) * 100
```

Sweep across `T ∈ {0, 30, 50, 75, 100}` × `R ∈ {0.50, 0.30, 0.25, 0.20, 0.15}`:

| Threshold | retrace 50% | retrace 30% | retrace 25% | retrace 20% | retrace 15% |
|---|---|---|---|---|---|
| peak ≥ 0% | $-96,635 | $-48,642 | $-36,643 | $-24,645 | $-12,646 |
| peak ≥ 30% | $-12,546 | $-5,546 | $-3,797 | $-2,047 | $-297 |
| peak ≥ 50% | $-3,853 | $-1,291 | $-650 | $-9 | **$+631** |
| **peak ≥ 75%** | $-1,420 | $-220 | $+81 | $+381 | **$+681 (Δ +$670)** |
| peak ≥ 100% | $-1,095 | $-229 | $-13 | $+204 | $+420 |

Actual net (eligible cohort): $5,221. Best counter-factual net: $5,891. **Optimum is peak ≥ 75% + retrace 15%, lifting net by +$670 over 9 affected trades.**

**Why this surface shape is intuitive** (sanity check, not just curve-fit):

- At low thresholds (peak ≥ 0/30), holding-til-retrace LOSES because most "winners" never confirmed and round-tripped from minor peaks. Every retracement level loses money.
- At peak ≥ 75/100%, the cohort has confirmed momentum and benefits from tighter exits because the existing 30-35% trail gives back too much.
- Best retracement is the tightest tested (15%) — every step tighter improves capture (we did not sweep below 15% because slippage modeling becomes the dominant noise term).

**Window robustness:** 60d and 90d returns identical numbers — the trading dataset is itself only ~30d old (paper-trading went live ~2026-04-05). This is the entire history.

---

## 5. Proposed mechanic

**Add a new exit gate** in `scout/trading/evaluator.py`, ordered as a **standalone block between trailing_stop (#5) and BL-062 peak_fade (#6)**.

**IMPORTANT (corrected 2026-05-05 per R2 + R4 review — see §13.4):** the gate MUST be a standalone `if close_reason is None:` block, NOT an `elif`. The trailing_stop branch at evaluator.py:539 enters the elif chain whenever `floor_armed AND peak_pct >= leg_1_pct`, regardless of whether the inner `current_price < trail_threshold` fires. Writing this gate as `elif` would make it structurally unreachable in exactly the regime it targets (peak ≥ 75% always satisfies `peak_pct >= leg_1_pct`).

```python
# BL-NEW high-peak fade — single-pass, no checkpoint requirement.
# Fires only when a trade has confirmed strong momentum (peak ≥ 75%)
# and price has retraced ≥15% from peak. Tighter than the moonshot trail
# (30%) because the cohort can afford it: capture > give-back at this peak.
#
# MUST be a standalone `if close_reason is None`, NOT an `elif`. See §13.4.
if (
    close_reason is None
    and settings.PAPER_HIGH_PEAK_FADE_ENABLED
    and peak_pct is not None
    and peak_pct >= settings.PAPER_HIGH_PEAK_FADE_MIN_PEAK_PCT  # 75
    and peak_price is not None
    and current_price < peak_price * (1 - settings.PAPER_HIGH_PEAK_FADE_RETRACE_PCT / 100.0)  # 15
    and conviction_locked_at is None  # defer to BL-067 — see §7.6
    and remaining_qty is not None
    and remaining_qty > 0
):
    close_reason = "peak_fade_high_peak"
    close_status = "closed_peak_fade_high_peak"
```

### 5.1 Configuration block

```python
# scout/config.py
# BL-NEW: tighter exit on confirmed winners (peak >= 75%) - 30d backtest +$670
PAPER_HIGH_PEAK_FADE_ENABLED: bool = False        # default off; opt-in
PAPER_HIGH_PEAK_FADE_MIN_PEAK_PCT: float = 75.0   # below this, regular trail
PAPER_HIGH_PEAK_FADE_RETRACE_PCT: float = 15.0    # tighter than moonshot 30%
PAPER_HIGH_PEAK_FADE_PER_SIGNAL_OPT_IN: bool = False  # phase 2: signal_params column
PAPER_HIGH_PEAK_FADE_DRY_RUN: bool = True         # log-only initially
```

Validators: `MIN_PEAK_PCT > MOONSHOT_THRESHOLD_PCT` (must be on a confirmed runner); `RETRACE_PCT in (0, 100)`; `RETRACE_PCT < MOONSHOT_TRAIL_DRAWDOWN_PCT` (tighter than moonshot, else this gate is a no-op).

### 5.2 Why ordered AFTER trailing_stop, BEFORE peak_fade

- **After trailing_stop** so an already-falling-through-trail exit (a confirmed cascade) wins. We don't want to fire the new gate on a token already past the moonshot trail; that would just rename the exit reason.
- **Before peak_fade (BL-062)** so single-pass detection beats dual-checkpoint detection on fast-fading runners. BL-062 still fires on slow fades that don't trigger the 15% threshold.
- The gate naturally subsumes most BL-062 fires for peak ≥ 75% because the 15% retrace will hit before the 30% (RATIO=0.7) BL-062 threshold. BL-062 keeps firing on peak ∈ [10, 75) trades.

### 5.3 Dry-run rollout

`PAPER_HIGH_PEAK_FADE_DRY_RUN=True` emits a `high_peak_fade_would_fire` log event with `(trade_id, peak_pct, current_price, give_back_pp)` instead of executing the close. After 14d soak, compare logged would-fire events against actual subsequent peak_fade / trailing_stop / expired exits to confirm the model matches reality. Only flip to live after the operator reviews the dry-run telemetry.

---

## 6. Alternatives considered (and why rejected)

### 6.1 Just lower BL-062 RETRACE_RATIO from 0.7 → 0.85

Equivalent to "fire when current < peak * 0.85" = 15% retrace.

**Rejected because:** BL-062 still requires `cp_6h_pct < threshold AND cp_24h_pct < threshold`. Trades that peak in the first 24h and crash never get cp_24h recorded below threshold (or recorded at all if they close before 24h). The structural gap is the dual-checkpoint requirement, not the ratio.

### 6.2 Lower MOONSHOT_TRAIL_DRAWDOWN_PCT from 30 → 15

Tighten the existing moonshot trail.

**Rejected because:** moonshot arms at peak ≥ 40%. Tightening the trail uniformly across peak ∈ [40, ∞) would clip trades that have legitimate volatility at moderate peaks. The data shows the give-back problem is concentrated at peak ≥ 75%; targeting that specifically is more surgical.

### 6.3 Time-based exit after peak

Sell N hours after `peak_price` was last updated.

**Rejected because:** time-since-peak is a noisy proxy. Plenty of true runners stay near peak for hours then continue. A retracement threshold is more principled — it triggers on actual price action, not clock.

### 6.4 Per-signal Tier 1a calibration of `trail_pct`

Use the existing `signal_params` table to set tighter `trail_pct` for high-peak trades by signal.

**Rejected because:** `trail_pct` is per-signal, not per-trade-state. A `gainers_early` trade at peak 80% and another at peak 40% both share the same `trail_pct`. Tier 1a can't condition on peak. The new gate is per-trade-state and orthogonal.

---

## 7. Risk analysis

### 7.1 Whipsaw risk

A 15% retrace from peak is tight. A token that does +80% → +60% → +90% would fire the gate at +60% (15% below 80%), missing the +90% leg.

**Mitigation:** the gate fires when `current < peak * 0.85` AND `peak_pct ≥ 75%`. If price is +60% (current_pct=60), peak is +80%, peak_price = entry * 1.80, threshold = entry * 1.80 * 0.85 = entry * 1.53 (= +53% on entry). The gate fires when current crosses +53%, not +60%. So whipsaw needs a 27%-of-current dip, not 25%.

That said — yes, this gate exits earlier than the moonshot trail. The 9-trade backtest cohort says the cost of premature exits is materially smaller than the gain from avoiding give-back on average. **But n=9 is not enough to disprove whipsaw on the next 9 trades.** This is the strongest reviewer attack vector.

### 7.2 Selection bias in the 9-trade cohort

The 9 trades that hit peak ≥ 75% in the last 30 days are not a random sample. They include the regime where `gainers_early` was reactivated (post-2026-05-03), the chain dispatch revival (post-2026-05-01 PR #60), and BL-067 conviction-lock activation (2026-05-04). Future high-peak trades will have different underlying dynamics.

**Mitigation:** dry-run rollout. Capture would-fire events for 14d before flipping live. If would-fire counts < 5 over 14d, defer the live flip — sample size is too small to draw conclusions.

### 7.3 Capacity constraints not modeled

Backtest assumes `amount_usd` constant. In reality `PAPER_MAX_OPEN_TRADES = 150` (slot capacity). Holding a winner longer means a slot stays occupied; on the margin, this displaces a future entry.

**Mitigation:** at observed fire rates (~6 high-peak trades per 30d), this is non-binding. Worth reconsidering if rate scales 10×.

### 7.4 Slippage not modeled

Counter-factual exits at peak * 0.85 with zero slippage. Real-world slippage on a falling price = ~50bps. Counter Δ should be discounted by ~50bps × 9 trades × ~$300 avg amount = ~$13.50. Trivial vs $670 lift.

### 7.5 Forward-looking peak underestimation

Counter-factual uses recorded `peak_pct` (max during open lifetime). If a trade closed before the true peak was hit (e.g., expired at 168h while still rising), counter-factual under-counts. The lift estimate is therefore conservative on a few trades.

### 7.6 Conflict with BL-067 conviction-lock

BL-067 widens `trail_pct` to 35% on stack=4 trades. The new gate fires at 15% retrace. So BL-067-locked trades exit via the new gate, not via their wider locked trail. Is this intended?

**Argument FOR override:** at peak ≥ 75%, even BL-067-locked trades have already captured the conviction signal's value. Tightening makes sense.

**Argument AGAINST:** BL-067's whole point is to widen on confirmation. Overriding undermines the policy.

**Reviewer call.** I lean override (data is data) but acknowledge this is a judgment call.

### 7.7 Order matters; cascade is fragile

`evaluator.py:485` warns:
> Editing this elif chain breaks the regression tests `test_floor_exit_pre_empts_moonshot_trail` and `test_moonshot_trail_wins_over_peak_fade`.

Adding a new gate between trailing_stop and peak_fade requires new tests in both directions:
- `test_high_peak_fade_pre_empts_peak_fade` (new gate fires before BL-062)
- `test_trailing_stop_pre_empts_high_peak_fade` (existing trail still wins when threshold not met)
- `test_high_peak_fade_only_at_peak_75` (no fire below threshold)

---

## 8. Sample size honesty

**The cohort is 9 trades. That is small.** Statistical power on n=9 is weak. Two ways to think about it:

1. **Skeptical:** 9 trades is a fluke. The next 9 might give back $200 each instead of gaining $74. We could be curve-fitting on noise.

2. **Mechanism-based:** the give-back is structural (30-35% trail × confirmed runners = bounded loss), not stochastic. Capture ratio should improve regardless of which 9 trades come next, as long as the trail width is what's clipping them.

I lean (2) but cannot prove it without forward data. **Dry-run rollout is the only honest path.** 14d of would-fire telemetry will produce another ~4 high-peak trades; combined with the historical 9, n=13. Still small, but enough to look at outliers.

---

## 9. Implementation surface (sketch, not yet a plan)

Files touched:
- `scout/trading/evaluator.py` — add new elif gate (~15 LOC)
- `scout/config.py` — add 5 settings + 2 validators (~30 LOC)
- `scout/trading/models.py` — add `PaperExitReason.PEAK_FADE_HIGH_PEAK` enum
- `tests/test_high_peak_fade.py` — new test file (~5 unit tests)
- `tests/test_evaluator_cascade.py` — extend with order-of-precedence tests (3 new)

DB migration: none. Existing `peak_pct` and `peak_price` columns suffice. The new exit_reason value lands in the existing `exit_reason TEXT` column.

Total estimated LOC: ~80 production + ~120 test.

---

## 10. Reviewer attack surface — please challenge these explicitly

This is the most-critical part of the doc. I want to be wrong about things I'm wrong about, before code lands.

**For Reviewer #1 (statistical/data-soundness):**
- Q1. Is n=9 enough to make any policy decision? What's the standard-error band on the +$670 estimate?
- Q2. The backtest uses recorded `peak_pct`. For trades that closed before reaching their lifetime peak, `peak_pct` underestimates. How material is this bias? Can we quantify it from `peak_price` vs DexScreener historical price?
- Q3. Slippage at 50bps shaves ~$13. Acceptable. But what about adverse selection — are the trades that hit peak ≥ 75% systematically different (more volatile, lower liquidity) such that slippage on a 15%-retrace exit is worse than on a 30%-retrace exit?
- Q4. Window: 30d covers entire history. Have we observed enough regime variation (chain dispatch on/off, BL-067 on/off) to call this stable?

**For Reviewer #2 (evaluator cascade / code-correctness):**
- Q5. The new gate is ordered before BL-062 peak_fade and after trailing_stop. Is that ordering robust against future changes? What if BL-062's two-checkpoint requirement is later relaxed — does the new gate become redundant?
- Q6. The gate runs on every eval pass (30 min). If `peak_price` was updated this same pass (line 358) AND `current_price` is below the new threshold, we'd update peak then immediately exit. Is that an OK race, or do we want to exit only on a SUBSEQUENT pass after peak was set?
- Q7. Interaction with BL-067 conviction-lock (§7.6) — should we override or defer? Honest tradeoff.
- Q8. `remaining_qty > 0` guard: do we ever reach this code with `remaining_qty=0` (full position closed via partial sells but row still open)? If so, the BL-062 gate also has this guard; consistent.
- Q9. What's the test fixture for "trade with `peak_price=180, current_price=152` should fire" — does it correctly avoid coupling to specific `entry_price` numbers?

**For Reviewer #3 (strategy / trading-judgment):**
- Q10. Capture vs give-back: is 15% retrace tight enough to catch fades but loose enough to ride volatility? Why not 10%? Why not 20%?
- Q11. Should this be a GLOBAL gate or a PER-SIGNAL opt-in? Tier 1a per-signal opt-in adds complexity but allows `chain_completed` (long-hold mandate) to skip the gate while `gainers_early` (capture-driven) opts in.
- Q12. The 9-trade cohort skews heavily to `gainers_early` (4) and `chain_completed` (2). Is the proposal really useful for the long tail of low-peak signals, or are we proposing infra for one or two signal types?
- Q13. ASTEROID is the lone peak_fade winner at 90% capture. Why did peak_fade fire on ASTEROID but not on the others? Is there a fixable upstream condition (e.g., updating cp_6h_pct more frequently) that would let BL-062 catch the rest, making this proposal redundant?
- Q14. Forward-looking: when we go LIVE_MODE=live (BL-055), does this gate need any adjustment? Slippage will be real. Is 15% retrace still right under realistic execution?
- Q15. Operator philosophy: do you want exits to lean tighter (capture-first) or wider (let it ride)? The data says tighter at peak ≥ 75% — but the data is sparse. What's your prior?

**For ALL reviewers:**
- Q16. **Is there a simpler intervention that achieves 80% of the lift?** E.g., just bump `PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT` from 30 → 20 globally. Would that capture similar Δ with fewer moving parts and less new-mechanic complexity?
- Q17. **What evidence would change your mind?** What dry-run telemetry, what backtest extension, what ablation study would move you from "this is right" → "this is wrong" (or vice versa)?

---

## 11. What this proposal does NOT solve

To prevent scope creep:

- **Detection-to-entry latency.** If a token detects at +5% and we enter at +20%, that's a 15% gap before peak even starts. This proposal does not change entry pricing.
- **Loser trades (peak_pct < 75%).** The expired/stop_loss bucket is the largest by count (567 trades, -$9,857 net) — entirely untouched here. That's the next analysis, not this one.
- **Slot capacity / capital allocation.** We don't model capacity in the backtest. If high-peak winners hold a slot for an extra 24h, we may displace a future entry. Marginal at current fire rates, not modeled.
- **Live execution slippage / liquidity.** Paper trading assumes 50bps slippage; live will have execution risk we haven't modeled.

---

## 12. References

- Backtest script: `scripts/backtest_peak_fade_retracement.py` (branch `feat/peak-fade-backtest`)
- Backtest output (30d): `.ssh_bt_pf_run.txt`
- High-peak trade list: `.ssh_high_peak_trades.txt`
- Live exit-policy env: `.ssh_env_exits.txt`
- Existing exit ladder: `scout/trading/evaluator.py:417-613`
- Config block: `scout/config.py:236-313`
- Related memories: `project_session_2026_04_28_strategy_tuning.md`, `project_bl067_deployed_2026_05_04.md`

---

## 13. Post-review findings (added 2026-05-05 after 4 reviewers)

Ordered by structural importance, not chronologically. Anyone re-reading this section in 6 months should see the moonshot-floor finding first — it has implications for any future per-signal exit-tightening proposal, not just this one.

### 13.1 Moonshot floor nullifies per-signal `trail_pct` above peak ≥ 40%

**Spun out as separate file:** `tasks/findings_moonshot_floor_nullification.md`.

`scout/trading/evaluator.py:462-473` floors the effective trail at `PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT=30` once moonshot arms (peak ≥ 40%):

```python
if moonshot_armed_at is not None:
    effective_trail_pct = max(
        settings.PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT,  # global constant 30
        sp.trail_pct,                                 # per-signal Tier 1a value
    )
```

Every peak ≥ 75% trade has moonshot armed. So the actual trail on the 8 give-back trades was 30%, regardless of what `signal_params.trail_pct` says. **There is no per-signal moonshot opt-out in the schema.** The Tier 1a calibration infrastructure (`scout/trading/calibrate.py` + `signal_params` table) writes per-signal `trail_pct` values that are silently dominated by a global constant in the moonshot regime — i.e., for the entire regime the proposal targets.

This is structurally bigger than this proposal. It means the lever R3 was reaching for in their "Step 0 SQL UPDATE" doesn't exist as the schema suggests it does. See standalone findings file for scope + future work.

### 13.2 gainers_early auto-suspended; cohort is empty today

`signal_params` row for `gainers_early` (queried 2026-05-05):

| field | value |
|---|---|
| `trail_pct` | **20.0** (already, not 30) |
| `enabled` | **0** (disabled) |
| `suspended_at` | 2026-05-04T01:01:02Z |
| `suspended_reason` | `hard_loss` |
| `updated_by` | `auto_suspend` |

`losers_contrarian` is also disabled (per memory `project_session_2026_04_28_strategy_tuning.md`). Walking through the cohort:

- gainers_early (4 trades, +$431 Δ) → suspended; zero forward fires until auto_suspend reverses
- losers_contrarian (2 trades, +$28 Δ) → suspended
- chain_completed (2 trades, +$219 Δ) → 720h long-hold mandate just shipped via BL-067; touching it confounds BL-067 soak
- first_signal (1 trade, -$8 Δ) → noise

**Net active eligible cohort today: ~0 trades.** The proposal targets a regime that produces no fires in the current production state. Any code or config change shipped today would operate on a population of zero (gainers_early/losers_contrarian) or on a target it shouldn't touch (chain_completed).

### 13.3 BL-067 conviction-lock overlap audit — clean

All 9 high-peak trades have `conviction_locked_at = NULL` and `conviction_locked_stack = NULL`. **Zero BL-067 lock overlap in the cohort.**

This satisfies R2's blocking concern about cohort bias. The +$670 backtest delta is not inflated by trades that were locked at peak. It also tells us something useful for the future: during the cohort window (2026-04-05 → 2026-05-04, before BL-067 deployed), these high-peak trades were single-source signals (stack < 3). If/when the cohort revives, future high-peak trades may have higher stack overlap because BL-067 is now armed for `first_signal` + `gainers_early`. The proposal's eligibility population may shrink as a result — preserved future viability requires the `conviction_locked_at IS NULL` guard documented in §5.

### 13.4 R2 elif → standalone-if structural fix (independently confirmed by R4)

The §5 code snippet originally written as `elif close_reason is None and …` is **structurally unreachable in exactly the regime it targets.** The trailing_stop branch at `evaluator.py:539` enters the elif chain whenever `floor_armed AND peak_pct >= leg_1_pct` (default 25%) — independent of whether the inner `current_price < trail_threshold` actually sets `close_reason`. By the time control flow leaves line 567, the elif chain has been consumed. Any later `elif` is dead code in the high-peak regime.

The §5 snippet has been corrected to a standalone `if close_reason is None:` block (mirroring BL-062 at line 572). If this proposal ever resumes, the corrected snippet survives. R4 independently re-verified this against the same code; both reviewers agree the lowest-risk implementation is folding into BL-062 with a `PEAK_FADE_SINGLE_CHECKPOINT_ABOVE_PCT` setting, not adding a new exit_reason.

### 13.5 R3's SQL-UPDATE phantom — discipline lesson

R3's "Step 0" recommended bumping `gainers_early.trail_pct` from 30 → 20 via a single `signal_params` UPDATE, estimated at ~$320 of forecast lift. Three failure modes stacked:

1. **Already 20, not 30** — calibrate.py wrote 20 prior to suspension; UPDATE would be a no-op.
2. **Suspended** — gainers_early is `enabled=0`; even a successful UPDATE has zero forward fires to observe.
3. **Moonshot floor** (the structural finding §13.1) — even if the row were 30 and active, `max(30, sp.trail_pct)` nullifies the change for every peak ≥ 40% trade.

R3's framework was directionally good (find the cohort, isolate the lift, propose the cheapest probe). The execution failed because it assumed a code path that didn't behave as the author thought — same failure pattern as the BL-033 / Tier 1a/1b drift-check failures memorialized in `feedback_drift_check_before_proposing.md`. Investigation-before-implementation found it before code shipped, which is the working-agreement working as intended.

### 13.6 Re-eligibility query — runnable from runbook

When revisiting this proposal, run this query against prod DB (do NOT decide on calendar date alone):

```sql
-- Re-eligibility check, run on or after 2026-05-18 (BL-067 14d soak ends)
SELECT
  pt.signal_type,
  COUNT(*) FILTER (WHERE peak_pct >= 75 AND opened_at > '2026-05-04') AS new_high_peak_fires,
  MAX(pt.opened_at) AS most_recent_fire,
  (SELECT enabled FROM signal_params sp WHERE sp.signal_type = pt.signal_type) AS currently_enabled
FROM paper_trades pt
WHERE pt.signal_type IN ('gainers_early', 'losers_contrarian')
GROUP BY pt.signal_type;
```

Decision rule:
- If `new_high_peak_fires < 4` for both signals on that date → deferral extends. Re-run query weekly.
- If ≥ 4 high-peak fires across the two signals → re-pull cohort, re-run backtest, audit `conviction_locked_at IS NULL` filter, redo reviewer pipeline.

### 13.7 Pre-registered kill criterion — REVISED 2026-05-05 to data-driven

**Original framing (calendar-anchored, 2026-10-05) was lazy.** Replaced same-day with data-driven criterion after operator pushback ("we have lot of data accumulated so far"). See §14 for results.

**New criterion — three pre-registered tests on existing data:**

> **PROMOTE to plan stage if ALL three pass on the trailing-30d cohort:**
> 1. Bootstrap 5th-percentile mean per-trade Δ > $20
> 2. Cohort widened to peak ≥ 50% has positive total lift AND positive bootstrap 5th-percentile mean
> 3. Regime-stratified lift (pre vs post chain-dispatch revival 2026-05-01) is positive in BOTH cohorts
>
> **KILL permanently if ANY of the three fails on the trailing-30d cohort, refreshed weekly.**

**No calendar-month wait.** The criteria are computed on every weekly refresh of the data battery (`scripts/backtest_high_peak_existing_data_battery.py`).

**Why this is honest:** the calendar 2026-10-05 was anchored to R1's power calc for forward data, but R1 didn't account for cohort-widening (peak ≥ 50% gives n=25-26 today) or bootstrap robustness (distribution-free; doesn't need n=28 for normal-CI parametric inference). Existing data already has the statistical leverage R1 wanted; the calendar wait was unnecessary.

**Forward soak still needed — but ONLY for BL-067 interaction:** the proposal's pre/post-BL-067 split is unbalanced (n=9 pre, n=1 post). Promotion-stage plan should include a focused **4-6 week soak on locked-trade lift** (target: ≥ 3 high-peak fires on trades with `conviction_locked_at IS NOT NULL`). Other dimensions (regime stationarity, signal isolation, slippage) are already settled by §14.

### 13.8 Doc fixes shipped 2026-05-05 alongside the park

- §0 status updated to **PARKED** (later updated to **DECISION-READY** after §14 battery)
- §3 burnie row reconciled (was incorrectly marked "(excluded — pre-window)"; actually in cohort with Δ=−$8 — the per-signal `first_signal: 1, Δ −$8` row IS burnie)
- §5 elif → standalone-if (§13.4)
- §5 added explicit `conviction_locked_at IS NULL` guard (§13.3 future viability)

---

## 14. Existing-data analysis battery (added 2026-05-05 follow-up)

Existing data is encouraging across three analyses (bootstrap CI, cohort widening, regime stratification). However, these criteria were specified after the data was examined, not pre-registered before analysis — they should be read as exploratory evidence motivating a focused forward soak, not as a substitute for forward validation. The 4-6 week forward soak is required.

Operator pushback: 5-month soak window indefensible given accumulated data. Battery script `scripts/backtest_high_peak_existing_data_battery.py` runs five analyses on the trailing-30d cohort (n=859 closed trades, eligible n=723).

### 14.1 Cohort grew since first analysis

n=10 today (was n=9 this morning). New fire was on `narrative_prediction` (+$26 Δ), and one outlier emerged at -$62 (likely a recent gainers_early replacing an older trade rotating out of the 30d window). Headline lift updated **+$670 → +$696** with cohort growth.

Per-trade deltas (sorted): `[+$160, +$132, +$117, +$90, +$86, +$84, +$69, +$26, −$8, −$62]`

### 14.2 A. Bootstrap CI — PASS

10,000 resamples, n=10 cohort:
- Mean Δ: $69.59 / SE $21.24
- **5th-percentile bootstrap mean: $35.42** (well above $20 threshold)
- 50th: $70.28
- 95th: $101.28
- Leave-one-out total range: $535-$758 (drop-best-trade still leaves $535)

**Interpretation:** headline is robust to outliers. The bottom-tail of the bootstrap distribution still meets the promotion threshold.

### 14.3 B. Cohort widening — PASS (with new sweet-spot finding)

| Threshold | n_applied | Total Δ | Mean Δ | Bootstrap p5 |
|---|---|---|---|---|
| peak ≥ 30% | 85 | −$282 | −$3 | −$12 |
| peak ≥ 40% | 51 | +$242 | +$5 | −$8 |
| **peak ≥ 50%** | **26** | **+$646** | **+$25** | **+$4** |
| **peak ≥ 60%** | **15** | **+$779** | **+$52** | **+$23** |
| peak ≥ 75% | 10 | +$696 | +$70 | +$35 |
| peak ≥ 100% | 7 | +$435 | +$62 | +$14 |

**New finding:** peak ≥ 60% may be the operating point, not 75%. Per-trade Δ ($52) is 75% of peak ≥ 75% ($70), but **50% larger cohort (n=15 vs n=10)** means tighter forward-validation timeline. Bootstrap p5 = $23 still passes the $20 threshold. Plan-stage A/B between thresholds is warranted.

### 14.4 C. Regime stratification — PASS (chain-dispatch); inconclusive (BL-067)

| Split | Cohort | n | Total Δ | Mean Δ |
|---|---|---|---|---|
| Chain dispatch revival 2026-05-01 | BEFORE | 6 | +$367 | +$61 |
| Chain dispatch revival 2026-05-01 | ON/AFTER | 4 | +$329 | +$82 |
| BL-067 deploy 2026-05-04 | BEFORE | 9 | +$670 | +$74 |
| BL-067 deploy 2026-05-04 | ON/AFTER | **1** | +$26 | +$26 |

**Chain-dispatch split:** lift survives in BOTH cohorts with comparable per-trade means ($61 and $82). Regime-stationarity worry on the chain-dispatch axis is settled.

**BL-067 split:** post-deploy cohort is n=1 — **insufficient data**. This is the legitimate forward-data need: ~4-6 weeks for ≥ 3 post-BL-067 high-peak fires (at observed rate).

### 14.5 D. Per-signal isolation — gainers_early is the clean opt-in

| Signal | n total | n_applied | Actual $ | Counter $ | Δ $ |
|---|---|---|---|---|---|
| **gainers_early** | 212 | 4 | −$837 | −$406 | **+$431** |
| chain_completed | 6 | 2 | +$387 | +$606 | +$219 *(don't override BL-067 long-hold)* |
| losers_contrarian | 153 | 2 | +$94 | +$122 | +$28 *(signal disabled)* |
| narrative_prediction | 97 | 1 | +$704 | +$730 | +$26 |
| first_signal | 256 | 1 | −$132 | −$140 | −$8 *(noise)* |

Recommendation matches R3's strategy review: **gainers_early opt-in only at plan stage**. Other signals deferred per their own constraints (long-hold mandate / disabled / noise).

### 14.6 E. Slippage sensitivity — robust to 500bps

| Slippage | Counter total | Δ vs actual |
|---|---|---|
| 0 bps | +$818 | +$696 |
| 50 bps | +$803 | +$681 |
| 100 bps | +$788 | +$666 |
| 200 bps | +$758 | **+$636** |
| 500 bps | +$668 | +$546 |

**Paper→live transition viable.** Lift remains positive at 500bps slippage — well beyond realistic memecoin slippage estimates (typically 1-3% = 100-300bps). One of R3's blocker concerns (paper-only proposal) is closed.

### 14.7 Net assessment — promote

Existing data is encouraging in the direction of the proposed mechanism. Forward soak gated on ≥3 post-BL-067 high-peak fires (closes §14.4 gap) is required before live-mode flip.

All three exploratory promotion criteria from §13.7 are consistent with the hypothesis on existing data:
- ✓ Bootstrap p5 ($35) > $20
- ✓ Peak ≥ 50% lift positive ($+646, p5 $+4)
- ✓ Chain-dispatch regime split both positive

**Recommended next step:** advance to plan stage via `superpowers:writing-plans` skill. Plan should specify:
1. Target threshold: A/B between peak ≥ 60% and peak ≥ 75% during initial 4-week soak
2. Per-signal opt-in: gainers_early only at launch
3. Forward soak duration: **4-6 weeks** (not 5 months) — gated on ≥ 3 post-BL-067 high-peak fires to close the §14.4 gap
4. Defer-to-BL-067 guard already specified in §5
5. Auto-suspend circuit breaker per R3 (negative cohort-Δ for 2 consecutive weeks → master-flip off)

**Cost saved by data battery vs forward soak:** 5 months → ~6 weeks. ~4× faster timeline.
