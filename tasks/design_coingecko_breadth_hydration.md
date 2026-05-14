**New primitives introduced:** one configurable breadth knob (`COINGECKO_VOLUME_SCAN_PAGES`) and one local raw-row combiner helper for existing CoinGecko market rows.

# CoinGecko Breadth + Trending Hydration Design - 2026-05-14

## Goal

Reduce signal misses where CoinGecko-listed top gainers never reach
`gainers_snapshots`, `volume_history_cg`, `momentum_7d`, `slow_burn_candidates`,
or `velocity_alerts` because they are outside the current top-500 volume scan
or appear only in `/search/trending` without market data.

Scope is signal discovery only. Paper-trade policy and Telegram alerts are out
of scope for this change.

## Drift Check

| Area | Existing in tree? | Decision |
|---|---|---|
| CoinGecko market ingestion | Yes: `scout/ingestion/coingecko.py` has `fetch_top_movers`, `fetch_trending`, and `fetch_by_volume`. | Extend existing ingestion; do not add a new provider module. |
| Raw market fan-in | Yes: `scout/main.py` combines `last_raw_markets` and `last_raw_by_volume` for gainers/spikes/momentum/slow-burn/velocity. | Include hydrated trending rows in the same fan-in. |
| Trending snapshots | Yes: `scout/trending/tracker.py` stores `/search/trending` candidate snapshots. | Leave snapshot semantics unchanged; this change hydrates the raw-market side. |
| CoinGecko rate limiting | Yes: `scout.ratelimit.coingecko_limiter` and `_get_with_backoff`. | Reuse existing limiter/backoff. |
| Top-gainer storage | Yes: `scout/gainers/tracker.py` expects `/coins/markets` shaped rows. | Feed it hydrated `/coins/markets` rows; no schema change. |

## Hermes-First Analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| CoinGecko top-gainer / market breadth ingestion | None installed on VPS. Public CoinGecko Agent SKILL exists at `https://docs.coingecko.com/docs/skills` and `https://github.com/coingecko/skills`, but it is API knowledge, not gecko-alpha runtime persistence. | Build inside existing `scout/ingestion/coingecko.py`; use CoinGecko SKILL/API docs as reference. |
| Trending-token market hydration | None installed on VPS. Installed Hermes `xurl`, `kol_watcher`, `narrative_classifier`, and dispatcher cover X/KOL narrative flow, not CoinGecko hydration. | Build inside existing CoinGecko ingestion. |
| Generic crypto market-data replacement | GoldRush/Covalent Hermes skills exist for wallet/holder/transfer/pricing/DEX intelligence, not this exact CoinGecko breadth fan-in. | Track for provider consolidation, but do not replace this signal path. |
| X/KOL narrative signals | Existing Hermes `kol_watcher` / `narrative_classifier` / `xurl` cover this. | Reuse existing Hermes path; not part of this CoinGecko gap. |

Awesome/evolving ecosystem check: the 2026-05-14 audit reviewed
`awesome-hermes-agent`, HermesHub, CoinGecko first-party skills, GoldRush skills,
and PRB agent-skills. No installed or credible upstream skill replaces this
runtime fan-in while preserving gecko-alpha's DB writes and signal detectors.

One-sentence verdict: custom code is justified only as an extension of the
existing CoinGecko ingestion path; upstream skills are reference material, not a
production replacement.

## Design

1. **Trending hydration**
   - `fetch_trending` keeps reading `/search/trending` for rank.
   - It then batches the top trending CoinGecko IDs into one `/coins/markets`
     call with `ids=<comma ids>` and `price_change_percentage=1h,24h,7d`.
   - Returned market rows populate `last_raw_trending`.
   - Returned candidates preserve `cg_trending_rank` while carrying hydrated
     market cap, volume, price, and price-change fields.
   - If hydration fails, return rank-only candidates and avoid writing
     `market_cap_rank` into the raw `market_cap` field.

2. **Breadth expansion**
   - Add `COINGECKO_VOLUME_SCAN_PAGES` with default `3`.
   - `fetch_by_volume` requests pages `1..N` using the existing limiter/backoff.
   - Pages degrade independently: failed pages are skipped, successful pages
     still produce candidates and raw rows.
   - Dedupe remains by CoinGecko ID.
   - Default 3 raises volume breadth from top-500 to top-750 while keeping the
     scheduled main-cycle CoinGecko call budget under the 25/min limiter at the
     current 60s scan interval.

3. **Raw market fan-in**
   - Add a small helper that combines market rows from top movers, hydrated
     trending, and volume scan with first-seen dedupe.
   - Use that helper in `run_cycle` before gainers/spikes/momentum/slow-burn/
     velocity surfaces.
   - Keep held-position refresh rows in `price_cache` only; they maintain open
     trade pricing and should not create fresh discovery signals.

## Tests

Add focused tests before code:

- Trending hydration populates `last_raw_trending` from `/coins/markets` and
  preserves `cg_trending_rank`.
- Hydration failure still returns rank-only trending candidates and does not use
  `market_cap_rank` as `market_cap`.
- Volume scan honors `COINGECKO_VOLUME_SCAN_PAGES`.
- Raw market combiner includes hydrated trending rows and dedupes by ID.

## Non-Goals

- No paper-trade threshold changes.
- No Telegram alert changes.
- No new DB tables.
- No replacement of gecko-alpha runtime with a remote MCP/skill call.
