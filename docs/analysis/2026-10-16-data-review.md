**New primitives introduced:** NONE (analysis only).

# gecko-alpha legacy data and strategy-evidence review (2026-10-16)

Read-only review of `/root/gecko-alpha/scout.db` (1.6 GB, WAL, snapshot 2026-10-16 ~01:00 UTC) and `/root/xs_study/`.
Every query and script is saved in `docs/analysis/2026-10-16-data-review-queries/`, and Appendix A maps each section to its script.
Nothing was written on the VPS outside `/tmp/data-review/`.

## Executive summary

1. **The 09-18 headline only reproduces if you also drop 69 coins.** Re-running the study today gives a basket of 1.25x with 15 winners. Retention has pruned the stored flag prices for 69 coins, so the re-denomination check can no longer see them. One of them, `survivor-2`, shows 459x. Drop those 69 and the basket is 0.99x vs BTC 1.04x with 14 winners, which matches the study. `gecko/backtest/study.py:145` keeps coins with no stored price, so it has the same hole.
2. **F4 reproduces exactly, and it is still fragile.** It has n=101, mean 1.91x, median 0.53x, ex-top-3 0.75x and a 90% CI of 0.88-3.19. Its 3 winners carry the whole result, and 49% of its coins halve. It ran below BTC in 3 of 6 entry months. A threshold sweep shows no dose-response: raising the 7-day volume ratio from 2 to 5 lowers the mean.
3. **The strongest descriptive split is entry market cap.** F4 coins at $5M or more: n=46, mean 3.54x, median 0.86x, CI 1.29-6.16. F4 coins under $5M: n=52, mean 0.54x, and 63% halve. This split was found after the fact, on the same data.
4. **CoinGecko surfaces are all dead.** predictions, slow_burn, gainer_acceleration, momentum_7d, gainers, trending and category last wrote on 2026-09-19. volume_spikes last wrote on 09-21.
5. **Since 09-19, discovery has been almost entirely Solana DEX new pools.** `dex_pool_discoveries` added 144,720 pools since 09-19, and only 472 had liquidity of $250K or more at discovery. It has one row per pool and no time series.
6. **No long-horizon price history exists after 09-19.** The only time series are rolling windows: volume (14 days), txns (30 days) and score (21 days). The outcome ledger stops at 7 days, and `price_cache` holds only the latest price. A point-in-time backtest of a new rule for the post-09-19 period cannot be done from the legacy DB alone.
7. **The Telegram history cannot rank callers at 30 days or more.** Only 28 of 4,052 `source_calls` rows have a call-time price, and the snapshot runner only covers 24 hours. Using the study charts and the latest cached price, n is 2-27 per channel and the medians are 0.14-0.58x at 30-90 days. The spec's `caller_quality` feature has no usable history to start from.
8. **159 of the 1,800 study coins were flagged only by control-arm predictions.** `is_control=1` marks randomly chosen baseline coins, not real flags, and 1,558 of 3,352 prediction rows have it. The study universe and the harness both include them.
9. **The spec's backtest substrate does not exist yet.** `/root/gecko-history/` is absent. The tables spec §10 says the harness reads, `gainers_snapshots` and `trending_snapshots`, hold only 7 days and 3 days of September data.
10. **Recommendation.** Freeze a copy of `scout.db` plus `/root/xs_study` now. Fix the fail-open stored-price exclusion. Pre-register F4 with a $5M entry-cap floor as a forward test only. Do not use caller quality as a ranking input until there is a call-price backfill.

---

## 1. Inventory

Status means live if the table was written within 7 days of 2026-10-16. Run `inv.py`, which does count(*) and min/max of the primary timestamp for all 104 tables. The table below lists only the ones that hold discovery, social-call, price or outcome data.

| Table | Rows | First | Last | Status / note |
|---|---:|---|---|---|
| predictions | 3,352 | 04-09 | 09-19 | DEAD (CG). 1,558 are control arm |
| paper_trades | 2,234 | 04-21 | 08-09 | DEAD. 2 rows have `opened_at`=1970 sentinel |
| velocity_alerts | 460 | 04-18 | 06-22 | DEAD |
| slow_burn_candidates | 1,066 | 05-10 | 09-19 | DEAD (CG) |
| gainer_acceleration | 1,273 | 06-02 | 09-19 | DEAD (CG) |
| momentum_7d | 36 | 09-16 | 09-19 | DEAD and pruned (30-day retention). The study saw 324 events from 08-20 |
| volume_spikes | 128 | 09-02 | 09-21 | DEAD and pruned (45 days). The study saw 465 events from 08-04 |
| gainers_snapshots / losers_snapshots | 17,649 / 12,916 | 09-12 | 09-19 | DEAD. Only the last 7 days survive |
| trending_snapshots | 2,205 | 09-16 | 09-19 | DEAD. Only 3 days survive |
| category_snapshots / narrative_signals | 124,347 / 7,180 | 09-12 / 04-09 | 09-19 | DEAD (CG) |
| volume_history_cg | 0 | – | – | EMPTY |
| holder_snapshots, social_signals, cryptopanic_posts, curve_launch_* , venue_listings, wallet_snapshots, second_wave_candidates, perp_anomalies | 0 | – | – | EMPTY |
| dex_pool_discoveries | 403,753 | 07-26 | 10-16 | LIVE. Solana only, one row per pool, `goplus_safe` always NULL |
| candidates | 431 | 10-09 | 10-15 | LIVE, rolling. 342 of 431 are Solana |
| entry_mcap_snapshots | 10,067 | 06-29 | 10-15 | LIVE |
| signal_first_seen | 7,790 | 08-09 | 10-15 | LIVE |
| volume_snapshots | 424,906 | 10-02 | 10-16 | LIVE, 14-day rolling window, ~1/min, 1,011 tokens |
| txns_h1_buys_snapshots | 906,234 | 09-16 | 10-16 | LIVE, 30-day rolling, DexScreener and GeckoTerminal |
| score_history | 639,679 | 09-25 | 10-16 | LIVE, 21-day rolling |
| signal_events | 425,237 | 10-02 | 10-16 | LIVE, 14-day rolling |
| signal_outcome_ledger | 630,997 | 07-03 | 10-16 | LIVE. Forward labels r15m to r7d and peak7d only |
| price_cache | 90,146 keys | 04-15 | 10-16 | LIVE for `dex:` keys only (latest price, overwritten). CG slugs frozen at 09-21 |
| conviction_watchlist_snapshots | 402,861 | 07-18 | 10-02 | DEAD since 10-02. The runs table is still written |
| tg_social_messages | 3,897 | 2025-10-16 | 10-15 | LIVE. 2 of 8 channels are still posting |
| tg_social_signals | 1,912 | 04-28 | 10-15 | LIVE |
| source_calls | 4,052 | 04-28 | 10-15 | LIVE. 2,140 are X rows, dead since 06-23 |
| source_call_price_snapshots | 9,865 | 07-31 | 10-15 | LIVE. Only 83 identities |
| narrative_alerts_inbound (X) | 2,140 | 05-14 | 06-24 | DEAD |
| outcomes / alerts | 33 / 33 | 05-01 | 06-13 | DEAD |
| contract_coin_map | 402,582 | 06-29 | 10-16 | LIVE, but 401,434 rows have NULL coin_id. Only about 1,180 are real mappings |

## 2. Discovery breadth

The table counts distinct ids first seen per month in each surface. Months with a dash had no rows. Run `disc.sql`.

| Surface | Apr | May | Jun | Jul | Aug | Sep | Oct | Dead since |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| predictions (CG) | 511 | 215 | 61 | 57 | 41 | 46 | – | 09-19 |
| paper_trades | 388 | 303 | 39 | 68 | 27 | – | – | 08-09 |
| velocity_alerts | 55 | 123 | 55 | – | – | – | – | 06-22 |
| slow_burn (CG) | – | 225 | 156 | 155 | 100 | 104 | – | 09-19 |
| gainer_acceleration (CG) | – | – | 178 | 151 | 148 | 140 | – | 09-19 |
| momentum_7d (CG, pruned) | – | – | – | – | – | 18 | – | 09-19 |
| volume_spikes (CG, pruned) | – | – | – | – | – | 91 | – | 09-21 |
| gainers_snapshots (pruned) | – | – | – | – | – | 210 | – | 09-19 |
| trending_snapshots (pruned) | – | – | – | – | – | 76 | – | 09-19 |
| narrative_alerts_inbound (X) | – | 262 | 176 | – | – | – | – | 06-24 |
| conviction_watchlist | – | – | – | 475 | 151 | 148 | – | 10-02 |
| tg_social_signals (resolved tokens) | 79 | 174 | 122 | 38 | 29 | 49 | 16 | live |
| entry_mcap_snapshots | – | – | 151 | 3,074 | 2,851 | 3,004 | 988 | live |
| signal_first_seen | – | – | – | – | 3,450 | 3,343 | 998 | live |
| dex_pool_discoveries (Solana tokens) | – | – | – | 17,792 | 144,687 | 158,111 | 80,806 | live |

The outcome ledger tells the same story. In `signal_outcome_ledger`, the surfaces `gainers_early`, `losers_contrarian`, `trending_catch`, `first_signal` and `volume_spike` all stop in September. Only `dex_new_pool` (16,143 tokens in Oct) and `chain_completed` (829) keep emitting.

These are the only discovery records added after 09-19.

- **dex_pool_discoveries.** 144,720 Solana pools, of which 472 had liquidity of $250K or more at discovery. No other chain is present.
- **entry_mcap_snapshots, signal_first_seen and candidates.** About 1,000 tokens in October. These come from the DEX ingestion path.
- **Telegram.** About 65 resolved tokens across Sep and Oct, from @thanos_mind and @detecter_calls only.

There is no base, ethereum or bsc DEX discovery record at all. Spec S2 assumes four chains, so those three chains start from zero history.

## 3. Telegram callers

There are 11 configured channels. Five have been removed, and only 2 have posted since 06-25. Run `tg.sql`.

| Channel | Msgs | First | Last | With contract | With cashtag | Either | Resolved distinct tokens |
|---|---:|---|---|---:|---:|---:|---:|
| @nebukadnaza | 989 | 2026-02-10 | 06-21 | 284 | 49 | 308 | 211 |
| @detecter_calls | 937 | 2026-03-23 | **10-15** | 114 | 384 | 397 | 120 |
| @thanos_mind | 738 | 2026-03-09 | **10-15** | 100 | 442 | 454 | 124 |
| @cryptoyeezuscalls (removed 09-01) | 726 | 2026-04-15 | 06-25 | 105 | 408 | 411 | 41 |
| @Alt_Crypto_Gems (removed 09-01) | 277 | 2025-10-16 | 06-23 | 83 | 197 | 199 | 34 |
| @alohcooks (removed 09-01) | 203 | 2026-02-08 | 04-29 | 54 | 18 | 63 | 19 |
| @hatoriinvestments (removed 09-01) | 20 | 2026-07-03 | 08-31 | 0 | 5 | 5 | 1 |
| @SamuraisJournal | 7 | 2026-09-01 | 09-02 | 1 | 1 | 1 | 1 |
| @gem_detecter, @CallerFiona1, @cytuseth | 0 | – | – | – | – | – | – |

870 messages were posted more than 7 days before they were parsed, because the listener backfilled history when it started. `source_calls` has 735 rows ingested more than a day late, up to 189 days.

**Price snapshot substrate.** `source_call_price_snapshots` holds 9,865 rows covering only 83 identities. Fifteen-minute runs began on 07-31, and the visibility epoch is 08-14. For 78 of the 83 identities the snapshots span less than 7 days. The `source_calls` forward fields only reach 24 hours.

| `source_calls` with | Call-time price | 1h | 6h | 24h |
|---|---:|---:|---:|---:|
| @thanos_mind | 19 | 13 | 13 | 13 |
| @detecter_calls | 9 | 7 | 7 | 7 |
| All other channels and all 2,140 X rows | 0 | 0 | 0 | 0 |

`outcome_status` is unresolvable for 3,707 of 4,052 rows, and 3,488 are cashtag-only calls. **The leaderboard the brief asked for cannot be built from the snapshot tables.**

**Best available proxy.** Run `callers.py`. It takes the first resolved call per channel and token, 551 in total. The call price comes from the study chart at the call time, or else from the resolution snapshot `price_usd`, which only the 161 newest signals have. The forward price comes from the chart at +7, +30 or +90 days, and also from the latest `price_cache`.

| Channel | First calls | Priced | +30d chart: n, median, %≥2x | +90d chart: n, median, %≤0.5x | Call to latest cached price: n, median, mean, %≥7x |
|---|---:|---:|---|---|---|
| @thanos_mind | 124 | 56 | 14, 0.40x, 0% | 7, 0.20x, 86% | 27, 0.53x, 1.12x, 4% |
| @detecter_calls | 120 | 57 | 20, 0.54x, 5% | 12, 0.14x, 75% | 25, 0.76x, 0.98x, 0% |
| @cryptoyeezuscalls | 41 | 9 | 9, 0.71x, 0% | 8, 0.43x, 50% | 8, 0.62x, 0.82x, 0% |
| @nebukadnaza | 211 | 4 | 4, 0.56x, 0% | 4, 0.46x, 50% | 4, 0.53x, 0.69x, 0% |
| @Alt_Crypto_Gems | 34 | 2 | 2, 1.25x, 0% | 2, 0.59x, 50% | 2, 0.60x, 0.60x, 0% |
| others | 21 | 1 | – | – | – |

- **The horizons the data supports.** 24 hours from the snapshots, for 28 calls. Up to 90 days only for the 79 calls on coins that also appear in the study charts. Those coins are a biased subset, because they were also flagged by a CoinGecko surface. The cached-price horizon varies from 1 to 143 days.
- **Verdict.** n is between 2 and 27 per channel. Every channel with n of at least 8 has a median below 0.8x at 30 days or more. That is not evidence of skill, and the n is too small to rank anyone.
- **Recoverable gap.** 395 of the 551 first calls are DEX identities with a contract address. Daily GeckoTerminal OHLCV backfill could price them. It is keyless, but its free-tier history depth is UNVERIFIED. This is the only route to a real caller history.

## 4. Outcome substrate

Run `charts.py`, `ts.sql` and `ev.py`.

- **Study charts.** `/root/xs_study/charts/*.json` holds 1,800 files, 74 MB, of CoinGecko `market_chart` data with `prices`, `market_caps` and `total_volumes`. They start at each coin's first-flag time, between 04-10 and 09-18, and end on 09-18. Granularity is mixed: the median chart has 150 points, the maximum has 2,159 hourly points, and 100 charts have gaps longer than 3 days. 40 charts end before 09-10, so those coins' "hold to now" multiple uses their last listed price, not zero. `btc.json` covers 04-01 to 09-17 in 170 daily points.
- **Study v2 coin files.** `/root/xs_study/v2/coins/*.json` holds 1,800 files with the top pool today, `pool_created_at`, current reserve, and up to 20 daily GeckoTerminal candles before t0. 1,610 have OHLCV status 200, and 1,411 have candles. The pool is today's top pool, not the pool that traded at t0, as the prereg disclosed.
- **Extract DB.** `/root/xs_study/extract/history_prices.db`, made 10-09, holds the seven flag tables' (coin, time, price) rows. It was made after the pruning, so it is also missing 68 coins.
- **Price history in the legacy DB.** None at daily resolution or coarser after 09-19. `volume_history_cg` is empty. `price_cache` is latest-only. `signal_outcome_ledger` gives r15m, r1h, r4h, r24h, r7d and peak7d: 481,736 rows are complete and 334,465 have r7d. That is real 7-day forward substrate for 80k Solana new pools, but it says nothing about 30-180 day holds.

DEX time series and their cadence:

| Table | Content | Cadence | Window kept |
|---|---|---|---|
| volume_snapshots | `volume_24h_usd` per contract | about 1 per minute, 1,361 distinct minutes/day, 740 tokens with 100+ rows | 14 days |
| txns_h1_buys_snapshots | h1 buys and sells | per scan | 30 days |
| score_history | scorer output | per scan | 21 days |
| dex_pool_discoveries | fdv, liquidity, h1 volume at discovery | once per pool | permanent |
| source_call_price_snapshots | GeckoTerminal price | 15 min, 83 identities | permanent |

**Can a new rule be backtested point-in-time for the post-09-19 period from the legacy DB alone? No.**

- **Entry features.** These exist point-in-time only for Solana new pools: liquidity, fdv, h1 volume and `pool_created_at` at discovery. Volume and txns history exist only inside the rolling windows.
- **Outcomes.** There are no 30, 90 or 180 day forward prices. Outcomes beyond 7 days need external OHLCV.
- **Act now.** The rolling windows lose data every day.

## 5. Strategy evidence

The scripts for this section are `xs.sh`, which runs the original `analyze2.py` and `robust.py`, and `xs2.py`. There are no saved result files under `/root/xs_study` apart from `probe/probe_results.json`, so every number below was recomputed today.

**Reproduction caveat.** The study keeps any coin with no stored price.

- **The original scripts today.** They report R0 at 1.25x, 15 winners and 53 coins excluded for a bad price ratio. The 09-18 report said 0.99x, 14 winners and 54 excluded.
- **Why they differ.** `audit.py`'s stored-price lookup no longer finds 69 coins, 67 of them flagged by `volume_spike`. `rerun.py` now crashes with a `KeyError` on `dfdv-xstock`. `analyze2.py` quietly treats missing as passing the check.
- **After dropping the 69.** R0 is n=1,668, mean 0.99x, median 0.73x, BTC 1.04x, 14 winners. That matches the study. F4 holds 98 of the 101 coins either way.

**Pre-registered rules.** Pass criterion 3 compares each rule with the all-flagged R0 mean for the same entry months.

| Rule | n | Mean | Median | BTC | Ex-best | Same-month R0 | %≤0.5x | 7x+ | Result |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| R0-R5 (v1) | 337-1,737 | 1.06-1.25 | 0.66-0.75 | 1.03 | 0.96-1.09 | – | 26-38% | – | all fail |
| F1 age ≤ 90d | 562 | 1.08 | 0.58 | 1.06 | 0.99 | 1.42 | 46% | 10 | fail on 2,3,4,5 |
| F3 vol ratio ≥ 2 | 419 | 1.20 | 0.68 | 1.06 | 1.08 | 1.27 | 34% | 4 | fail on 3,5 |
| **F4 = F1 and F3** | **101** | **1.91** | **0.53** | **1.07** | **1.42** | **1.63** | **49%** | **3** | **PASS** |
| F2 young chain (contaminated) | 122 | 1.59 | 0.61 | 1.06 | 1.39 | 1.73 | 47% | 7 | fail on 3 |

**F4 robustness.**

- **Overall.** Ex-top-1 is 1.42x, ex-top-3 0.75x, ex-top-5 0.65x. The bootstrap 90% CI is 0.88-3.19, and P(mean ≤ BTC) is 0.095.
- **By month.** April 4.42x (n=18), May 0.45x, June 1.15x, July 0.74x, August 2.78x, September 0.97x.
- **Carried by three coins.** stonk-3 at 51x, bitway at 44x and lobster-2 at 25x.
- **Excluding Robinhood.** n=70, mean 2.44x, ex-top-3 0.75x. F4 on Robinhood alone is n=31, mean 0.73x. F4 missed the Robinhood winners (pons, hookr-fun, netnet and others) because they have no candles before their flag date, and several show a negative age (trap 15).

**Threshold sweep.** This is descriptive only and was not pre-registered.

- **Too few winners to choose a threshold.** All 20 (age, volume-ratio) cells have between 1 and 3 coins at 7x or more. Their ex-top-3 means are 0.49-0.75, so no cell clears 1.0 once the top 3 coins are removed.
- **No dose-response.** By volume-ratio bucket the means are 1.01, 0.98, 2.62, 1.38 and 1.03. The 1-2 bucket at 2.62x is driven by outliers, with ex-top-3 at 0.84. Ratios of 5 or more have the worst halving rate, 41%.

**Descriptive cuts on the 1,668 verifiable coins.** Run `xs2.py`. About 60 cuts were run, so expect several CIs to exclude BTC by chance.

| Cut | n | Mean | Median | Ex-top-3 | %≤0.5x | 90% CI |
|---|---:|---:|---:|---:|---:|---|
| F4 and entry mcap ≥ $5M | 46 | 3.54 | 0.86 | 0.99 | 33% | 1.29-6.16 |
| F4 and entry mcap < $5M | 52 | 0.54 | 0.40 | 0.42 | 63% | 0.41-0.70 |
| Entry mcap $1-5M (all) | 371 | 0.79 | 0.60 | 0.67 | 44% | 0.67-0.92 |
| Entry mcap $25-100M (all) | 348 | 1.13 | 0.83 | 0.92 | 19% | 0.92-1.41 |
| momentum_7d surface | 100 | 2.71 | 1.10 | 1.75 | 22% | 1.78-3.83 |
| momentum_7d, excluding Robinhood | 70 | 2.63 | 1.15 | 1.41 | 16% | 1.49-4.14 |
| ≥ 3 distinct surfaces | 514 | 1.37 | 0.75 | 1.11 | 28% | 1.09-1.68 |
| Exactly 1 surface | 742 | 0.76 | 0.72 | 0.73 | 35% | 0.71-0.81 |
| velocity surface | 211 | 0.78 | 0.46 | 0.54 | 52% | 0.53-1.17 |
| Solana single-chain coins | 274 | 0.85 | 0.47 | 0.63 | 51% | 0.61-1.23 |
| Token age 0-30 days | 261 | 0.97 | 0.43 | 0.78 (ex-best) | – | – |

- **Month regime.** Entry-month means vs BTC were 0.98/1.00 (Apr), 0.72/0.97 (May), 0.88/1.20 (Jun), 1.30/1.19 (Jul), 1.62/1.19 (Aug) and 0.93/0.97 (Sep). Only July and August beat BTC, and Robinhood drives August.
- **Chain.** Robinhood is n=109 with mean 1.70x and 7 winners. Ethereum and base single-chain coins all fall below BTC, at 0.73x and 0.76x.

**Candidate features for a v2 prereg.** These are ranked by evidence per unit of test cost. All of them were found in-sample, so each prereg must be a forward test with a frozen hash, not a pass on these data.

1. **Entry-cap floor on F4 (F4 plus mcap at entry ≥ $5M).**
   - *Data:* mcap at entry. It is point-in-time in the study charts' `market_caps`, and forward from DexScreener `marketCap`/`fdv`.
   - *Prereg:* F4 vs F4-with-floor on watchlist entrants, 90-day return vs BTC, n ≥ 30 per arm. Pass if the floor arm's median beats the no-floor arm's AND its ex-top-3 mean is above BTC.
   - *Cheapest of the five:* it is one comparison in the existing ledger.
2. **Multi-surface or multi-collector confluence.** Example: sighted by 2 or more independent collectors within 14 days.
   - *Evidence:* R4 and R5 failed their prereg. The gradient is consistent, though: a single surface gives 0.76x and three or more give 1.37x, with ex-top-3 at 1.11.
   - *Data:* the `observations` source mix in gecko.db. In the legacy DB it is only reconstructable for the dead CoinGecko surfaces.
   - *Prereg:* rank-tier comparison at 30 and 90 days.
3. **Prior 7-day price momentum, as context or a filter.**
   - *Evidence:* the strongest median, 1.10x with ex-top-3 1.75x. It comes from a single month (08-20 to 09-18), and its rows are now pruned to 36.
   - *Data:* forward needs 7-day OHLCV from GeckoTerminal. In the legacy DB it is gone except inside `events.json` and the charts.
   - *Prereg:* spec §6 bars `momentum_ctx` as a rule input in v1, so this needs a new prereg and at least 2 regimes.
4. **Negative filters, the cheapest to adopt as disqualifiers to test.**
   - *Candidates:* entry mcap under $5M, the velocity-style 1-hour pump trigger, and token age of 30 days or less with no volume confirmation.
   - *Prereg:* that the excluded arm underperforms the kept arm at 90 days.
5. **Caller quality: NOT supported.** The data is too sparse and what exists is negative (section 3). Keep it at zero weight until a GeckoTerminal backfill gives at least 30 priced calls per channel at 30 days.

The data does not support any claim of an edge. F4 remains a forward test, as spec §7 says.

## 6. Data-quality traps

Each trap below was verified with a query, mostly in `traps.sql`.

| # | Trap | Evidence | Mitigation |
|---|---|---|---|
| 1 | **The re-denomination exclusion fails open as retention prunes.** | 53 coins have a bad ratio. 69 have no stored price: 67 flagged by `volume_spike` and 2 by `momentum_7d`. `survivor-2` shows 459x with $0.001M mcap. `study.py:145` keeps coins with no stored price. | Exclude coins with no stored price, or freeze the stored-price map from a pre-prune snapshot. The /root snapshots from before 08-08 may hold them (UNVERIFIED). |
| 2 | **Control-arm predictions sit in the universe.** | 1,558 of 3,352 predictions have `is_control=1`. 159 study coins were flagged only by control predictions. | Filter `is_control=0`, or label control coins and treat them as a baseline. |
| 3 | **Retention has erased surfaces the spec relies on.** | momentum_7d has 36 rows vs 324 study events. volume_spikes has 128 vs 465. gainers_snapshots keeps 7 days, trending 3 days. `/root/gecko-history` does not exist. | Copy `scout.db` and `/root/xs_study` read-only before any further pruning or cutover. |
| 4 | **CoinGecko slugs sit in address columns.** | `contract_coin_map` has 29 slug addresses, `candidates` 2, `score_history` 5 distinct ids. `tg_social_signals.token_id` holds 426 slugs and 456 `dex:` keys. | Branch on the id shape. Never join slug to address. |
| 5 | **`contract_coin_map` is mostly empty.** | 401,434 of 402,582 rows have NULL coin_id: 401,390 come from `gt_new_pools` placeholders. | Do not treat a row as a mapping. Filter `coin_id IS NOT NULL`. |
| 6 | **Duplicate identities across chains.** | 223 coin_ids have addresses on more than one chain. 18 prediction symbols map to more than one coin_id. No address maps to two coin_ids. | Key on (chain, address) and keep the CoinGecko id separate, as spec §4 already does. |
| 7 | **EVM address case is mixed.** | `tg_social_signals` has 81 of 185 EVM addresses in mixed case, and `contract_coin_map` 15 of 1,010. `dex:robinhood:0xfC5A…` keys are mixed-case. No same-address/different-case duplicates exist yet. | Lowercase EVM addresses only, as in `gecko/identity.py`. |
| 8 | **Timestamp formats are mixed.** | `created_at` uses `YYYY-MM-DD HH:MM:SS` with a space. Most `*_at` columns use `T…+00:00`. `source_calls.call_ts` uses `.000Z` for X (2,140 rows) and `+00:00` for Telegram (1,912). `pool_created_at` uses `Z`. `signal_params.updated_at` is mixed. `tg_social_channels.removed_at` has 1 space-format row out of 6. | Normalize before any lexical comparison. A space sorts before `T`, so same-day comparisons go wrong. |
| 9 | **A 1970 sentinel in `paper_trades.opened_at`.** | 2 rows. 2 rows also have no entry price. | Use `opened_at > '2020'`, as the harness does. |
| 10 | **Telegram history is backfilled, not point-in-time.** | 870 messages were parsed more than 7 days after posting. Ingest delay reaches 189 days. Repeated calls are separate rows, for example 3 rows for LLL within 31 minutes. | Use `posted_at`/`call_ts` as the event time. Take the first call per channel and token. |
| 11 | **`resolved_state` vocabulary drifts.** | `RESOLVED` (426), `resolved` (35), `eligible_contract` (494) and `unresolved` (3,097) all appear. | Map the variants explicitly. |
| 12 | **Chart survivorship.** | 40 charts end before 09-10, so delisted coins are valued at their last price, not zero. | Value at zero those coins, or report a sensitivity check. |
| 13 | **`price_cache` is not history.** | It holds the latest price per key and is overwritten. CoinGecko slugs are frozen at 09-21. | Never use it as a dated price. |
| 14 | **DEX discovery is Solana-only.** | `dex_pool_discoveries` has only network=solana, and `goplus_safe` is NULL on all rows. | Do not use it to infer the chain mix or security status. |
| 15 | **Negative token ages in v2 (today's top pool, not the pool at t0).** | 175 of 1,555 coins have a `pool_created_at` after their first flag: 20 on Robinhood, 3 of them winners. F1 (age ≤ 90) counts them as young. F4 is unaffected, because none has candles before t0. Restricting F4 to age ≥ 0 gives n=101, 1.91x, CI 0.86-3.18. | Treat a negative age as unknown, not as young. |

## 7. Residual risks and open questions

- **Older snapshots.** Whether the operator-owned /root snapshots from before 08-08 still hold the pruned momentum_7d and volume_spikes prices is UNVERIFIED. If they do, they could restore the 69 stored prices.
- **GeckoTerminal backfill.** Whether keyless OHLCV history is deep enough for 90-180 day caller backfill is UNVERIFIED.
- **Pre-registration hash.** The F4 prereg hash `f7d38fa2…` matches `v2.sha`. This review did not alter it.

## Appendix A: query provenance

All scripts are in `docs/analysis/2026-10-16-data-review-queries/`. They ran read-only: `sqlite3 -readonly` or `mode=ro` URIs.

| Section | Script | What it does |
|---|---|---|
| 1 | `inv.py` | count(*) and min/max of the primary timestamp for every table |
| 2 | `disc.sql` | `select '<surface>', m, count(*) from (select <id>, substr(min(<ts>),1,7) m from <table> group by 1) group by 2;` for each surface. Also the ledger surfaces by month, and dex network by month |
| 3 | `tg.sql`, `tg2.sql`, `callers.py` | Channels, message counts, contract and cashtag coverage, `source_calls` price coverage, snapshot spans. `callers.py` builds the proxy leaderboard |
| 4 | `charts.py`, `ts.sql`, `ev.py` | Chart and v2 file spans, rolling-table cadence, events.json vs live-table row counts, control-arm count |
| 5 | `xs.sh` | Runs the original `analyze2.py` and `robust.py` on copies, with `audit.py` pointed at the extract DB. Adds the F4 threshold sweep |
| 5 | `xs2.py` | The 69-unverifiable split and the descriptive cuts |
| 6 | `traps.sql` | Each trap's count |

Key SQL, verbatim.

```sql
-- source_calls price coverage per caller (section 3)
select source_type, source_id, count(*) n, sum(price_at_call is not null) p0,
       sum(forward_24h_pct is not null) f24h
from source_calls group by 1,2 order by n desc;

-- snapshot span per identity (section 3)
select case when d<1 then '<1d' when d<7 then '1-7d' when d<30 then '7-30d' else '>=30d' end, count(*)
from (select identity_key, julianday(max(snapshot_at))-julianday(min(snapshot_at)) d
      from source_call_price_snapshots group by 1) group by 1;          -- 25 / 53 / 4 / 1

-- control arm (section 6, trap 2)
select is_control, count(*) from predictions group by 1;                 -- 0:1794  1:1558

-- empty mapping rows (section 6, trap 5)
select source, address_type, count(*) from contract_coin_map group by 1,2;  -- gt_new_pools/solana 401390

-- post-CG DEX discovery (section 2)
select count(*), sum(liquidity_usd>=250000) from dex_pool_discoveries
where first_seen_at>='2026-09-19';                                       -- 144720 | 472
```

The Python ratio check below is the one that finds the 69 coins. It reuses `audit.py`'s `stored` and `coins`.

```python
miss=[x for x in live if x["cid"] not in stored]                          # 69 (live DB), 68 (extract)
bad =[x for x in live if x["cid"] in stored and not 0.5<=x["p0"]/stored[x["cid"]][1]<=2]   # 53
```
