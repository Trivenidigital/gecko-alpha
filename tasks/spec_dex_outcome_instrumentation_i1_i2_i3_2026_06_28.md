**New primitives introduced:** `contract_coin_map` table + resolver (I1), `entry_mcap_snapshots`
table (I2), `txns_h1_buys_snapshots` raw-capture table (I3), two coverage metrics
(`dex_resolution_health` + `dex_measurable_cohort_size`), and per-table **freshness AND data-quality**
watchdogs (§12a + §12c). All observe-only: no scorer, gate, threshold, or trading-alert behavior
changes (system-health watchdog alerts route to an operator/health channel only).

# Spec — DEX-outcome instrumentation substrate (I1 / I2 / I3)

**Date:** 2026-06-28
**Status:** IMPLEMENTATION SPEC — observe-only substrate. **Not** a gate/alert/threshold change.
**Evidence base:** `findings_same_asset_under_gate_cohort_30d_2026_06_28.md` (F1)
**PR for the findings that motivate this:** #383 (docs)
**Goal:** make the DEX-stage cohort *measurable* so a future gate recalibration / lever choice can be
made on evidence instead of guesswork.

**Revision v2 (2026-06-28):** addresses spec-review CHANGES REQUESTED on #384 — B3 strong data-quality
watchdogs (§12a+§12c §below), B2 metric split (`dex_resolution_health` vs `dex_measurable_cohort_size`
+ DEX classifier), B1 survivorship-bias disclosure, B4 raw-first proxy semantics + non-optional GT
capture, and clarifications C1 (call-rate sizing), C2 (entry definition), C3 (health-channel routing).
B-/C-number → section mapping is in the re-review comment on #384.

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

**Call-rate budget (C1).** Resolution is *not* per-cycle-per-token. New `/coins/{id}` calls are made
only for (a) a CG-native coin first seen this cycle that has no `platforms` entry cached yet, and
(b) a bounded backlog drain of unresolved DEX contracts at **≤ N calls/cycle** (default `N=5`).
At 60 cycles/hr that is ≤300 calls/hr = ≤5/min, leaving ≥25/min of the shared 30 req/min budget for
ingestion; PK upsert + a negative-result TTL prevent re-resolving. `N` is a Settings value so the
budget can be tuned without a code change. If the limiter is contended, resolution yields first
(ingestion has priority).

**Measurability ceiling & retroactive linkage (B1).** A DEX mint has **no coin_id until CoinGecko
lists it**, and every outcome surface is coin_id-keyed. So linkage is necessarily **retroactive** —
established when/if CG lists the token — and the measurable universe is **only DEX tokens that
eventually CG-list.** Winners tend to list (ANSEM did); **never-listing fizzles are permanently
invisible.** Consequence: any precision computed on this substrate is **upward-biased** (the
denominator undercounts fizzles). This is acceptable for a first pass but MUST be corrected before a
recalibration decision — `dex_measurable_cohort_size` (below) reports the listed cohort only, and the
eventual cohort analysis must separately estimate/bound the never-listing fizzle population (e.g., from
the raw DexScreener/GT DEX universe count vs the CG-listed subset).

## I2 — non-pruned entry-mcap retention

**Primitive:** `entry_mcap_snapshots(contract_address TEXT PRIMARY KEY, chain TEXT, first_seen_at TEXT,
mcap_usd_at_entry REAL, liquidity_usd_at_entry REAL, token_age_days_at_entry REAL, captured_at TEXT)`.

**Mechanism:** on first sighting of a contract (the same path that writes `candidates`), write-once the
entry mcap/liquidity/age. PK = `contract_address` with write-once semantics (do not overwrite on
re-sighting — preserve the *earliest* mcap, mirroring the `first_seen_at` MIN-merge logic at
`db.py:5860`). **Excluded from `prune_old_candidates`** — this table is the durable record that pruning
currently destroys.

**Entry definition (C2).** "Entry" = the **earliest DEX-side sighting** of the contract. When a token
has both a DEX row and a CG-native row, **prefer the DEX-side mcap/liquidity**; CG-native rows
frequently carry `mcap=0` / `liquidity=0` placeholders (ANSEM's CG row had `mcap=0`), which must never
overwrite a real DEX-side value. Capture rule: write the row only when `mcap_usd_at_entry > 0` from a
DEX source; if the first sighting is a zero/placeholder, hold the slot open (`captured_at` null) until a
non-zero DEX mcap is observed, then write-once. This keeps "entry" the genuine earliest *tradeable*
mcap, not a placeholder.

**Backfill/replay:** seed from the current 7-day `candidates` window (the only entry mcap that still
exists). **Pre-pruning entry mcap is unrecoverable** — document this as a known one-time gap; coverage
starts accruing from deploy forward.

## I3 — free `txns_h1_buys` accumulation proxy capture

**Primitive:** `txns_h1_buys_snapshots(contract_address TEXT, txns_h1_buys INTEGER,
txns_h1_sells INTEGER, source TEXT, scanned_at TEXT)`. **Raw absolute values only — no derived growth
field is stored.**

**Semantics — store raw, derive later (B4).** `txns_h1_buys`/`txns_h1_sells` are DexScreener's
*rolling-1-hour* counts. We capture the **absolute value + source + timestamp every cycle** (like
`volume_snapshots`) and compute any delta **in analysis**, where snapshot spacing is explicit (a true
~1h-spaced delta, or a per-cycle delta) — rather than baking a noisy `current − prior` across 60s
cycles into a misnamed `txns_growth_1h` at capture time. **No `txns_growth_1h` field ships in this PR.**

**Source coverage (B4).** `txns_h1_buys` is populated by DexScreener. **GeckoTerminal-sourced tokens
must also be captured** (non-optional): parse GT `transactions.h1.buys`/`sellers` (`models.py:171`) and
record with `source='geckoterminal'`. For any token where neither source provides buy/sell counts,
**do not write a row** — the gap is then visible to the non-null-rate watchdog (below) instead of being
masked by a zero. `source` lets analysis account for DS-vs-GT definitional differences.

**Zero new API calls** (both fields already fetched). **Explicitly NOT scored:** nothing here
contributes to `quant_score` in this PR. Wiring an accumulation signal into the scorer is a separate,
later, recalibration-gated change.

---

## Watchdogs — freshness (§12a) AND data-quality (§12c) — B3

Freshness alone is insufficient (B3): a table can be **fresh but semantically empty** — backfill rows
keep flowing while live resolution → 0, or every-cycle rows write while values are NULL/zero. Both
tiers ship together; the second is the one that catches our signature silent-failure class
(*heartbeat ≠ health*).

### Tier 1 — freshness (§12a): "did writing stop?"
| Table | Expected write rate | Staleness alarm |
|---|---|---|
| `contract_coin_map` | bursty (new DEX contracts) | no new rows in 24h while DEX candidates flowing |
| `entry_mcap_snapshots` | ≈ new-contract rate (tens/hr) | no writes in 1h while pipeline cycling |
| `txns_h1_buys_snapshots` | every cycle for active contracts | no writes in 2× cycle interval |

### Tier 2 — data-quality (§12c): "is what we wrote actually usable?"
| Check | Definition | Alarm condition |
|---|---|---|
| **Live resolution-success rate** | of DEX contracts where a `/coins/{id}` resolution was *attempted* this window, fraction that produced a coin_id (exclude `source='backfill'`) | < floor (default 5%) over 24h while attempts > 0 |
| **Non-zero entry-mcap rate** | fraction of `entry_mcap_snapshots` rows written this window with `mcap_usd_at_entry > 0` | < 90% over 24h |
| **Non-null `txns_h1_buys` rate** | fraction of `txns_h1_buys_snapshots` rows this window with `txns_h1_buys` not null | < floor (default 50%) over 6h |
| **Coverage-trend degradation** | `dex_resolution_health` (below), 7d trailing slope | metric **falls** 3 consecutive daily rollups (regression, not merely low) |
| **Fresh-but-empty (the silent-failure signature)** | Tier-1 OK (rows writing) AND that table's Tier-2 quality-rate ≈ 0 | escalate immediately — this is exactly the failure freshness alone misses |

Watchdogs run in the existing hourly maintenance loop. **Routing (C3):** every alert here goes to the
**operator/system-health channel only** — never the trading/signal channel — so "no increase in
outbound *trading* alerts" holds. Alerts use `parse_mode=None` and emit
`*_alert_dispatched`/`*_alert_delivered` logs around every send (global §12b) so successful deliveries
are traceable, not just failures.

## Coverage metrics — substrate-health vs analysis-readiness (B2)

A single ratio over "all DEX contracts" conflates *is the instrument working* with *how many tokens are
even CG-listable*. Split into two metrics, plus a precise classifier.

**DEX-vs-CG classifier.** `score_history` holds only `contract_address`. Classify by address form,
persisted (cheap column / lookup) so it does not depend on the pruned `candidates.chain`:
CG-native slug → `^[a-z0-9]+(-[a-z0-9]+)+$` (lowercase, hyphenated, no `0x`/mint shape);
EVM DEX → `0x` + 40 hex; Solana DEX → base58, 32–44 chars.

**(1) `dex_resolution_health` — substrate health.** Among DEX contracts that **are CG-listed** (coin_id
discoverable), the fraction fully wired:
```
listed_dex = DEX contracts (classifier) whose coin_id is resolvable via contract_coin_map
covered    = listed_dex rows that ALSO have an entry_mcap_snapshots row AND >=1 coin_id outcome match
health     = covered / listed_dex
```
Answers "is I1+I2+I3 working for the tokens it *can* work for?" — should climb toward ~1.0; it is what
the Tier-2 coverage-trend watchdog tracks, and is **not** dragged down by never-listing tokens.

**(2) `dex_measurable_cohort_size` — analysis readiness.** Absolute count of DEX contracts that are
fully outcome-joinable (coin_id + entry mcap + >=1 outcome surface). This is the **n** for re-running
the F1 cohort; the proceed-gate is an absolute-n threshold (e.g. n >= 30 with >=1 ran->=10x), **not** a
ratio.

**Survivorship caveat on both (B1).** Both are computed over *CG-listed* DEX tokens only; never-listing
fizzles are absent, so any later precision is upward-biased. The cohort analysis MUST separately bound
the never-listing population (raw DS/GT DEX universe count vs the CG-listed subset) and report that
bound alongside the cohort — not as an afterthought — before any recalibration verdict.

---

## Acceptance criteria (operator's gate for this spec → its implementation PR)

| Criterion | How this spec satisfies it |
|---|---|
| contract↔coin_id linkage persists at ingest | I1 `contract_coin_map`, upsert at ingest |
| entry mcap retained beyond pruning window | I2 `entry_mcap_snapshots`, excluded from prune |
| `txns_h1_buys` (or equiv) captured historically | I3 snapshot table, per-cycle |
| all new behavior read-only / observe-only | no scorer/gate/threshold/alert change; proxy captured-not-scored |
| no increase in outbound alerts | no new *trading* alerts; freshness + data-quality watchdogs route to the operator/health channel only (C3) |
| clear replay/backfill story | I1 seed from CG-native + observed matches; I2 seed from 7d candidates (pre-prune gap documented); I3 forward-only |
| explicit metric: how many DEX contracts become outcome-measurable | split: `dex_resolution_health` (substrate) + `dex_measurable_cohort_size` (readiness), CG-listed only with documented survivorship bound (B1/B2) |

## Out of scope (explicit — do NOT do in the implementation PR)

- Gate recalibration / `MIN_SCORE` change.
- Wiring `txns_growth_1h` (or `holder_growth`) into `quant_score`.
- Any alert/threshold/lane behavior change.
- Paid holder feed.

## Sequencing

1. Implement I1+I2+I3 + freshness & data-quality watchdogs + both coverage metrics (one observe-only PR).
2. Soak 2–4 weeks; watch `dex_resolution_health` climb toward ~1.0 and `dex_measurable_cohort_size`
   reach the proceed-gate (n ≥ 30 with ≥1 ran-≥10×).
3. Re-run the F1 cohort on the now-measurable DEX corpus → real numerator/denominator, **with the
   never-listing survivorship bound reported alongside (B1).**
4. **Only then** evaluate Track 3 (gate ~40, ≈13/day within soft ceiling) + proxy/retune
   counterfactuals against actual DEX outcomes. Any setting admitting >20/day → watchlist/soak only.
