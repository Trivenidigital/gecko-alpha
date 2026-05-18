# CoinGecko budget attribution — held_position starvation after #158 activation

Date: 2026-05-18
Backlog: BL-NEW-CG-LANE-ORDER-HELD-POSITION-FIRST (filed alongside this doc)
Status: findings + small code-fix proposal

## Executive summary

After PR #158 went live on 2026-05-18T16:09:51Z (held-position price-refresh
lane enabled via `HELD_POSITION_PRICE_REFRESH_ENABLED=True`), the first cycle
refreshed all 150 held positions cleanly (`simple_price_missing_ids=[]`,
`stale_open_count=0`). Every cycle since has shown `refreshed_count=0` and
`not_found_count=145-147`. /simple/price returns nothing.

Root cause is **lane ordering inside `_fetch_coingecko_lanes`**, not a
/simple/price coverage issue and not a held-position cadence issue. The lane
is structurally last in the CG-call sequence and is starved by the upstream
scanner lanes consuming the rolling budget. The first cycle worked only
because a fortuitous 40.9s pre-cycle global backoff allowed CG's IP-rate-limit
window to clear before the cycle bursted through.

Recommendation: **code fix — reorder `_fetch_coingecko_lanes` so
held_position runs FIRST.** Single-function reorder, ~10 line net change,
plus test update. Small, reviewable, reversible.

## Drift-check (§7a)

Existing primitives the proposal touches:
- `_fetch_coingecko_lanes` in `scout/main.py:628-667` — exists, current order
  is `top_movers → trending → by_volume → midcap_gainers → held_position`.
- `coingecko_limiter` and `RateLimiter.is_backing_off()` — exist, ship-form
  ratified in PR #131. No re-design needed.
- `fetch_held_position_prices` — exists (PR earlier, activated by PR #158).
- Backlog: no existing entry covers lane-ordering specifically; the
  `BL-NEW-CG-RATE-LIMITER-BURST-PROFILE` design doc (in tasks/) explicitly
  documented serializing CG lanes but did not specify their order. At the
  time of #131, held_position was disabled (flag off) so its position in the
  sequence was inert.

Verdict: existing primitive, modify in place. No new primitives introduced.

## Hermes-first (§7b)

Same domain as `BL-NEW-CG-RATE-LIMITER-BURST-PROFILE`. The Hermes-first
analysis in `tasks/design_bl_new_cg_rate_limiter_burst_profile.md` checked
the Hermes skill hub and awesome-hermes-agent ecosystem for async Python
rate-limiter / lane-orchestration primitives and found none applicable to
gecko-alpha's aiohttp CoinGecko scheduling. That verdict carries forward —
this fix extends the existing in-tree primitive, not a new domain.

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Async lane orchestration / per-lane budget reservation | No installed/public Hermes skill found | Reorder existing `_fetch_coingecko_lanes` |
| CoinGecko `/simple/price` priority routing | No Hermes skill found | Same — in-tree lane order is the lever |

awesome-hermes-agent ecosystem check: no CoinGecko lane-scheduling primitive.

One-sentence verdict: extend in-tree `_fetch_coingecko_lanes` ordering; no
Hermes path applies.

## Evidence

### Lane-completion timeline post-#158 flip

```
16:09:51Z  service restart (operator flip)
16:14:17Z  cg_fetch_attempted coins/markets — fortuitous 40.9s global backoff
16:15:07Z  cg_candidates_returned (top_movers small-cap done)
16:15:25Z  cg_candidates_fetched (trending done)
16:15:25Z  cg_fetch_attempted coins/markets:volume_desc (by_volume start)
16:15:58Z  cg_volume_scan_returned (by_volume done)
16:15:58Z  rate_limiter_waiting 8.8s — held_position acquire blocking
16:16:07Z  held_position_refresh_summary  refreshed=150  ✓ FIRST CYCLE OK

16:17:31Z  cg_fetch_attempted coins/markets — cycle 2 start
16:17:40Z  cg_candidates_returned
16:17:59Z  cg_candidates_fetched (trending done)
16:17:59Z  cg_fetch_attempted coins/markets:volume_desc
16:18:17Z  rate_limiter_waiting 14.5s
16:18:32Z  cg_volume_scan_returned
16:18:32Z  rate_limiter_waiting 8.2s
16:18:40Z  cg_429_backoff fires  ← /simple/price hit 429
16:18:40Z  held_position_refresh_summary  refreshed=0  not_found=146  ✗
```

Every cycle from 16:18:40Z through 18:04Z (and continuing) follows the
same shape: scanner lanes complete (sometimes), held_position fires
last and hits 429.

### Lane attribution (2h window)

| Lane | `cg_fetch_attempted` count | Sub-calls per invocation |
|---|---|---|
| `coins/markets` (top_movers) | 42 | 2 (market_cap_asc + volume_desc) |
| `coins/markets:volume_desc` (by_volume) | 27 | up to 3 (pages) |
| `coins/markets:market_cap_desc_midcap` | 4 | up to 3 (pages, every 3rd cycle) |
| `/simple/price` (held_position) | not logged at this layer | 1 |

Scanner lanes are the dominant consumers. /simple/price is the smallest
single-cycle consumer and is structurally last.

### 429 pressure under conservative config

VPS `.env` runs the post-#129/#130/#131 conservative profile:
- `COINGECKO_RATE_LIMIT_PER_MIN=6`
- `COINGECKO_MIN_REQUEST_INTERVAL_SEC=8.0`
- `COINGECKO_REQUEST_JITTER_SEC=2.0`
- `COINGECKO_429_COOLDOWN_SEC=120` (default)

Even at 6/min with 8-10s spacing, the 2h window contains 42
`cg_429_backoff` events. This matches the design doc's note that
chronic 429 pressure persists under conservative tuning (free-tier CG
IP-rate-limit is more aggressive than the local 25/min cap). The
budget cannot be safely raised; it can only be reallocated.

## Why first cycle worked and others don't

The 16:14:17Z entry shows the cycle 1 start with a pre-existing 40.9s
global backoff (from a 429 prior to the operator restart). That wait
let CG's IP-rate-limit window drain before the cycle bursted through.
By the time held_position's /simple/price fired at 16:16:07Z, the cycle
had already paid 40.9s of cooldown + 8.8s of spacing — CG was ready.

Cycle 2 had no such pre-cycle reset. Scanner lanes consumed the fresh
budget; held_position arrived at /simple/price after CG's window was
saturated → 429 → 120s cooldown → cycle 3 starts before cooldown clears
→ held_position waits for the cooldown to drain, fires /simple/price
into a still-saturated CG window → 429 again. The pattern is
self-reinforcing.

## Rejected alternatives

- **Defer to `/coins/{id}` fallback (PR #163 design).** The fallback gate
  requires `/simple/price` to miss specific tokens AND `/coins/{id}` to
  recover them. The current symptom is `/simple/price` returning nothing at
  all (all 147 not_found), which is a budget issue, not a coverage issue.
  The fallback also competes for the same budget; it would not help.
- **Bump `HELD_POSITION_PRICE_REFRESH_INTERVAL_CYCLES` from 1 to 3+.** Cuts
  /simple/price load by ~67%, but the underlying ordering still puts it last
  on the cycles when it does fire. The 429 starvation continues, just less
  often. Config-only stop-gap if the code fix is not approved; not the
  right primary fix.
- **Raise `COINGECKO_RATE_LIMIT_PER_MIN` above 6.** Triggers more 429s; the
  prior tuning history shows free-tier CG is the binding constraint, not the
  local limiter.
- **Reduce `COINGECKO_VOLUME_SCAN_PAGES` from 3 to 2.** Cuts scanner breadth.
  Operator instruction explicitly says NOT to do this blindly. Lane reorder
  achieves the same protection without breadth cost.

## Recommendation: code fix

Reorder `_fetch_coingecko_lanes` so `fetch_held_position_prices` runs FIRST,
before the scanner lanes. Keep the existing stop-on-backoff cascade.

Rationale:
- held_position is **1 call/cycle**. Scanners are 2-3 each (7-10 total).
  Held is ~10-14% of the cycle's CG budget.
- held_position is the most operationally critical surface — the live
  trailing-stop evaluator depends on fresh price_cache rows. A starved
  scanner lane gives up a cycle of scanning; a starved held_position lane
  silently breaks downstream exits.
- Scanner lanes are designed to tolerate cycle-skip (each surfaces signals
  that re-fire on subsequent cycles). Held_position freshness is per-cycle.
- Risk asymmetry favors the reorder: scanners losing 1 call (`/simple/price`
  going first) costs at most one cycle of scanner coverage; held_position
  losing every cycle is the current state.

### Implementation sketch

```python
async def _fetch_coingecko_lanes(session, settings, db):
    # held_position FIRST: 1 call, highest operational priority, smallest
    # budget footprint. Protects per-cycle price-cache freshness for the
    # live trailing-stop evaluator from being starved by the larger
    # scanner lanes' rolling-window consumption.
    held_position_raw = await _call(
        "held_position_prices", fetch_held_position_prices, session, settings, db
    )
    if coingecko_limiter.is_backing_off():
        logger.warning("coingecko_lanes_stopped_for_backoff",
                       after="held_position_prices")
        return [], [], [], [], held_position_raw

    cg_movers = await _call("top_movers", cg_fetch_top_movers, session, settings)
    # ... rest of scanner cascade unchanged ...
```

Plus an updated test in `tests/test_main.py` exercising the new ordering
invariant: a backoff after held_position should preserve `held_position_raw`
in the returned tuple and skip the scanner lanes.

### Backout

`git revert` the PR. Single function, single test file. Reversible.

## Validation plan (post-deploy)

Compare a 2h window pre-fix vs post-fix on srilu-vps:

- Required: `held_position_refresh_summary.refreshed_count > 0` for at least
  3 consecutive cycles outside a fresh 429 cooldown window.
- Required: `simple_price_missing_ids` shrinks toward `[]` for the steady-
  state cohort.
- Tolerated: scanner-side `cg_429_backoff` count may rise slightly (held
  now consumes its 1 call first); should not block scanners from completing
  at least 1 successful surface per cycle on average.

Hard guardrail (per operator): do NOT mark #158 24h validation complete
until journal evidence exists OUTSIDE sustained 429 windows.

## Side-finding worth filing

The current 6/min + 120s-cooldown profile produces chronic 429 pressure even
without held_position. The design doc tracks this. After the lane-reorder
fix lands and we see whether scanner 429 rate rises, we may need a separate
investigation into either (a) Demo API key (per #129's deploy notes) or
(b) further per-lane prioritization. Not in scope for this fix.

## What this doc is NOT

- Not a fallback-design retirement. The `/coins/{id}` fallback design from
  PR #163 stays on file pending evidence outside 429 windows.
- Not a held-position cadence change. INTERVAL_CYCLES stays at 1.
- Not a scanner-breadth reduction. VOLUME_SCAN_PAGES stays at 3.
