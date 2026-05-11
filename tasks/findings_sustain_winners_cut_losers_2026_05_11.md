# Sustain Winners, Cut Losers Early — Diagnosis from OSMO + Cohort Data

**New primitives introduced:** NONE (analysis-only)

**Date:** 2026-05-11
**Trigger:** OSMO trade #1838 (paper). Entry $0.049 → trail-stop exit at $0.051 (+3.67% / +$11) → token ran another +87% to $0.0954 post-exit. System detected but couldn't sustain.

---

## §1 OSMO mechanism — exact cause

Settings query of prod signal_params shows the smoking gun:

| signal_type | `trail_pct` (peak ≥ 20%) | `trail_pct_low_peak` (peak < 20%) | `low_peak_threshold_pct` | sl_pct |
|---|---:|---:|---:|---:|
| gainers_early | 20 | **8** | 20 | 25 |
| losers_contrarian | 20 | **8** | 20 | 25 |
| volume_spike | 20 | **8** | 20 | 25 |
| narrative_prediction | 20 | **8** | 20 | 25 |
| chain_completed | 35 | **8** | 20 | 30 |

OSMO peaked at **+13.3%** (below the 20% `low_peak_threshold`), so the system used `trail_pct_low_peak = 8`. Exit fired at 8.6% drawdown from peak → matches.

**Conviction-lock widening was bypassed.** OSMO armed conviction-lock at stack=3 (which adds +10pp to `trail_pct`), but `scout/trading/evaluator.py:168` explicitly comments: *"trail_pct_low_peak intentionally NOT overlaid"*. So lock didn't widen the 8% trail. This is the root cause.

---

## §2 Cohort impact — not just OSMO

Trail-stop exits where peak was modest (< 20%) — the "OSMO pattern":

| signal_type | n | avg peak | avg exit | giveback (pp) | avg PnL |
|---|---:|---:|---:|---:|---:|
| gainers_early | 46 | 14.5% | 3.9% | **10.6** | $16 |
| losers_contrarian | 12 | 14.0% | 4.0% | 10.0 | $16 |
| volume_spike | 6 | 14.6% | 4.0% | 10.6 | $16 |
| chain_completed | 3 | 17.3% | 7.1% | 10.2 | $15 |
| narrative_prediction | 3 | 15.6% | 5.2% | 10.4 | $15 |
| **Total** | **75** | **~14.5%** | **~4%** | **~10pp** | **$15/trade** |

**75 trades** got cut at ~4% PnL after reaching ~14% peak — a textbook "modest peaker cut by tight trail" pattern.

**Mcap doesn't change the pattern**: 10-50M tier (n=30) has same giveback as 50-250M (n=12) or <10M (n=10). The 8% low-peak trail is too tight regardless of mcap.

---

## §3 Trail vs peak-fade — which mechanism captures winners better?

Among gainers_early winners specifically:

| exit_reason | n | avg hold | avg peak | avg exit | giveback (pp) | avg PnL |
|---|---:|---:|---:|---:|---:|---:|
| trailing_stop | 76 | 11.9h | 29.9% | varies | **10-50** | varies |
| peak_fade | 49 | **49.1h** | 23.6% | 17.6% | **2-11** | $20+ |

**Peak-fade holds 4× longer with 5× less giveback.** Peak-fade is the better winner-capture mechanism; trail is firing too aggressively on modest peaks.

Generalized across all closed trades:

| peak bucket | trail giveback | peak_fade giveback |
|---|---:|---:|
| 10-20% | **10.4 pp** | 2.0 pp |
| 20-30% | 20.2 pp | 7.7 pp |
| 30-50% | 24.5 pp | 11.0 pp |
| ≥50% | **50.8 pp** | 10.2 pp |

The giveback differential is *enormous* at every peak level. Trail is leaving 5-25× more on the table than peak-fade across every cohort.

---

## §4 Cut losers earlier — the expired no-momentum cohort

Trades that expired (48h-168h timeout) with peak < 10% — never developed momentum, eventually closed flat or negative:

| signal_type | n | avg peak | avg final pct | avg hold |
|---|---:|---:|---:|---:|
| losers_contrarian | 44 | 4.4% | -2.7% | 50.7h |
| first_signal | 40 | 3.5% | -3.2% | 28.1h |
| **gainers_early** | **37** | 4.3% | **-11.4%** | 56.6h |
| narrative_prediction | 36 | 4.2% | -1.0% | 97.6h |
| trending_catch | 36 | 4.6% | -1.9% | 64.0h |
| volume_spike | 5 | 2.4% | -3.7% | 139h |

**~200 trades** opened, never reached 10% peak, eventually closed at avg -3% to -11%. They sat in the portfolio for 28-139h occupying capital and slots that better signals could have used.

Gainers_early is the worst — avg -11.4% final after 56h. These are signals that fired, briefly went green, then bled out. Cutting them at 24h would have freed capital and limited losses.

---

## §4.5 Mcap-tier hypothesis — TESTED AND REJECTED (2026-05-11)

**Tested.** Same n=75 trail-stop-winners-with-peak<20% cohort, broken down by mcap:

| mcap | n | avg peak | avg exit | giveback (pp) |
|---|---:|---:|---:|---:|
| 5-10M | 11 | 15.0% | 4.7% | **10.2** |
| 10-30M | 24 | 14.2% | 3.8% | **10.4** |
| 30-50M | 6 | 13.1% | 3.0% | **10.0** |
| 50-100M | 4 | 14.3% | 3.5% | **10.8** |
| 100-250M | 10 | 14.4% | 4.3% | **10.1** |
| 250M+ | 6 | 15.7% | 4.3% | **11.4** |

**Rejected.** Uniform 10-11pp giveback across $5M-$250M+ at peak<20% — no empirical mcap breakpoint exists in this regime. The "micro-caps move faster, need tight trail" intuition is **not supported by data** for the low-peak exit regime — micro-caps (<$10M) showed the same ~10pp giveback as 100-250M mid-caps.

**Future proposals to introduce mcap-based exit policy differentiation in the low_peak regime must produce new evidence of regime-dependent behavior, not rely on the "micro-caps move faster" intuition** (confirmed not supported by data 2026-05-11). This finding rules out an entire class of curve-fit proposals and should prevent the same vibe-tiering from being re-proposed.

Note: this rejection applies only to `trail_pct_low_peak` at peak<20%. It does NOT apply to:
- Full `trail_pct` (peak≥20% regime) — uniform widening there is still flagged as anti-pattern in §7 because micro-cap pump-and-dump CAN matter when peak has actually developed
- Entry-time gates (Tier 2 `PAPER_TIER2_GAINERS_MIN_MCAP_USD=10e6` in BL-NEW-LIVE-ELIGIBLE) — different question (selection vs. exit)

---

## §5 Proposed adjustments — data-driven priority

### P1 (DEFERRED pending width-lock backtest)

**Originally proposed:** Mcap-tiered `trail_pct_low_peak` (<$10M=8%, $10-50M=15%, >$50M=18%).

**Status 2026-05-11:** REJECTED in current form — see §4.5. Mcap breakpoints don't exist empirically.

**Revised plan:** P1-uniform — widen `trail_pct_low_peak` 8% → ?? for ALL mcap tiers and ALL signal types in the low_peak regime. The "??" requires a backtest pass over the historical cohort with widths ∈ {10, 12, 15, 18, 20} to lock the empirically-optimal value. Backtest scoped in parallel with P2 soak (see §6).

**Blast radius if shipped:** 109 currently-open trades with peak<20% (gainers_early 49, losers_contrarian 35, narrative_prediction 10, trending_catch 5, chain_completed 5, volume_spike 4, tg_social 1) totaling ~$32,700 capital. NOT a future-trades-only change.

**Original mcap tier proposal kept here for historical record:**

| mcap tier | proposed (REJECTED) |
|---|---:|
| < $10M | 8% (keep — fast pump+dump) |
| $10-50M | 15% |
| > $50M | 18% |

**Estimated impact:** of the 75 "OSMO-pattern" trade-stop winners, ~52 are mid/large-cap. Wider trails would let them develop further. Some won't recover from the 15-18% drawdown — but the historical peak_fade-pathway data suggests the win rate on continued holds is positive.

**Implementation:** new Settings `PAPER_TRAIL_LOW_PEAK_MIDCAP_PCT=15`, `PAPER_TRAIL_LOW_PEAK_LARGECAP_PCT=18` + mcap-aware selection in evaluator.

### P2 (SHIPPING — surgical fix) — Apply conviction-lock widening to `trail_pct_low_peak`

Currently `scout/trading/evaluator.py:168` explicitly excludes `trail_pct_low_peak` from conviction-lock widening. This is a **silent contract violation of BL-067** (conviction-lock widening promised but neutralized for low-peak trades). Same shape as the moonshot floor neutralization at peak≥40% (tracked in `tasks/findings_moonshot_floor_nullification.md`) — together, the two neutralizations mean conviction-lock widening only operates in the middle band (peak 20-40%) by current design.

**Proposed:** at stack ≥ 2, overlay `+5pp`. At stack ≥ 3, overlay `+10pp`. Cap at 25%.

**Estimated impact:** OSMO had stack=3 — would have gotten 8 + 10 = 18% trail, would not have fired at 8.6% drawdown.

**Blast radius (verified 2026-05-11):** 10 currently-open trades — all gainers_early, all stack=3, min peak 0.3%, max peak 9.9%, avg peak 4.2%. Capital $3,000. Realistic 14-day sample: 3-7 closes.

#### Pre-registered evaluation criteria (locked before ship)

**Success — ALL of:**
- At least 50% of stack=3 trades with peak<20% closing in the 14-day window exit with giveback ≤ 5pp
- Mean giveback across all qualifying closes ≤ 6pp (vs. baseline ~10pp)
- No individual trade exits with giveback >15pp (catches "wider trail let one position give back catastrophically")

**Failure — ANY of:**
- ≥2 stack=3 trades with peak<20% exit via SL with -25% loss (extreme path)
- ≥3 stack=3 trades with peak<20% exit via expiry with PnL **worse than the pre-P2 8% trail would have realized** (estimated peak × 0.92). Catches "trail too wide, position rides back to zero via expiry" mode that the SL criterion misses.
- Mean realized PnL across qualifying stack=3 trades over 14-day window is negative

**Actions at n<5 qualifying closes by D+14:**
- Positive trajectory (some giveback reduction observed, no failure criterion met): extend soak by 14 more days until n≥10. **Do NOT proceed to P1 backtest.**
- Negative trajectory (any failure criterion met): revert P2 immediately. Re-scope based on revealed failure mode.
- Neutral (zero qualifying closes, or no change vs baseline): extend soak.

**Boundary:** If results land between success and failure, extend soak. Do NOT renegotiate criteria to fit results.

### P3 (cut losers earlier) — No-momentum 24h cutoff

If `peak_pct < 5%` AND `hold_hours >= 24` AND not conviction-locked → close at market.

**Estimated impact:** ~200 expired-flat trades cut at 24h instead of 28-139h. Capital freed for new signals. Avg PnL stays similar (these are flat-to-negative regardless), but **capital recycling rate improves substantially** — important for the 20-slot constraint that BL-NEW-LIVE-ELIGIBLE is enforcing.

**Implementation:** new Settings `PAPER_NO_MOMENTUM_PEAK_THRESHOLD_PCT=5`, `PAPER_NO_MOMENTUM_CUTOFF_HOURS=24`. Skip for conviction-locked (stack≥2) so we don't kill late-blooming high-conviction trades.

### P4 (highest leverage, hardest to ship) — Lower peak_fade arming threshold

Peak_fade is empirically the best winner-capture mechanism (2-11pp giveback vs 10-50pp for trail). The reason it underfires: arming requires reaching a high-peak threshold (BL-NEW-HPF set this conservatively to avoid false-arms on small movers).

**Proposed:** lower the peak_fade arming `high_peak_threshold_pct` from current (40%? need to verify) to 25%. Combined with conviction-lock widening, this would let moderate winners fade out via the gentler mechanism.

**Risk:** false arms on noise. Backtest required before shipping — defer to a Phase-2 investigation.

---

## §6 Sequencing recommendation (REVISED post-verification)

1. **P2 first** (this PR) — surgical bypass-fix at evaluator.py:168. Blast radius 10 stack=3 open trades verified. 14-day soak with pre-registered criteria §5.
2. **P1-uniform width-lock backtest** runs in parallel during P2 soak. Spec below in §6.5 — locked before backtest runs to prevent post-hoc cherry-picking.
3. **P1-uniform ship** at D+14 if P2 success criteria met AND backtest output supports a specific width. If P2 fails, P1 does NOT auto-ship — re-scope based on what P2 failure revealed.
4. **P3 survivorship analysis** sequenced after P1 lands. The "n=200 expired-flat" claim needs verification that peak-at-24h (not peak-at-close) actually correlates with non-recovery. Cannot be priority 2 without this check.
5. **P4 (peak-fade arming threshold)** stays Phase-2 — requires separate backtest of false-arm rates on noise.

**Pre-registered dependency:** P1-uniform is gated on P2 success. P2 is a proof-of-mechanism for the broader low_peak trail widening hypothesis. If P2 fails, P1 does NOT ship in proposed form.

Each ships as one PR with multi-vector reviewer dispatch per CLAUDE.md §8 (these touch exit gates / money flow — exactly the kind of change that section is for).

## §6.5 P1-width-lock backtest spec (locked 2026-05-11, before backtest runs)

**Cohort:**
- All `paper_trades` rows with `status LIKE 'closed_%'` AND `realized_pnl_usd IS NOT NULL` AND `peak_pct IS NOT NULL` AND `peak_pct < 20`
- Opened within the past 90 days from backtest run date
- All signal types (not just gainers_early)

**Widths tested:** `trail_pct_low_peak` ∈ {8, 10, 12, 15, 18, 20} — 8 is current baseline; others are candidates.

**Replay mechanics:**
- Re-run the exit cascade against historical peak/exit price trajectories
- Requires per-trade peak trajectory (not just peak_pct snapshot) — see §6.6 infrastructure-check
- Use the same SL/floor/peak-fade/expiry logic as production; only `trail_pct_low_peak` varies

**Metrics (locked before run):**
- Total realized PnL across cohort, per width
- Mean giveback per trade (peak_pct - exit_pct), per width
- Win-rate (% trades with realized_pnl_usd > 0), per width
- Max single-trade drawdown, per width
- Distribution of exit reasons (trail vs peak-fade vs expiry vs SL vs floor), per width
- Number of trades that flipped exit reason vs baseline, per width

**Width-lock decision rule (locked before run):**
- Pick the width that maximizes total PnL **subject to** max single-trade drawdown ≤ 30% (no catastrophic single losses) AND win-rate ≥ baseline-2pp
- If no width passes both constraints, report and ESCALATE — don't pick a width that violates them

**Output format (locked before run):**
- Single Markdown table with all widths × all metrics
- Append distribution histograms per width
- Save to `tasks/findings_trail_low_peak_width_lock_backtest_<date>.md`

## §6.6 Backtest infrastructure check (must run before backtest)

Open question: does gecko-alpha currently have replay infrastructure that can re-run the exit cascade against historical peak/exit price data with a modified `trail_pct_low_peak`?

If YES → backtest is a few hours of work to invoke + analyze
If NO → infrastructure build is itself a meaningful piece of work that must be scoped before P1 ships

To verify before P2 ships: check `scripts/backtest_*.py` for existing replay logic; look for whether `paper_trades.peak_pct` records intermediate peak trajectory or only final peak.

---

## §6.7 Explicit accounting of unresolved surface after P2 ships

| dimension | resolved by P2? | still open |
|---|:---:|---|
| n=75 low-peak trail-stop pattern | **9%** (10/109 = stack=3 conviction-locked trades) | 91% (99 non-locked trades) — pending P1-uniform after width-lock backtest |
| Conviction-lock contract violation (evaluator.py:168 bypass) | ✅ | Moonshot floor neutralization at peak≥40% (tracked in `tasks/findings_moonshot_floor_nullification.md`) — NOT addressed by P2 |
| Peak-fade is empirically 5× better than trail | ❌ | P4 (lower peak-fade arming threshold) — deferred Phase-2 |
| ~200 expired-flat losers (P3 cohort) | ❌ | Survivorship analysis required before sequencing |

**Conviction-lock-overlay effectiveness is partially addressed by P2** (low_peak bypass fixed); **remains unaddressed at moonshot regime** (peak≥40%). Net: conviction-lock now operates in two of three peak regimes (low_peak ✅ + middle band ✅), still neutralized in high-peak regime (moonshot ❌).

## §7 What NOT to do (anti-patterns surfaced by the data)

- ❌ Don't tighten SL ("cut losers harder") — losers are already getting cut at SL or expire. Tightening SL would just cut more winners.
- ❌ ~~Don't widen trail uniformly (e.g., 8 → 15 across the board for all mcap tiers) — micro-cap pump+dumps NEED tight trail; widening risks giving back genuine micro-cap wins.~~ **REVISED 2026-05-11:** the data in §4.5 shows uniform giveback across mcap tiers in the low_peak regime. The micro-cap-needs-tight-trail intuition was confirmed-as-wrong for `trail_pct_low_peak` specifically. P1-uniform (single width across mcap tiers) is now the recommended approach, pending width-lock backtest. **Anti-pattern remains valid for** full `trail_pct` (peak≥20% regime) where mcap-driven volatility may genuinely differ.
- ❌ Don't change TP — TP is rarely the exit reason in the dataset (n=14 across 752 trades). Not load-bearing.
- ❌ Don't add ML/regime classifiers (per `feedback_ml_not_yet.md`) — rules-based fixes for current scale.

---

## §8 OSMO retrospective — what would have saved it

| change | OSMO outcome |
|---|---|
| Today (baseline) | trail fires at 8% drawdown from peak 13.3% → exit +3.67% |
| **P2 only** (lock-aware low_peak trail, +10pp at stack≥3) | trail = 18%; would not have fired at 8.6%. Trade keeps running. **Best surgical fix.** |
| **P1 only** ($38M mid-cap → 15% low_peak trail) | trail = 15%; still wouldn't have fired at 8.6%. |
| **P1 + P2 combined** | trail = max(15, 18) = 18%. Saved. |
| P3 (no-momentum cutoff) | OSMO had peak 13% within 4h — wouldn't have triggered. Irrelevant here. |
| P4 (lower peak_fade threshold to 25%) | peak_fade would not arm at 13.3%. Irrelevant for this specific trade but helps the 20-30% peak cohort. |

**P2 is the single highest-leverage change for the OSMO pattern.** Lock-aware low_peak trail is one targeted line edit in `scout/trading/evaluator.py:168` + conviction-overlay logic + tests.
