# BL-NEW-LIVE-EVALUABLE-SIGNAL-AUDIT — Findings 2026-05-17

**Filed:** 2026-05-17 (cycle 7 of autonomous backlog knockdown)
**Source:** srilu-vps `prod.db` (`/root/gecko-alpha/scout.db`)
**Branch:** `feat/live-evaluable-signal-audit`
**Backlog item:** `BL-NEW-LIVE-EVALUABLE-SIGNAL-AUDIT` (filed 2026-05-12; estimate ~3h analysis + write-up)

## TL;DR

Of the 9 `signal_type`s that have ever produced paper trades, **only 3 are structurally live-eligible under current Tier 1/2 rules:** `chain_completed` (Tier 1a, always), `volume_spike` (Tier 2a, always), and `gainers_early` (Tier 2b, conditional on mcap + 24h gates). The remaining 6 either depend on `conviction_stack ≥ 3` (Tier 1b) and the signal-data shape caps stack below 3 in practice, or they fire alone with no path to admission.

**Two surface findings that warrant follow-up beyond this audit:**

1. **`chain_completed` has fired ZERO trades post-cutover (since 2026-05-11)** — Tier 1a's strongest signal has been silent for >6 days. Either the chain-pattern detector broke, or it's been a quiet window. Worth confirming.
2. **`losers_contrarian` (94 post-cutover trades, 0% eligible) + `narrative_prediction` (24 trades, 0% eligible) consume operator attention** — paper slots, alert noise, MiroFish jobs, calibration cycles — with structurally zero live-eligibility surface. At the next live-trading roadmap revisit, these should be evaluated against that constraint.

## Cohort table — all-time

| signal_type | n_trades | n_eligible | max_stack | n_stack≥3 | eligible % |
|---|---:|---:|---:|---:|---:|
| `gainers_early` | 491 | 35 | 3 | 39 | 7.1% |
| `losers_contrarian` | 330 | 0 | 0 | 0 | 0.0% |
| `first_signal` | 256 | 0 | 3 | 1 | 0.0% |
| `narrative_prediction` | 206 | 0 | 0 | 0 | 0.0% |
| `trending_catch` | 113 | 0 | 0 | 0 | 0.0% |
| `volume_spike` | 40 | 6 | 0 | 0 | 15.0% |
| `chain_completed` | 18 | 0 | 0 | 0 | 0.0% |
| `long_hold` | 14 | 0 | 0 | 0 | 0.0% |
| `tg_social` | 3 | 0 | 0 | 0 | 0.0% |

## Cohort table — post-cutover (≥ 2026-05-11T13:52Z, the writer-deployment time)

| signal_type | post-cutover n | eligible | % |
|---|---:|---:|---:|
| `gainers_early` | 124 | 35 | **28.2%** |
| `losers_contrarian` | 94 | 0 | 0.0% |
| `narrative_prediction` | 24 | 0 | 0.0% |
| `volume_spike` | 6 | 6 | **100.0%** |

Pre-cutover-only signals (last `opened_at` < 2026-05-11): `first_signal` (last 2026-05-01), `trending_catch` (last 2026-05-10; auto-killed per memory `project_trending_catch_soak_2026_05_10.md`), `chain_completed` (last 2026-05-07), `long_hold` (last 2026-04-25), `tg_social` (last 2026-05-09).

## Structural verdict per signal_type

| signal_type | Tier path | Structural max stack | signal_data shape | Verdict |
|---|---|---:|---|---|
| `gainers_early` | Tier 2b (mcap + 24h gates) OR Tier 1b (stack≥3) | 3 observed | `{price_change_24h, mcap}` | **LIVE-ELIGIBLE** under Tier 2b gates; 28.2% post-cutover |
| `volume_spike` | Tier 2a (always) | 0-1 | `{spike_ratio}` | **LIVE-ELIGIBLE** unconditionally — 100% post-cutover |
| `chain_completed` | Tier 1a (always) | 0 | `{pattern, boost}` | **LIVE-ELIGIBLE** structurally (Tier 1a unconditional). All-time table shows 0/18 eligible, but ALL 18 rows opened pre-cutover (last 2026-05-07; writer 2026-05-11) → `would_be_live` is NULL by `feedback_mid_flight_flag_migration.md`. "0/18" is a NULL artifact, NOT an eligibility hole. Separate concern: 0 trades post-cutover (see Finding 1). |
| `first_signal` | Tier 1b (stack≥3) potentially | 3 observed (1 trade), so structurally reachable | `{quant_score, signals}` | **TIER 1b ELIGIBLE** when conviction-stack reaches 3 — one such trade observed. Eligibility writer NULL-blanket post-cutover-only means we can't confirm whether the 1 stack-3 trade would have stamped =1; it predates 2026-05-11 cutover. Signal currently inactive (last 2026-05-01); see Finding 3. **V36 MUST-FIX fold:** earlier draft claimed "max stack=2 by design" from `FIRST_SIGNAL_MIN_SIGNAL_COUNT=2` — that gate controls *admission* via `len(signals_fired)` at signals.py:327, NOT the runtime `conviction_stack` (BL-067's cross-signal-type 504h co-firing count at `conviction.py:265`). The two are mechanistically distinct. |
| `trending_catch` | Tier 1b (stack≥3) ONLY | 1 by design (fires alone) | `{source, mcap_rank}` | **STRUCTURALLY NON-ELIGIBLE** — single-source from `trending_snapshot`; auto-killed 2026-05-11 |
| `losers_contrarian` | Tier 1b (stack≥3) ONLY | 0 observed | `{price_change_24h, mcap}` | **STRUCTURALLY NON-ELIGIBLE** — fires alone from gainers/losers scanner; no path to stack≥3 |
| `narrative_prediction` | Tier 1b (stack≥3) ONLY | 0 observed | `{fit, category, mcap}` | **STRUCTURALLY NON-ELIGIBLE** — fires alone from narrative scanner; no path to stack≥3 |
| `long_hold` | Tier 1b (stack≥3) ONLY | 0 | `{origin_trade_id, origin_signal}` | **N/A** — follow-on exit-strategy trade, not a primary entry signal; live-eligibility may be conceptually inapplicable |
| `tg_social` | depends on signal_data; current shape doesn't carry a Tier marker | 0 | `{channel_handle, contract_address, mcap_at_sighting}` | **STRUCTURALLY NON-ELIGIBLE** under current Tier rules — even if dispatched, the signal_data shape doesn't match any Tier 1/2 admission path |

## Empirical eligible-subset rate, post-cutover

The 3 structurally-eligible signal_types account for **41 of 41 = 100%** of post-cutover eligible trades. The structurally-non-eligible signal_types contribute **0 of 118 = 0%** eligible trades (94 `losers_contrarian` + 24 `narrative_prediction`).

This is the load-bearing empirical confirmation of the structural argument: writers correctly stamp 0% eligible for the structurally-non-eligible signal_types — they would never have made it past the Tier-1/2 filter regardless of paper performance.

**Caveat (V36 fold):** "0% eligible" for `losers_contrarian` + `narrative_prediction` is a STRUCTURAL result. "0% eligible all-time" for `chain_completed` is a CUTOVER ARTIFACT (all 18 trades pre-2026-05-11; writer not running, would_be_live NULL). The two have the same surface number but different mechanisms — readers should not confuse the structural verdict with the NULL artifact.

## Finding 1: `chain_completed` silence post-cutover

`chain_completed` last opened a trade at `2026-05-07T20:19:44Z` — 10 days before this audit. Tier 1a is supposed to be the "strongest cohort." Possible causes:

- **Chain-pattern detector regression** — `scout/scoring/chain_completed.py` or the upstream `chain_matches` table writer broke
- **Quiet 10-day window** — chain patterns require a specific volume_breakout/momentum cascade that simply hasn't materialized
- **Auto-suspend on chain_completed** — would show in `signal_params` table; worth a one-query check

**Recommended follow-up:** file `BL-NEW-CHAIN-COMPLETED-SILENCE-AUDIT` to drill into journalctl + `chain_matches` table for the 10-day window. If pattern detection is intact, this is informational. If broken, it's a real outage of the highest-tier signal.

## Finding 2: Resource consumption by structurally-non-eligible signals

Post-cutover trade contribution by structurally-non-eligible types (V36 fold — precise arithmetic; total post-cutover n = 248):

- `losers_contrarian`: 94 trades (~38% of post-cutover paper volume; eligible 0)
- `narrative_prediction`: 24 trades (~10% of post-cutover paper volume; eligible 0)

Combined: **~48% (118/248) of post-cutover paper trades are from signal_types that can never go live under current Tier rules.** Operator resources consumed:

- Paper slots (up to 50 simultaneous per cap)
- Telegram alerts (each open + each close)
- MiroFish narrative jobs (up to 50/day cap)
- Calibration cycles (`scout/trading/calibrate.py` runs against all signal_types)
- Auto-suspend bookkeeping (`signal_params` rows)

**Not a recommendation to disable** — paper trades on non-eligible signals still validate the scoring model and produce per-signal PnL evidence. But at the next live-trading roadmap revisit, the operator should explicitly choose: (a) keep paper-trading non-eligible signals as a research surface, (b) add a settings-driven allowlist that excludes them from auto-suspend/calibration/alert calculations, (c) demote them to the lower-cost `narrative` event stream without trade dispatch.

## Finding 3: `first_signal` is effectively dead (cause undetermined)

Last opened 2026-05-01 — 16 days ago. **V36 MUST-FIX fold:** the pre-cutover 256-trades-0-eligible figure is a NULL artifact (writer wasn't running), NOT a structural verdict. The 1 row with `conviction_locked_stack ≥ 3` proves Tier 1b is reachable.

Either:

- The first_signal dispatcher's admission rule (`len(signals_fired) >= FIRST_SIGNAL_MIN_SIGNAL_COUNT=2`) has stopped firing — upstream momentum-ratio + cg_trending_rank pair no longer co-occur for fresh candidates
- A silent configuration retirement
- Genuine 16-day quiet window

**Open question deferred to follow-up:** the 1 first_signal trade with `conviction_locked_stack=3` — pre-cutover (NULL), slot-cap-rejected, or writer-bug-skipped? Not drilled in this audit.

**Recommended follow-up:** `BL-NEW-FIRST-SIGNAL-RETIREMENT-DECISION` reframed: confirm whether `first_signal` should be retired in code, revived via lowered admission gates, OR investigated as a dispatcher-side regression. Filing-only; no immediate action.

## Recommended follow-ups (to file as net-new backlog entries)

| ID | Trigger | Cost |
|---|---|---|
| BL-NEW-CHAIN-COMPLETED-SILENCE-AUDIT | Finding 1 | ~1h journalctl + chain_matches inspection |
| BL-NEW-FIRST-SIGNAL-RETIREMENT-DECISION | Finding 3 | ~1h trace + decision |
| Future live-roadmap revisit | Finding 2 | Allowlist decision; defer until BL-055 active |

## What's NOT in scope

- Dashboard surface for these audit results (can fold into BL-NEW-LIVE-ELIGIBLE follow-up if useful)
- Settings-driven allowlist (Finding 2's option b) — operator-decision, not implementation-time
- Backfill of pre-cutover rows (367 `gainers_early` rows have NULL `would_be_live`; per `feedback_mid_flight_flag_migration.md`, pre-cutover rows correctly stay NULL — not eligible for A/B comparison)

## Source queries

All findings reproducible via:

```bash
ssh root@srilu-vps "sqlite3 /root/gecko-alpha/scout.db <<EOF
SELECT signal_type, COUNT(*) n,
       SUM(CASE WHEN would_be_live=1 THEN 1 ELSE 0 END) eligible,
       MAX(COALESCE(conviction_locked_stack,0)) max_stack,
       SUM(CASE WHEN COALESCE(conviction_locked_stack,0)>=3 THEN 1 ELSE 0 END) stack_3plus,
       SUM(CASE WHEN would_be_live IS NULL THEN 1 ELSE 0 END) null_count,
       MIN(opened_at) earliest, MAX(opened_at) latest
FROM paper_trades GROUP BY signal_type ORDER BY n DESC;
EOF"
```

Tier-rule source-of-truth: `scout/trading/live_eligibility.py:42-75` (`matches_tier_1_or_2`).

## Hermes-first verdict

No Hermes skill covers signal-type × eligibility-rule coverage analysis. Project-internal data analysis. awesome-hermes 404 (consistent).

## Drift verdict

NET-NEW. `tasks/findings_*` does not contain a prior live-eligibility audit by signal_type. `BL-NEW-LIVE-ELIGIBLE` shipped the writer (`compute_would_be_live`); this entry asks what the writer can never stamp `=1` for and why.
