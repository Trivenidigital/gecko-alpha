**New primitives introduced:** `contract_coin_map` table + resolver (I1), `entry_mcap_snapshots`
table (I2), `txns_h1_buys_snapshots` table + `txns_growth_1h` derivation (I3), one
`dex_measurability_coverage` metric, and three table-freshness watchdogs (§12a). All
observe-only: no scorer, gate, threshold, or alert behavior changes.

# Spec — DEX-outcome instrumentation substrate (I1 / I2 / I3)

**Date:** 2026-06-28
**Status:** IMPLEMENTATION SPEC — observe-only substrate. **Not** a gate/alert/threshold change.
**Evidence base:** `findings_same_asset_under_gate_cohort_30d_2026_06_28.md` (F1)
**PR for the findings that motivate this:** #383 (docs)
**Goal:** make the DEX-stage cohort *measurable* so a future gate recalibration / lever choice can be
made on evidence instead of guesswork.

## Why this exists (one paragraph)

F1 proved the cleanly-measurable CG-native corpus is not where the early-catch prize lives (3/672 ran
≥10×, all scored ≤18; 80-day `predictions` max move +91%). The prize lives in the DEX-mint corpus
(ANSEM: 477× at DEX stage vs 21× at CG stage) — which is **unmeasurable today** because (B1) no durable
`contract↔coin_id` link exists, (B2) entry-mcap is pruned at 7 days, and (B3) the accumulation proxy
isn't captured. This spec builds exactly the substrate that removes B1–B3, and **nothing else**.

## Scope guardrails (operator-imposed, carried forward)

- ❌ No prod threshold change / gate recalibration. ❌ No alerting behavior change. ❌ No paid
  Helius/Moralis. ❌ No slow-burn lane. ❌ No scorer signal wired to affect score (the proxy is
  captured but NOT yet scored).
- ✅ Everything here is read-only/observe-only capture. ✅ Free-tier-first.
- ✅ Gate recalibration stays blocked until this substrate soaks 2–4 weeks and the DEX cohort can be
  re-measured.

---

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| On-chain identity resolution (contract↔coin_id) | None found (hub catalog has no crypto/Solana skill; confirmed in prior holder-memo research) | Build in-tree — resolution uses CoinGecko's own free `/coins/{id}` endpoint |
| Time-series accumulation / volume capture | N/A — already an in-tree pattern (`volume_snapshots`) | Reuse the in-tree pattern |
| Token metadata enrichment | None found | Build in-tree |

**awesome-hermes-agent ecosystem check:** repo returns 404; no registry covers crypto contract↔id
resolution. **Verdict:** no Hermes skill applies; all three primitives are thin in-tree captures over
data we already fetch or a free CG endpoint.

## Drift-check (file:line evidence — all three gaps confirmed real, not redundant)

- **I1 — no resolver exists.** `scout/ingestion/coingecko.py` calls only `/coins/markets` +
  `/search/trending` (no `/coins/{id}` platforms hop). `resolver_cache` is `symbol→outcome`;
  `symbol_aliases` is `canonical↔venue_symbol` — **neither maps a DEX contract to a CG coin_id.**
  *(A `/coins/{id}` client may already exist from the held-position fallback #163 — reuse if so.)*
- **I2 — entry-mcap is destroyed at 7d.** `db.py:6175 prune_old_candidates(keep_days=7)` deletes the
  only DEX mcap source. `paper_trade_entry_snapshots` captures `mcap_usd_at_entry` but is keyed by
  `paper_trade_id` (paper-traded tokens only; ANSEM was never traded) — **not a corpus-wide store.**
  Reuse its field shape; widen the key to `contract_address`.
- **I3 — proxy not captured.** `txns_h1_buys`/`txns_h1_sells` are already on the model
  (`models.py:39–40`) but only used transiently; **no snapshot table** exists. `volume_snapshots`
  (`contract_address, volume_24h_usd, scanned_at`) is the exact pattern to copy.

---

## I1 — durable `contract↔coin_id` resolver

**Primitive:** `contract_coin_map(contract_address TEXT, chain TEXT, coin_id TEXT, resolved_at TEXT,
source TEXT, confidence TEXT, PRIMARY KEY(contract_address, chain))`.

**Mechanism:** at ingest (or a low-rate background pass), for DEX-sourced candidates lacking a mapping,
call CG `/coins/{id}` only when a coin_id is already known, OR resolve via the platforms map: CG
`/coins/{id}` returns `platforms{chain: contract}`. Populate the reverse map (contract→coin_id) as CG
coins are seen. **Free Demo-tier endpoint; respect the existing 30 req/min limiter** — this is a
low-rate enrichment, not per-cycle-per-token (cache hits dominate; PK upsert avoids re-resolving).

**Backfill/replay:** seed the map from existing rows where the link is already implicit — CG-native
`candidates` (contract_address *is* coin_id) and any name/symbol-exact matches already observed — and
mark those rows `source='backfill'`, `confidence` accordingly. Forward resolution fills the rest.

**Constraint:** resolution is best-effort and **never blocks ingest or the gate**; failures log and
retry. No scorer input.

## I2 — non-pruned entry-mcap retention

**Primitive:** `entry_mcap_snapshots(contract_address TEXT PRIMARY KEY, chain TEXT, first_seen_at TEXT,
mcap_usd_at_entry REAL, liquidity_usd_at_entry REAL, token_age_days_at_entry REAL, captured_at TEXT)`.

**Mechanism:** on first sighting of a contract (the same path that writes `candidates`), write-once the
entry mcap/liquidity/age. PK = `contract_address` with write-once semantics (do not overwrite on
re-sighting — preserve the *earliest* mcap, mirroring the `first_seen_at` MIN-merge logic at
`db.py:5860`). **Excluded from `prune_old_candidates`** — this table is the durable record that pruning
currently destroys.

**Backfill/replay:** seed from the current 7-day `candidates` window (the only entry mcap that still
exists). **Pre-pruning entry mcap is unrecoverable** — document this as a known one-time gap; coverage
starts accruing from deploy forward.

## I3 — free `txns_h1_buys` accumulation proxy capture

**Primitive:** `txns_h1_buys_snapshots(contract_address TEXT, txns_h1_buys INTEGER,
txns_h1_sells INTEGER, scanned_at TEXT)` + a derived (not-yet-scored) `txns_growth_1h` field on the
in-memory model.

**Mechanism:** per cycle, snapshot `txns_h1_buys`/`txns_h1_sells` (already on the model) exactly like
`volume_snapshots`. Compute `txns_growth_1h = current − prior_snapshot` for observability only.
**Zero new API calls** (data already fetched). Optionally parse GeckoTerminal `transactions.h1.buys`
(`models.py:171`, ~2 lines) for cross-source corroboration.

**Explicitly NOT scored:** `txns_growth_1h` is captured and logged but **does not contribute to
`quant_score`** in this PR. Wiring it into the scorer is a separate, later, recalibration-gated change.

---

## §12a compliance — every new table ships with a freshness watchdog

| Table | Expected write rate | Staleness alarm |
|---|---|---|
| `contract_coin_map` | bursty (new DEX contracts) | no new rows in 24h while DEX candidates flowing |
| `entry_mcap_snapshots` | ≈ new-contract rate (tens/hr) | no writes in 1h while pipeline cycling |
| `txns_h1_buys_snapshots` | every cycle for active contracts | no writes in 2× cycle interval |

Watchdogs go in the existing hourly maintenance loop. Alerts use `parse_mode=None` and emit
`*_alert_dispatched`/`*_alert_delivered` logs (global §12b).

## The measurability-coverage metric (operator acceptance criterion)

**`dex_measurability_coverage`** — emitted each cycle / on a daily rollup:

```
covered   = # DEX contracts in score_history that now have BOTH a contract_coin_map row
            AND an entry_mcap_snapshots row AND ≥1 coin_id-keyed outcome-surface match
total_dex = # DEX contracts in score_history over the trailing window
coverage  = covered / total_dex
```

Target: coverage climbs from ~0 toward a usable fraction over 2–4 weeks. **This single number is the
gate** for re-running the F1 cohort on the DEX corpus — when enough DEX contracts are outcome-joinable,
the real numerator/denominator becomes measurable and gate recalibration can be evaluated.

---

## Acceptance criteria (operator's gate for this spec → its implementation PR)

| Criterion | How this spec satisfies it |
|---|---|
| contract↔coin_id linkage persists at ingest | I1 `contract_coin_map`, upsert at ingest |
| entry mcap retained beyond pruning window | I2 `entry_mcap_snapshots`, excluded from prune |
| `txns_h1_buys` (or equiv) captured historically | I3 snapshot table, per-cycle |
| all new behavior read-only / observe-only | no scorer/gate/threshold/alert change; proxy captured-not-scored |
| no increase in outbound alerts | only new alerts are §12a staleness watchdogs (system-health, not signal) |
| clear replay/backfill story | I1 seed from CG-native + observed matches; I2 seed from 7d candidates (pre-prune gap documented); I3 forward-only |
| explicit metric: how many DEX contracts become outcome-measurable | `dex_measurability_coverage` (defined above) |

## Out of scope (explicit — do NOT do in the implementation PR)

- Gate recalibration / `MIN_SCORE` change.
- Wiring `txns_growth_1h` (or `holder_growth`) into `quant_score`.
- Any alert/threshold/lane behavior change.
- Paid holder feed.

## Sequencing

1. Implement I1+I2+I3 + watchdogs + coverage metric (one observe-only PR).
2. Soak 2–4 weeks; watch `dex_measurability_coverage` climb.
3. Re-run the F1 cohort on the now-measurable DEX corpus → real numerator/denominator.
4. **Only then** evaluate Track 3 (gate ~40, ≈13/day within soft ceiling) + proxy/retune
   counterfactuals against actual DEX outcomes. Any setting admitting >20/day → watchlist/soak only.
