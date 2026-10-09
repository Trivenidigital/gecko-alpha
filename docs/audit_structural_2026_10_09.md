# gecko-alpha structural audit — 2026-10-09

Scope: origin/master @ 5bbe8e5f (worktree C:\projects\ga-audit-20261009) plus prod runtime state on srilu-vps (read-only queries). Companion: `docs/research_hermes_crypto_ecosystem_2026_10_09.md`.

## 0. Prod runtime facts (queried 2026-10-09 ~02:40 UTC)

| Fact | Evidence |
|---|---|
| Prod HEAD = 7775189 on branch `docs/dashboard-health-release-plan-20260914`; NOT an ancestor of origin/master (48 commits behind, on a side line) | `git log` on VPS; `git merge-base --is-ancestor` locally |
| **CoinGecko subscription DEACTIVATED** — `GET /key` → HTTP 401, error_code 10004 "API subscription deactivated". Every CG call 401s; month-to-date credits 0 over 8,874 attempts | curl from VPS; `cg_http_error` journal lines |
| CG-derived surfaces dark since 2026-09-19: trending_snapshots, gainers_snapshots, predictions, narrative_signals last rows 09-19; volume_spikes 09-21; volume_history_cg = 0 rows | sqlite max() per table |
| Alive sources: DexScreener boosts (14 candidates/cycle), GeckoTerminal, dex_pool_discoveries (161K rows / 30d), Telegram social (369 msgs / 30d, 176 signals) | journal + sqlite |
| X/KOL narrative inbound (Hermes-fed) last row 2026-06-24 — dead for 3.5 months while the Hermes cron reports `last_status: ok` hourly (it is a `no_agent` shell script) | narrative_alerts_inbound; jobs.json |
| Last alert fired 2026-06-13; last paper trade opened 2026-08-09; alerts all-time = 33; candidates all-time = 553 | sqlite |
| signal_params: 8 of 9 signal types `enabled=0` (6 auto-suspended `hard_loss`, 2 operator); only `narrative_prediction` enabled | sqlite |
| 6.8-day uptime window: 208,615 tokens scanned → 16 candidates promoted → 0 alerts | pipeline heartbeat log |
| `conviction_gate_retired` logged every cycle; stages 4-6 of the original design (MiroFish → gate → alert) do not execute | journal |
| Prod .env has TRADING_ENABLED, TG_SOCIAL_ENABLED, CHAINS_ENABLED, NARRATIVE_ENABLED, KRAKEN_PILOT_ENABLED = true, SOLANA_MODE=SUPERVISED_LIVE, LIVE_MODE=shadow | `.env` flag names/values (no secrets read) |
| DB: 1.63 GB, 102 tables; signal_events 428K rows, alert_events 23K, paper_trades 2,234 | sqlite |
| Hermes: v0.21.6 is current upstream (2026-10-08); VPS runs v0.19.1 under `gecko-agent`, v0.14.0 under root; gateway active with no messaging platforms; model = openrouter/kimi-k2-thinking; no MCP servers configured; 5 gecko skills all `status: DRAFT` | VPS files |
| Ops surface: 21 systemd unit files, 19 crontab lines, 22 `*watchdog*` scripts, 6 in-process watchdog modules | repo + crontab |
| Local git: ~180 worktrees registered against this repo | `git worktree list` |

## 1. Size

| Area | Files | LOC |
|---|---|---|
| scout/ | 231 | 103,064 |
| tests/ | 533 | 194,877 |
| scripts/ | 55 | 23,152 |
| dashboard/ (py) | 13 | 10,287 (+~13K JS) |
| tasks/ | 385 | — |

scout/ LOC by purpose: detection ~18.2K (18%) · trading + live execution ~48.9K (47%) · audit/observability/db ~21.0K (20%) · config+main 6.9K · social 6.3K. db.py alone is 13,789 lines with 103 CREATE TABLE and 73 migration functions.

Tests: ~70-75% test process/observability/execution machinery; ~12-15% test detection.

Last 30 days of master commits: almost entirely docs about review receipts, clearances and closeouts.

## 2. What actually runs (main loop, 60 s)

DexScreener + GeckoTerminal + CG lanes → trackers (volume_spike, gainers, losers, momentum_7d, slow_burn-detect, acceleration, DEX discovery, perp) → scoring → `trade_first_signals` → detection-alert lane → conviction gate (**retired**, returns immediately). GoPlus safety is only reached inside the retired path.

Structural mismatch: the scorer corpus is $10K-$500K; the watcher corpus runs to $200-500M; paper admission is $5M-$200M and `trade_first_signals` explicitly skips DEX memecoins (`trading/signals.py:841-847`). The scorer either sees a token in a band trading ignores, or scores ~0 in the band trading uses (`findings_missed_gainers_gap_2026_06_02.md`).

Paper engine: 48 h max hold, TP 20 / SL 15. It measures a product the owner does not want (goal: hold ~6 months for 7x+). All paper signal lanes are net negative; `peak_fade` exits look profitable only by selection.

## 3. Signal catalog verdict

Scorer (14 signals) and detectors (12) are almost entirely **lagging** price/volume momentum (vol/liq, momentum_ratio, vol_acceleration, trending ranks, score_velocity, gainers, acceleration, secondwave, chains). The potentially **leading** inputs — holder growth (Moralis/Helius-gated), token age / new pools (research-only), curve launches (rh_pons, observe-only, emits no candidates), Telegram caller calls (quarantined), X/KOL narrative (dead since June) — are all off, key-gated, observe-only or dead.

The one rule that has ever passed a pre-registration (2026-09-18 v2 study, F4: token_age ≤ 90 d AND DEX 7-day volume ≥ 2× prior 7 days; n=101, mean 1.91× vs BTC 1.07×) is **not implemented as a surface**. It is fragile (ex-top-3 0.75×, median 0.53×) and earned a forward paper test, not live money.

## 4. Hotspots (top 15 by LOC) and verdicts

| Module | LOC | Verdict |
|---|---|---|
| scout/db.py | 13,789 | FOLD → ~6-10 tables, fresh schema; keep old DB read-only |
| live/solana_lane.py | 7,819 | RETIRE from the agent (operator CLI) |
| live/kraken_pilot.py | 5,687 | RETIRE (one 0.0002 BTC trade ever) |
| config.py | 3,612 | FOLD (~70 `*_ENABLED` flags) |
| main.py | 3,247 | FOLD into a small loop / Hermes cron |
| live/kraken_adapter.py | 2,435 | RETIRE |
| trading/combo_refresh.py | 2,170 | RETIRE (parole machinery for disabled signals) |
| ingestion/rh_pons.py | 2,000 | FOLD to a thin source (young-chain launches were 7 of 14 winners) |
| social/telegram/listener.py | 1,801 | KEEP concept, simplify |
| trading/signals.py | 1,500 | RETIRE most |
| trading/evaluator.py | 1,488 | FOLD (48 h exits ≠ 6-month holds) |
| social/telegram/caller_features.py | 1,202 | KEEP/FOLD (caller quality is leading) |
| outcome_ledger.py | 1,109 | FOLD to one forward-return table |
| trading/detection_alert.py | 1,075 | FOLD into one alert path |
| chains/tracker.py | 1,033 | RETIRE |

Tables: 103; ~45 are audit/ledger/receipt tables; 4 have no writer (detection_receipt_cohorts, social_signals, symbol_aliases, venue_overrides).

## 5. Existing Hermes footprint

Runs on srilu-vps only under `gecko-agent`. One hourly cron (`gecko-x-narrative-scanner`, `no_agent=true` shell script) posting HMAC-signed events to `/api/narrative-alert`; five DRAFT skills (crypto_narrative_scanner, kol_watcher, narrative_classifier, coin_resolver, narrative_alert_dispatcher); hub skills solana (guarded read-only), evm (reference only), rest-graphql-debug (quarantined). Hermes owns no part of detection, scoring, delivery or trading. The X/KOL scanner is the most agent-shaped component and has been silently dead since 2026-06-24.

## 6. Simplification opportunities (ranked by LOC removed per unit of detection risk)

1. Delete scout/live/ (32.3K) + ~35K test LOC — zero detection risk; not in the detection path.
2. Delete retired gate/MiroFish/conviction-alert path.
3. Replace paper engine with watchlist + forward-return ledger (1d/7d/30d/90d/180d) — ~14K LOC + ~32K tests. **Needs operator ruling.**
4. Collapse db.py to ~6-10 tables (~12K LOC); keep historical DB read-only for backtests.
5. Drop the audit/observability layer except one heartbeat + one freshness check per table (~10K LOC + ~50K tests; 11 of 16 cron watchdogs).
6. Retire duplicate lagging detectors (~5K LOC); keep one "momentum context" feature.
7. Shrink dashboard (~24K) to one watchlist view or a Telegram digest.
8. Collapse ~70 flags to ~15 settings.
9. One universe band instead of three (medium risk; backtest against gainers_snapshots).
10. Promote leading inputs (caller ledger, holder growth, new-pool/curve first-seen, smart-money) into the agent — highest value, least proven.

## 7. Residual risks

- Historical DB (gainers_snapshots 17.6K, trending_snapshots, dex_pool_discoveries 366K, /root/xs_study charts for 1,800 coins) is the most valuable asset for validating a rebuild. Preserve before any schema change.
- Test-area shares are filename heuristics.
- CoinGecko reactivation is a billing decision; design must not assume Pro.
