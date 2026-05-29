**New primitives introduced:** NONE

# Liquidity Backfill Decision Packet (2026-05-29)

**Backlog item:** `BL-NEW-TODAYS-FOCUS-LIQUIDITY-VENUE-FACTS` (PR-B in the Today's Focus product roadmap)

**Audit scope:** read-only decision packet. No implementation, no code change, no plan handoff. Operator-decides between three options; the chosen option's plan is a separate PR.

## Triage Context

The 2026-05-28 liquidity coverage audit (`tasks/findings_liquidity_coverage_audit_2026_05_28.md`) found `paper_corpus.coverage_rate = 0.0` for the live `/api/todays_focus` cohort. The audit's headline:

> The headline blocker is NOT a sub-80% coverage threshold; it is a **structural 0% coverage** caused by the `candidates.liquidity_usd` column being empty/zero in the pipeline path that populates the current paper cohort.

The findings doc named three forward-paths but explicitly deferred the decision to PR-B's plan:

> (a) file a separate backfill PR to populate `candidates.liquidity_usd` from a data source before PR-B's UI work;
> (b) wire an alternative liquidity source (DexScreener / CoinGecko `/coins/markets`) keyed appropriately;
> (c) defer the liquidity-on-row column entirely until coverage is non-trivial.

This packet supplies the inputs operator needs to choose, plus follow-up findings from broader-cohort prod data and code-path analysis.

## Pinned Production State (2026-05-29)

### Aggregate `candidates` table coverage

| metric | value |
|---|---:|
| total candidates rows | 1,711 |
| rows with `liquidity_usd > 0` | 605 |
| coverage rate | **35.4%** |

### Coverage by chain

| chain | rows | with liquidity | coverage |
|---|---:|---:|---:|
| `coingecko` (placeholder) | 995 | 0 | **0.0%** |
| `solana` | 606 | 496 | 81.8% |
| `base` | 69 | 69 | 100.0% |
| `ethereum` | 30 | 30 | 100.0% |
| `bsc` | 9 | 8 | 88.9% |
| `ton` | 1 | 1 | 100.0% |
| `hyperevm` | 1 | 1 | 100.0% |

### Recent paper-trade cohort (last 36h, all signal_types)

14 paper rows opened. **All 14 have `chain="coingecko"` or NULL.** **0/14 have `liquidity_usd > 0`.** Signal mix: `chain_completed`, `narrative_prediction`, `tg_social`. Today's Focus paper-corpus is structurally CG-sourced — DexScreener/GeckoTerminal-sourced rows do not currently produce paper signals.

## Root Cause (Code Path)

Per `scout/ingestion/coingecko.py` + `scout/models.py:92-109`:

- `from_coingecko()` constructs `CandidateToken` with `chain="coingecko"` (literal placeholder, not a real chain) and `liquidity_usd=0.0` (hardcoded).
- The CoinGecko `/coins/markets` Demo endpoint does NOT surface a liquidity field. CoinGecko Demo is a dead-end for liquidity data structurally.
- `scout/ingestion/dexscreener.py` + `models.py:154` correctly extracts `pair.liquidity.usd` (DexScreener has the field).
- `scout/ingestion/geckoterminal.py` + `models.py:204` correctly extracts `reserve_in_usd` (GT has the field).
- The aggregator (`scout/aggregator.py:11-24`) deduplicates by `contract_address` with last-write-wins semantics; `liquidity_usd` is NOT in `_PRESERVE_FIELDS`. If CoinGecko's $0 arrives after DexScreener's $50K for the same contract, the final row carries $0.

**Implication for option (a) — backfill `candidates.liquidity_usd`:** the WRITER side of the field works correctly for DexScreener/GT-sourced rows. The 0% coverage on `chain="coingecko"` is not a writer bug; it's a source-ordering and source-content effect. A "backfill" cannot synthesize liquidity data from nothing — it must read from DexScreener or GeckoTerminal as the source-of-truth. So option (a) collapses structurally into "option (b) with persistence and async cadence."

## Option Matrix

The three options the audit named, refined against today's evidence:

### Option (b1) — Per-row DexScreener lookup at render time

Add a DexScreener `/tokens/v1/{chain}/{contract}` call per Today's Focus paper row at render time. Resolve actual chain via DexScreener's search-by-contract endpoint (`/dex/tokens/{contract}`).

| dimension | value |
|---|---|
| data source | DexScreener `/tokens/v1/...` (already wired in `scout/ingestion/dexscreener.py`) |
| API cost | ~5-6 req per dashboard render (per the audit's 5-row cohort); DexScreener public quota is 300 req/min; utilization < 0.5% |
| latency added | ~250-1000ms wall-clock (5 rows × 50-200ms DexScreener latency); blocks `/api/todays_focus` response |
| coverage achievable | High — DexScreener has 81-100% coverage on DEX-tradeable chains |
| chain coverage | Resolves the `chain="coingecko"` placeholder by reverse-lookup; identifies actual chain in same call |
| persistence | None — re-fetched every render. No `candidates.liquidity_usd` update. |
| failure mode | Render-time blocking call; DexScreener 429 → slow render. Cache layer required to avoid pathological renders. |

### Option (b2) — Background backfill cron via DexScreener search

Periodic cron (e.g., every 15 min) that walks `candidates` rows with `chain="coingecko"` AND `liquidity_usd=0`, calls DexScreener search-by-contract for each, and UPDATEs `candidates.liquidity_usd` + `candidates.chain` if a match is found.

| dimension | value |
|---|---|
| data source | DexScreener `/dex/tokens/{contract}` search endpoint |
| API cost | One pass over un-covered rows per cron tick; 995 current `chain="coingecko"` rows = 995 calls one-time, then maintenance-only on new ingestion |
| latency added | None — cron runs out-of-band; dashboard reads pre-populated DB |
| coverage achievable | High on DEX-tradeable tokens (which is most of the paper universe by mcap); lower on truly CG-only tokens (no DEX listing) |
| chain coverage | Resolves placeholder via reverse-lookup; persists actual chain |
| persistence | Yes — writes to `candidates.liquidity_usd` and (optionally) `candidates.chain` |
| failure mode | Stale data (15-min lag); DexScreener 429 spread across the cron pass; data races on simultaneous ingestion writes (use UPDATE WHERE liquidity_usd=0 to avoid clobbering fresh DexScreener-sourced values) |
| new primitive | New cron script; `scripts/check_dexscreener_lag.py` watchdog per §12a |

### Option (b3) — Hybrid: cron-backfill + render-time cache

Cron runs every N minutes to keep `candidates.liquidity_usd` warm; dashboard reads from DB synchronously without per-render lookups; falls back to per-row DexScreener lookup on cache-miss (configurable).

| dimension | value |
|---|---|
| Best of both | Yes, but adds complexity (two code paths for the same data) |
| Recommend | Defer until (b1) or (b2) ships and operator measures cache-miss rate |

### Option (c) — Defer the liquidity-on-row column until Today's Focus V0 usage read

Skip the liquidity field on the Today's Focus surface until the V0 usage read (~2026-06-10) tells us whether the surface is being used at all. If operator usage is low, sunk-cost on liquidity wiring is avoided. If usage is high, scope a separate liquidity-source PR (likely b1 or b2) after the usage signal.

| dimension | value |
|---|---|
| cost | $0 engineering; operator sees "Liquidity: unavailable" or no field at all for ~12 days |
| risk | Trader friction persists for ~12 days; potential opportunity cost if V0 usage signal would have been HIGHER with liquidity visible |
| reversal | Trivial — option (b1)/(b2)/(b3) can be scoped immediately after the usage read |
| gating | The usage read ALREADY informs whether to keep investing in the surface; liquidity is the highest-value field on the surface, so usage signal interpretation will depend on whether liquidity is visible at evaluation time. **This is a confound:** evaluating usage WITHOUT liquidity may understate true demand for the surface. |

## What Was NOT Picked (for scope discipline)

- **Switching the paper signal pipeline to DexScreener / GeckoTerminal source.** This is a multi-PR program (re-source `chain_completed`, `narrative_prediction`, `tg_social`, `gainers_early`, `losers_contrarian`, `volume_spike` from CG to DEX-keyed sources). Out of scope for liquidity backfill; tracked as a future-program candidate, NOT proposed here.
- **CoinGecko paid tier with on-chain endpoints.** Cost gate; not evaluated in this packet.
- **GeckoTerminal as primary source for backfill.** GT has per-chain trending pools but no general per-token search endpoint at free tier; less flexible than DexScreener for retroactive backfill.
- **Aggregator field-protection change.** Adding `liquidity_usd` to `_PRESERVE_FIELDS` so DexScreener's $50K survives subsequent CG $0 writes would help forward but does NOTHING for the 995 already-zero rows. Surfaced for operator awareness; not a standalone solution.

## Recommendation

**Two viable choices today:**

1. **Option (b2) — background cron backfill.** Most robust for the trader-facing surface. Zero render latency, persists data, sets up the foundation for option (b3) later if needed. Cost is one new script + one watchdog (per §12a). Best alignment with "structurally fix the 0% coverage problem."
2. **Option (c) — defer until 2026-06-10 V0 usage read.** Most conservative; preserves engineering capacity. Acknowledges the confound (usage signal interpretation depends on what trader sees) and explicitly bakes that into the V0 evaluation.

**Not recommended today:**
- Option (b1) per-row render-time lookup as the FIRST step — blocks `/api/todays_focus` response on external API; high-risk for a dashboard that already loads under operator-watched latency.
- Option (b3) hybrid before either (b1) or (b2) is validated; adds complexity without evidence.

## Anti-Scope (this PR)

- No code change. No new script. No cron wiring. No schema migration.
- No backlog status change on `BL-NEW-TODAYS-FOCUS-LIQUIDITY-VENUE-FACTS` (still PROPOSED — operator decides which option becomes the next PR).
- No reordering of the PR-B / PR-D / V0-usage-read sequencing.
- No threshold tuning, no policy change, no implementation authorization.
- No mutations to `candidates`, `paper_trades`, or any other runtime table.

## Operator Decision Surface

The operator needs to pick exactly one of:
- **(b1)** per-row render-time lookup → scope a separate plan PR
- **(b2)** background cron backfill → scope a separate plan PR
- **(b3)** hybrid → scope a plan PR once b1 or b2 has prod data
- **(c)** defer until 2026-06-10 V0 usage read → no PR; revisit then

This packet stops at the decision surface. No build or plan handoff happens here.
