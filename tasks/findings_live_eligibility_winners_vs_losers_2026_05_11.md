# Live-Eligibility Analysis: Winners vs Losers in Paper Trades

**New primitives introduced:** NONE (analysis-only)

**Date:** 2026-05-11
**Question:** With 148 currently-open + 1,073 closed paper trades, what entry-time traits separate winners from losers cleanly enough to define a live-tradeable subset for the 20-slot capital constraint?
**Data scope:** 752 closed trades with `realized_pnl_usd` populated (post-BL-062, 2026-04-24 onwards). 321 older trades have NULL realized PnL — excluded.

---

## §1 Baseline (no filter)

| metric | value |
|---|---|
| Closed trades | 752 |
| Winners | 300 (39.9%) |
| Losers | 452 |
| Avg PnL/trade | +$10.68 |
| Total PnL | +$8,032 |

---

## §2 By signal_type — primary separator (ranked by total PnL)

| signal_type | n | WR% | avg $/trade | total $ | avg pnl_pct | verdict |
|---|---:|---:|---:|---:|---:|---|
| `chain_completed` | 9 | **100.0** | **$94** | $845 | **+56%** | ⭐ TAKE EVERY TIME |
| `volume_spike` | 29 | **65.5** | **$17** | $497 | +6.7% | ⭐ TAKE EVERY TIME |
| `gainers_early` | 255 | 53.7 | $13 | $3,210 | +0.6% | ✅ workhorse |
| `losers_contrarian` | 144 | 39.6 | $11 | $1,656 | +2.1% | ⚠️ low WR, high avg gain |
| `narrative_prediction` | 149 | 35.6 | $8 | $1,149 | +4.2% | ⚠️ low WR, but big gain when right |
| `first_signal` | 84 | 14.3 | $5 | $416 | **-1.4%** | ❌ SKIP for live |
| `trending_catch` | 78 | 16.7 | $3 | $259 | **-1.6%** | ❌ SKIP for live (currently soaking) |
| `long_hold` | 3 | 0.0 | $0 | $0 | -16% | ❌ tiny sample, skip |
| `tg_social` | 1 | 0.0 | $0 | $0 | 0% | ⚠️ tiny sample |

---

## §3 Within-signal selectors that further separate winners

### narrative_prediction.fit (monotonic — agent score IS predictive)

| fit_score | n | WR% | avg $ | avg pnl_pct |
|---|---:|---:|---:|---:|
| <65 | 100 | **30.0** | $6 | +2.5% |
| 65-74 | 42 | 45.2 | $9 | +7.0% |
| 75-84 | 5 | **60.0** | **$20** | +11.7% |
| ≥85 | 2 | 50.0 | $12 | +10.0% |

**Implication:** Gate `narrative_prediction` to `fit >= 65` (drops 100 weak trades, raises cohort WR from 35.6% → 41% with n=49). Stronger gate `fit >= 75` is high quality but tiny sample.

### gainers_early.mcap

| mcap tier | n | WR% | avg $ |
|---|---:|---:|---:|
| 250M+ | 12 | **83.3** | $19 |
| 10-50M | 128 | 55.5 | $14 |
| 1-10M | 44 | 52.3 | $12 |
| 50-250M | 71 | 46.5 | $9 |

**Implication:** Floor at `mcap >= $10M` drops the worst sub-cohort (50-250M only modestly worse, but with $10M floor we drop the noise of microcap "winners" who 2x then collapse).

### losers_contrarian.mcap (counterintuitive — small caps win bigger here)

| mcap tier | n | WR% | avg $ |
|---|---:|---:|---:|
| 1-10M | 25 | **44.0** | **$22** |
| 250M+ | 7 | 57.1 | $8 |
| 10-50M | 74 | 37.8 | $10 |
| 50-250M | 38 | 36.8 | $9 |

**Implication:** Contrarian on small caps (1-10M) has the best risk/reward — when they recover, they recover hard.

### volume_spike.spike_ratio
Mostly cluster at 5-6x (n=24, 66.7% WR, $19/trade). The cohort is small enough to take all.

### conviction_locked_stack (BL-067 — only deployed 2026-05-04)

| stack | n | WR% | avg $ |
|---|---:|---:|---:|
| 0 | 734 | 39.2 | $10 |
| **3** | **18** | **66.7** | **$24** |

Strong confirmation of BL-067 thesis. Only 18 trades so far — needs more soak. **Mandatory inclusion in live filter.**

### conviction_locked_stack × signal_type (gainers_early breakout)

| signal × stack | n | WR% | avg $ |
|---|---:|---:|---:|
| gainers_early stack=0 | 238 | 52.5 | $12 |
| **gainers_early stack=3** | **17** | **70.6** | **$25** |

---

## §4 What DOES NOT separate

- **chain (coingecko vs solana)** — only 1 solana trade, can't tell yet (BL-NEW-M1.5C just shipped to add visibility here)
- **lead_time_vs_trending_status** — `ok` vs `no_reference` produce nearly identical PnL (41.5% vs 39.1% WR)
- **would_be_live** — column is **all 0** across 752 trades. BL-060 infra exists but writer was never wired. **This is a finding** — surface as separate gap.
- **day-of-week** — Wednesday is 78.8% WR vs Friday 24%, but small samples and likely regime noise; do not gate on
- **hour-of-day** — no clean monotonic pattern
- **24h_change within gainers_early** — 20-30% vs 30-50% near identical (53.2% vs 55.2% WR)

---

## §5 Proposed live filter — Tier system

### TIER 1 — Mandatory take (any time the signal fires)
1. `signal_type = 'chain_completed'` (any pattern)
2. `conviction_locked_stack >= 3` (any signal type)

**Tier 1 performance:** n=27, **77.8% WR**, **$47/trade**, +$1,274 total
**Tier 1 daily volume:** typically 1-3/day, max 4

### TIER 2 — High quality (take if Tier 1 isn't capping bankroll)
1. `signal_type = 'volume_spike'` (any spike_ratio ≥ 5x — current threshold)
2. `signal_type = 'gainers_early'` AND `mcap >= $10M` AND `price_change_24h >= 25%`

**Tier 2 performance:** n=95, **55.8% WR**, **$14/trade**, +$1,367 total

### TIER 1 + 2 combined
- **n=113 trades over ~30d → ~4 trades/day** (peak day 13, low day 1)
- **58.4% WR** (vs 39.9% unfiltered)
- **+$21/trade** (vs +$10.7 unfiltered)
- **+$2,372 total** (29% of unfiltered PnL retained from 15% of trades)

### TIER 3 — Take if slot available + Tier 1+2 quiet
- `narrative_prediction` with `fit >= 65` AND `mcap in [10M, 50M]`
- `losers_contrarian` with `mcap in [1M, 50M]` (especially 1-10M for size weight)

---

## §6 Bankroll math

At avg **49 concurrent open** in Tier-1+2+filtered cohort (max 76), the system still oversubscribes a 20-slot allocation. Two options:

1. **FCFS-20** (BL-060 design, never wired): take Tier 1+2 trades in arrival order until 20 slots are full; reject overflow with `would_be_live=False`. Implement by populating the existing `would_be_live` column.
2. **Best-N-by-tier**: when at 20 slots, evict lowest-tier open position when a higher-tier signal fires. Riskier (forces premature exits) but maximizes quality.

Recommend Option 1 (simpler, no premature exit damage, and the Tier 1 daily rate ~1-3 means Tier 1 will almost never get crowded out).

At **$300/slot × 20 slots = $6,000 capital**, Tier-1+2's $21/trade avg × ~4 trades/day ≈ **+$84/day** = **+$2,500/mo** in paper.

---

## §7 Open gaps / what to wire next

1. **`would_be_live` column never populated** (BL-060 not wired). Wire it to the proposed Tier 1+2 + FCFS-20 cap so the dashboard surfaces "live-tradeable subset" PnL alongside the firehose.
2. **`conviction_locked_stack=3` cohort is only n=18.** Wait for n≥30 before fully committing to it as a Tier 1 gate. Current data is encouraging but not yet decisive.
3. **`narrative_prediction.fit >= 75` cohort is n=7.** Promising (60% WR, $20/trade) but too small to gate on.
4. **`chain_completed.pattern = 'full_conviction'` is the single biggest winner** (trade #1651: +$581, +356% peak). Worth a separate analysis of which `pattern` values dominate.
5. **Solana coverage is 1 trade out of 752.** Almost all of gecko-alpha's signal universe is coingecko-chain. BL-NEW-M1.5C (just shipped) will surface Solana-eligibility for paper alerts — observe over the 14d soak.

---

## §8 Recommended next code change — **SHIPPED 2026-05-11 PR #98**

**Status:** WIRED. PR #98 (`8a07662`) merged + deployed 2026-05-11T13:22Z.

Implementation diverged slightly from the inline-engine sketch below:
- Logic lives in `scout/trading/live_eligibility.py` (new module, ~106 LoC) — pure `matches_tier_1_or_2()` + async `compute_would_be_live()` with FCFS-20 cap query
- `scout/trading/paper.py:PaperTrader.execute_buy` calls both before INSERT; outer try/except defends paper-trade open from any new exception path
- 3 new tunable Settings: `PAPER_LIVE_ELIGIBLE_SLOTS=20`, `PAPER_TIER2_GAINERS_MIN_MCAP_USD=10e6`, `PAPER_TIER2_GAINERS_MIN_24H_PCT=25.0`
- PR-review V1 found 1 IMPORTANT (race in SELECT-then-INSERT cap check; acceptable for observational use, flagged as MUST-fix when live trading routes through) + 2 NIT folds (skip `compute_stack` for chain_completed/volume_spike; annotate evaluator long_hold reopen)

**Original sketch** (kept for historical reference):

```python
async def _open_paper_trade(...):
    # ... existing logic ...
    is_tier1 = (signal_type == 'chain_completed' 
                or conviction_locked_stack and conviction_locked_stack >= 3)
    is_tier2 = (signal_type == 'volume_spike'
                or (signal_type == 'gainers_early' 
                    and signal_data.get('mcap', 0) >= 10e6
                    and signal_data.get('price_change_24h', 0) >= 25))
    
    open_live_count = await db.count_open_where("would_be_live=1")
    if (is_tier1 or is_tier2) and open_live_count < 20:
        would_be_live = 1
    else:
        would_be_live = 0
    
    await db.insert_paper_trade(..., would_be_live=would_be_live)
```

Then the existing dashboard can filter on `would_be_live=1` to show what the live subset looks like in real time. **No production behavior change** — paper trades continue to open at the same rate. The column just records membership.

**Soak window:** 14-30d to confirm Tier 1+2 thesis with the live-eligible subset before flipping `LIVE_TRADING_ENABLED=True` and routing the same 20-slot cap through the Binance/Minara adapter.
