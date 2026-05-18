**New primitives introduced:** NONE.

# BL-NEW-DEX-PRICE-COVERAGE — Audit-Phase Findings 2026-05-18

**Data freshness:** Computed against srilu prod scout.db 2026-05-18. Originating BL filed 2026-05-12. Rebase note: the separate held-position refresh-rate follow-up identified below has since shipped via PR #158; that does not change this audit's DEX-coverage deferral.

**Source:** srilu `/root/gecko-alpha/scout.db`. Worktree HEAD = `cdeb31f` = origin/master.

**Drift-check:** worktree HEAD = origin/master (zero divergence). Grep for `price_cache|update_price_cache` returns 11 files under `scout/` — all consumers + the cache itself; no parallel session.

**Hermes-first verdict:** In-tree ingestion pipeline. No Hermes skill covers "per-token DEX price aggregation into a project's SQLite cache." awesome-hermes-agent reachable; `mercury` (Solana cash-flow analyzer) + `hermes-blockchain-oracle` exist but neither writes to gecko-alpha's `price_cache` table — that's project-internal storage. No external primitive applies.

## TL;DR

**Coverage gap remains empirically dormant — same framing as 2026-05-12. Status flip PROPOSED → DEFERRED-WITH-UPDATED-EVIDENCE.**

Originating concern (2026-05-12): DexScreener/GeckoTerminal don't write to `price_cache`; pure-DEX-discovered tokens with non-CG token_ids would have no cache row. Filed conditionally on "promote out of deferred state if a future audit shows contract-addr-shaped tokens accumulating in the held cohort."

Today: **148 open paper_trades, 100% cg-coin-id shape, 0% contract-addr shape.** The "DEX-discovered without CG listing" cohort is structurally empty. Same shape as 2026-05-12's 0/150.

The 21 stale-cache opens that DO exist are a **different bug** — cg-coin-id tokens whose CoinGecko held-position refresh lane missed them in the last 24h. That's a refresh-rate gap in `scripts/held_position_prices.py` (BL-NEW-HELD-POSITION-REFRESH), NOT the DEX-coverage gap this entry targets.

## Empirical evidence

### Open paper_trades by token_id shape

| Shape | Count | Verdict |
|---|---|---|
| `cg-coin-id` (CoinGecko format) | **148** | 100% — DEX coverage gap empirically irrelevant |
| `eth-style-contract` (`0x...`) | 0 | — |
| `solana-mint-shape` (32-44 char base58) | 0 | — |
| `other` | 0 | — |

### price_cache freshness on open paper_trades

| Freshness | Count | Note |
|---|---|---|
| fresh ≤30m | 104 | 70% — held-position refresh lane working |
| ≤6h | 14 | acceptable |
| ≤24h | 9 | acceptable |
| **stale > 24h** | **21** | refresh-rate gap, NOT DEX-coverage gap |
| no_cache_row | 0 | every open has SOME cache row |

### Stale opens by signal_type

| signal_type | stale count | shape |
|---|---|---|
| gainers_early | 11 | cg-coin-id |
| narrative_prediction | 5 | cg-coin-id |
| losers_contrarian | 3 | cg-coin-id |
| chain_completed | 2 | cg-coin-id |

ALL 21 stale opens are cg-coin-id shape. None match the DEX-coverage scenario.

## Why DEX-coverage gap stays DEFERRED

The originating BL's acceptance criterion was conditional: "promote out of deferred state if a future audit shows contract-addr-shaped tokens accumulating in the held cohort."

Today's audit:
- 0 of 148 opens are contract-addr shape (vs 0 of 150 in 2026-05-12)
- The cohort composition has been stable for 6+ days
- Implementing the DEX-coverage fix today would be ~2-4h of work protecting 0% of the cohort

Per CLAUDE.md §10 heuristic discipline: don't ritualize the fix into a phantom-protection ship. The framing remains correct.

## Different finding: refresh-rate gap surfaced (21 stale-cache opens)

The 21 cg-coin-id opens with stale_gt_24h cache freshness ARE a real coverage gap — but NOT the DEX-coverage class. They're the held-position refresh lane's CG-ingestion rate. Per the prior LC-bleed drill (2026-05-17 PR #150 evidence), the held-position refresh lane shipped 2026-05-12 (`scripts/held_position_prices.py` per BL-NEW-HELD-POSITION-REFRESH) covers ~80% of opens (35/44 fresh ≤30m in 2026-05-17 LC subset; 104/148 fresh in 2026-05-18 full opens).

This 14% stale rate (21/148) suggests the held-position refresh isn't reaching every cg-coin-id at the desired cadence. Possible causes:
- Refresh rate (current default may be insufficient for all 148 opens within 24h)
- Refresh ordering (LIFO vs FIFO may starve older opens)
- Rate-limiter contention with primary CG-markets ingestion

`BL-NEW-HELD-POSITION-REFRESH-RATE-GAP` is the correct separate follow-up — distinct from the DEX-coverage gap this audit targeted. That follow-up has since shipped via PR #158, so this PR should not add a duplicate backlog entry.

## Re-evaluation triggers (DEX-coverage gap)

Re-run when:
1. Any 30d window shows contract-addr-shaped tokens accumulating in `paper_trades.status='open'` (currently 0)
2. A pure-DEX signal source (e.g., DexScreener pool-discovery without CG listing) is added to the scorer
3. 2026-08-18 (90d calendar backstop)

## Cross-references

- `backlog.md` BL-NEW-DEX-PRICE-COVERAGE (originating L846; flipping to DEFERRED-WITH-UPDATED-EVIDENCE)
- `tasks/findings_open_position_price_freshness_2026_05_12.md` (originating triage data)
- `tasks/plan_held_position_price_freshness.md` (Alt A design pass that filed this BL as follow-up)
- `scripts/held_position_prices.py` (BL-NEW-HELD-POSITION-REFRESH — the lane that covers cg-coin-id refresh)
- 2026-05-17 PR #150 LC-bleed drill (§"Stale price 6 opens" finding — related but smaller subset)
