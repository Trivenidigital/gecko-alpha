# Design: Prospective sub-$30M high-conviction watchlist (V1, observe-only)

**New primitives introduced:** `conviction_watchlist_snapshots` table (new); prospective
conviction builder `scout/conviction/prospective.py` (new); `/api/conviction/prospective`
endpoint (new); "Prospective Watchlist" dashboard tab (new). REUSES `cross_surface_conviction`
scoring + the gainers tracker's per-surface→source lookback logic (no new detector, no new
signal).

Implements the pre-designed-but-unbuilt **`BL-NEW-CONVICTION-PROSPECTIVE-SCORE`** +
**`BL-NEW-CONVICTION-FORWARD-MEASUREMENT`** (see `tasks/design_cross_surface_conviction_2026_06_12.md`
§Deferred). V1 is **observe-only**: no Telegram alerts, no trades, no operator-action pressure.

## Hermes-first analysis (CLAUDE.md §7b)

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Crypto cross-surface conviction scoring | none (Hermes substrate is doc-ingest/agent dispatch) | build in-tree (reuses existing `cross_surface_conviction`) |
| Per-coin surface-detection aggregation over SQLite | none | build in-tree (reuses gainers builder lookups) |
| Read-only dashboard surface | none (in-tree FastAPI + React dashboard) | build in-tree |

awesome-hermes-agent ecosystem check: no crypto-watchlist / conviction skill. Verdict
`extends-Hermes`. Receipt: `tasks/.hermes-check-receipts/bl-new-conviction-prospective-watchlist.json`.

## Drift-check (CLAUDE.md §7a)

Prospective conviction is DESIGNED but NOT BUILT (named `BL-NEW-CONVICTION-PROSPECTIVE-SCORE`,
design_cross_surface_conviction_2026_06_12.md:54). The shipped conviction surface
(`/api/conviction/shortlist`, #364/#365/#366) is **retrospective** (rows already crossed
+20%). No `conviction_watchlist*` table, no prospective builder, no prospective endpoint
exists. `cross_surface_conviction()` (the pure scorer) and the gainers tracker's per-surface
lookback (tracker.py:198-286) are directly reusable. Net-new confirmed.

## Goal

A forward-looking watchlist: surface coins that — RIGHT NOW, before any +20%/24h appearance —
already have **sustained multi-surface early confirmation** and are **small-cap**, so the
operator can watch them before they move. Persist every snapshot so prospective precision
(do watchlisted coins subsequently pump?) becomes measurable — the honest gate for any
future high-tier alert (Phase 3).

## The conviction rule (settled)

A coin is on the watchlist when ALL hold:
1. **Not yet pumped** — no `gainers_comparisons` row (`appeared_on_gainers_at IS NULL` /
   absent). Coins that already appeared are the retrospective set, excluded.
2. **Sustained early confirmation** — `early_count` = # of the 8 independent surfaces whose
   FIRST detection of the coin is **≥ `CONVICTION_EARLY_LEAD_MINUTES` (1440 = 24h) ago**.
   `tier=high` when `early_count ≥ 4`, `watch` when `≥ 2` (reuses `_tier` gates).
3. **Small-cap** — current `market_cap < CONVICTION_WATCHLIST_MAX_MCAP` ($30M).

**Emerging metadata (display only, NOT high conviction):** `fresh_count` = # surfaces whose
first detection is **< 24h ago**. Shown as an "emerging" column so the operator can watch
conviction accrue toward sustained — but it never counts toward `tier`.

Time semantics: retrospective measures `lead = first_gainer_at − first_detection`; prospective
measures `age = now − first_detection`. Same surface→source lookups, different t0.

## Architecture (snapshot builder + read-only tab)

```
pipeline cycle (hourly gate)
  └─ build_prospective_watchlist(db, settings)        [scout/conviction/prospective.py]
       1. universe   = distinct coins first-detected by ANY surface within
                       CONVICTION_PROSPECTIVE_LOOKBACK_DAYS (union over the 8 sources)
       2. exclude    = coins present in gainers_comparisons (already pumped)
       3. per coin   = first-detection age per surface (reuse the tracker lookups);
                       early_count (age≥24h), fresh_count (age<24h), tier, contributing
       4. enrich     = current market_cap + mcap age from price_cache
       5. keep       = tier≥watch (the full denominator for precision; the TAB filters
                       to high + <30M, but we SNAPSHOT the whole ≥watch set)
       6. write      = one snapshot batch (snapshot_at = run ts) to
                       conviction_watchlist_snapshots
       7. observe    = structured log: rows_written, per_surface_contrib counts,
                       high_tier_count, sub30m_high_count  (surface-health visibility)

dashboard
  └─ GET /api/conviction/prospective?min_tier=high&max_mcap=30000000
       reads the LATEST snapshot batch, filters, returns rows + meta
  └─ "Prospective Watchlist" tab renders it (read-only, N-gated, observe-only banner)
```

### `conviction_watchlist_snapshots` (new table)
`id, snapshot_at, coin_id, symbol, name, early_count, fresh_count, tier,
contributing_surfaces (JSON), market_cap (nullable), mcap_age_minutes (nullable),
first_detection_ages (JSON {surface: minutes}), created_at`. Indexed on
`(snapshot_at)` and `(snapshot_at, tier)`. The latest `snapshot_at` batch is the live
watchlist; the full history is the prospective-precision event stream (later joined
against subsequent `gainers_comparisons` to compute "of N snapshotted high-tier coins,
how many appeared on gainers within Xd"). Pruned via the hourly loop
(`CONVICTION_WATCHLIST_SNAPSHOT_RETENTION_DAYS`, default 90 — keep long enough to
measure forward outcomes).

### Builder `scout/conviction/prospective.py`
- `build_prospective_watchlist(db, settings, *, now=None) -> dict` (returns the run
  summary for logging/tests). Pure-ish: takes db, does the union + per-surface age
  lookups (extracted/shared with the tracker where practical), scores via a small
  prospective adapter over `cross_surface_conviction`'s tier gates, writes the snapshot.
- Surface→source map (age = now − MIN(detection_time)): narrative→`predictions.predicted_at`,
  pipeline→`candidates.first_seen_at`, chains→`signal_events.created_at`,
  spikes→`volume_spikes.detected_at`, acceleration/momentum/slow_burn/velocity→their
  CG-slug tables' `detected_at` (the existing `_COIN_ID_SURFACES` set).
- mcap from `price_cache.market_cap` keyed by coin_id; `mcap_age_minutes` = now −
  `price_cache.updated_at` (staleness honesty — a stale mcap is flagged, not trusted blindly).
- Bounded: `CONVICTION_PROSPECTIVE_LOOKBACK_DAYS` (default 14) caps the universe; a
  per-run row cap with a `truncated` flag so a capped pool is never a silent recall hole.

### Endpoint `/api/conviction/prospective` (dashboard, read-only, additive)
Params `min_tier` (low|watch|high, default high), `max_mcap` (default 30_000_000),
`limit`. Reads latest snapshot batch only. `meta`: `read_only`, `not_trade_advice`,
`observe_only`, `prospective`, `calibration:"prospective_unvalidated"`, `snapshot_at`,
`snapshot_age_minutes`, tier gates, total in batch, returned count. Empty/missing snapshot
→ honest empty with `snapshot_at:null`.

### Dashboard tab "Prospective Watchlist"
New top-level tab (sibling of the existing Conviction tab). Compact table: symbol/name,
mcap, `early_count` (sustained ≥24h surfaces), contributing surfaces, `emerging`
(fresh_count), oldest-surface age, `snapshot_at` freshness, chart link (reuse `TokenLink`).
Banner: "Observe-only · prospective precision UNVALIDATED · not trade advice." N-gate:
show `INSUFFICIENT_DATA` until enough snapshots/outcomes; **never render a bare backtest
rate**. Retrospective sub-$30M comparison stays a small secondary panel/link, NOT the main
view (calibration reference only).

## Config (`scout/config.py`)
- `CONVICTION_PROSPECTIVE_ENABLED: bool = True` — observe-only builder; safe-on (writes a
  snapshot table, no alerts/trades).
- `CONVICTION_WATCHLIST_MAX_MCAP: float = Field(30_000_000, ge=0)`.
- `CONVICTION_PROSPECTIVE_LOOKBACK_DAYS: int = Field(14, ge=1, le=120)`.
- `CONVICTION_WATCHLIST_SNAPSHOT_RETENTION_DAYS: int = Field(90, ge=1)`.
- Reuses `CONVICTION_EARLY_LEAD_MINUTES` (1440), `CONVICTION_HIGH_TIER_MIN_SURFACES` (4),
  `CONVICTION_WATCH_TIER_MIN_SURFACES` (2), `CONVICTION_SCORE_ENABLED`.

## Failure-mode / observability (§12a, per BL-NEW-CONVICTION-FORWARD-MEASUREMENT)
- Snapshot-freshness: the builder logs `conviction_prospective_snapshot_written`
  (rows, high_tier, sub30m_high, per-surface contrib). A stale/empty snapshot is the
  silent-failure surface (an upstream detector dying collapses the watchlist). V1 logs
  the per-surface contribution counts every run so a collapse is visible in journalctl;
  a freshness watchdog/alert is a fast-follow (V1.1) — explicitly noted, not silently
  dropped.
- Builder wrapped so a failure can't crash the cycle; never raises into the pipeline loop.

## Anti-scope (V1)
No Telegram alerts, no trades, no paper-trade dispatch, no operator-action labels/urgency,
no new detector, no change to any signal threshold. Read-only dashboard + a snapshot table.
The high-tier ALERT (Phase 3) stays gated on the prospective precision THIS surface will
measure — not shipped here.

## Evaluation gate (what this unlocks)
Once snapshots + forward outcomes accumulate (n≥20 prospective high-tier coins), a
follow-up precision report joins snapshot coins → subsequent `gainers_comparisons`
appearances → Wilson-LB vs the firehose base rate. Only if that clears does Phase 3
(high-tier alert) become eligible. V1 ships the substrate that makes this measurable.

## Testing (TDD)
- Scorer adapter: 0/1/2/4/8 sustained surfaces → count/tier; age exactly at 1440 (inclusive);
  fresh (<24h) excluded from tier but counted in fresh_count; null/missing detection degrades.
- Builder (tmp_path db): seed per-surface source rows at controlled ages → assert universe,
  gainers-exclusion, early/fresh counts, mcap enrich + staleness, tier≥watch snapshot set,
  truncation flag, run summary.
- DB: snapshot insert/read, latest-batch query, prune by retention; migration idempotency.
- Endpoint: shape/contract, min_tier + max_mcap filters, latest-batch only, empty/missing
  snapshot, meta honesty (observe_only, prospective, snapshot_age).
- Frontend: tab renders, N-gate banner, observe-only copy firewall (no "act now"/advice),
  emerging-vs-high distinction, dist-commit discipline.

## Review gates (before merge)
Codex: (1) structural (builder reaches the right sources; gainers-exclusion correct;
age vs lead correctness), (2) failure-mode (snapshot-freshness visibility; builder
isolation; mcap-staleness honesty), (3) UI/data-contract (endpoint↔frontend shape;
observe-only copy firewall; N-gate).
