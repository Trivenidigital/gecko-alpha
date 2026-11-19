**New primitives introduced:** `CoinGeckoBudget` (monthly-credit ledger + per-bucket enforcement + provider heartbeat), `governed_cg_call` (borrowed accounting/enforcement for callers owning a retry loop), `cg_credit_ledger` table, `COINGECKO_DISCOVERY_INTERVAL_CYCLES` / `COINGECKO_DISCOVERY_ENABLED` / `COINGECKO_MONTHLY_*` / `COINGECKO_BUDGET_*` settings, `cg_pricing_live` provider-liveness predicate.

# CoinGecko monthly-budget repair

**Operator ruling 2026-08-21:** `B ARCHITECTURE + C INITIAL PROFILE / NO KEYLESS
BRIDGE / CG DISCOVERY DARK UNTIL SEP-1 / MONTHLY CREDIT GOVERNOR REQUIRED`.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| API quota / credit metering | none found — hub lists no provider-billing meter skill | build from scratch (provider-specific: CG's credit semantics — 200 deducts, 4xx/5xx do not — are not generic) |
| Rate limiting / backoff | already in-tree (`coingecko_limiter`) | reuse; this work adds the ORTHOGONAL monthly axis, does not touch rate limiting |
| Scheduling / cadence gating | already in-tree (`COINGECKO_MIDCAP_SCAN_INTERVAL_CYCLES`, `HELD_POSITION_PRICE_REFRESH_INTERVAL_CYCLES`) | reuse the existing cycle-counter idiom rather than inventing a scheduler |

awesome-hermes-agent ecosystem check: no provider-credit-budget component.
Verdict: the cadence work reuses in-tree idioms; only the monthly-credit ledger
is genuinely new, and it must be provider-specific.

## Root cause (retained verbatim per ruling)

This was **not** a "CoinGecko is too expensive" incident. It was a
**resource-model defect**: the system modeled **calls/minute** while the actual
hard production constraint was **calls/month**. CoinGecko treats those as
independent limits. The config's own design note reasons entirely about "the
25/min limiter" and never mentions monthly credits.

Evidence: designed bundle 8 calls/cycle x 1440 cycles/day = 11,520/day against a
3,226/day allowance (100k / 31d). Survived 21 days only because 429 backoff was
accidentally acting as a governor.

## Scope

### 1. Cadence — B architecture, C initial profile

- Main pipeline cadence stays **60s**. Unchanged.
- DexScreener / GeckoTerminal and all other free leading sources stay at 60s.
  Untouched.
- CG **discovery** lanes run every **8th** main cycle
  (`COINGECKO_DISCOVERY_INTERVAL_CYCLES=8`). See the round-4 section: 5 was
  derived from the main bundle alone and is untenable once the other
  discovery-class consumers are counted.
- `COINGECKO_VOLUME_SCAN_PAGES`: **3 -> 2**.
- **held-position pricing is NOT subject to the discovery cadence gate.** It
  keeps its independent cadence (`HELD_POSITION_PRICE_REFRESH_INTERVAL_CYCLES`)
  and its existing `no_open_trades` no-op.

~~Projected discovery ... ~62k/month~~ **SUPERSEDED — that figure counted the
main bundle ALONE.** The corrected full model is in the round-4 section below.

### 2. Monthly-credit governor

**Two counters — attempts are NOT credits.** CG deducts a monthly credit on
HTTP **200** only; 4xx/5xx do not deduct a monthly credit but DO count against
the per-minute rate limit.

- `cg_http_attempts` — every request. Rate/backoff observability.
- `cg_monthly_credits_observed` — successful billable calls only.

Both persisted (module counters reset on restart — see the in-memory-telemetry
lesson), keyed by billing month. Credits reset on the **1st of each month**.

**Reconciliation:** periodically call `/key` and compare
`cg_monthly_credits_observed` against the provider's remaining/total.
**The provider value is the acceptance truth; the local ledger is the
diagnostic/accounting truth.** Divergence is itself a finding (it means our
model of what bills is wrong).

**Envelopes (v1 — NO spillover):**

| Budget | Initial ceiling |
|---|---:|
| Discovery | 65,000 |
| Critical / held-position reserve | 30,000 |
| Operational / reconciliation margin | 5,000 |
| Total | 100,000 |

Unused critical reserve **must not** automatically spill back into discovery in
v1. That optimization waits for one measured month.

**Degrade, don't cliff:**
- Stop **discovery** when its 65k envelope is exhausted; preserve the critical
  reserve.
- Alert on abnormal **pacing** — compare *projected* month-end consumption
  against days remaining, not only absolute 50/75/90% thresholds.
- Page if projected month-end materially exceeds budget.

### 3. Open-gate liveness (the mandatory pre-September safety check)

**Finding.** The exit evaluator re-prices only from `price_cache`.

> ~~`price_cache` is written EXCLUSIVELY from CG lanes ... DexScreener/
> GeckoTerminal do NOT write it.~~ **RETRACTED 2026-08-21 (review round 2).**
> This was asserted, not audited, and it is false:
> `outcome_ledger._poll_dex_enrollments` prices `dex:` tokens from DEXSCREENER
> and writes them through `Database.cache_prices`. The retraction matters
> because the first version of the gate was BUILT on the false claim — it read
> `MAX(price_cache.updated_at)` as a CoinGecko liveness signal, so a fresh
> DexScreener row could present a dead CoinGecko as alive. Liveness is now taken
> from `cg_budget.last_success_at`, set only by a CoinGecko HTTP 200.

`resolve_price_source` admits a token iff it is CG-id-shaped ("the CG lanes
serve it") OR a `price_cache` row exists. That is a **registry** check, not a
**liveness** check — and the "CG lanes serve it" premise is false while CG is
dark or its envelope is exhausted.

Consequence today: a position could open, then be unmonitored (no trailing stop,
no SL) until `max_duration` force-closes it at `entry_price` with a fabricated
`pnl_pct=0` (`exit_reason="expired_stale_no_price"`).

**Fix.** Add a liveness condition to the open boundary: refuse to open a
position whose only registered price source is CG when CG pricing is not
currently obtainable (provider refusing, or discovery/critical envelope
exhausted, or `price_cache` staleness beyond a configured bound). Emit a
distinct rejection reason so this is never confused with the existing
unregistered-source rejection.

Risk is currently LATENT (0 open trades, signals suspended) — the ruling's point
stands: do not discover the dependency after a position opens.

## Anti-scope

- No keyless/anonymous CG calls. Using anonymous access *because* the paid
  monthly quota is exhausted is quota-circumvention under CG's API terms
  regardless of whether the endpoints answer today.
- No CG discovery before the **September 1** credit reset.
- No change to rate limiting / backoff (orthogonal axis).
- No spillover between envelopes in v1.
- No restoration of `VOLUME_SCAN_PAGES=3` without a full measured billing month
  AND evidence of discovery-quality loss from 2 pages.

## Acceptance

- Discovery cadence provably decoupled from the main cycle; free lanes unaffected.
- held-position pricing provably NOT gated by the discovery cadence.
- Credits counted on 200 only; attempts counted on every request; both survive
  restart.
- Discovery halts at its envelope while critical reserve remains spendable.
- Open gate refuses a CG-only token when CG pricing is not obtainable.
- Every threshold sourced from Settings.


## Review round 2 (2026-08-21) — five blockers, corrected

Green CI was not discriminating against any of these.

**S1 liveness used the wrong axis.** The gate read
`MAX(price_cache.updated_at)` and asserted price_cache had a single writer
family. FALSE: `outcome_ledger._poll_dex_enrollments` prices `dex:` tokens from
DEXSCREENER and writes them through `Database.cache_prices`. A fresh Dex row
made a dead CoinGecko look live. Liveness is now
`cg_budget.last_success_at` — set ONLY by a CoinGecko HTTP 200 — applied only to
`price_source == cg_lane`. Both discriminators are pinned.

**S1 the "single choke point" was not single.** Review named three ungoverned
direct CG paths. The full sweep found **eight**: `narrative/observer`,
`narrative/predictor`, `narrative/evaluator` (TWO — `fetch_prices_batch` and a
separate `/simple/price` fallback), `counter/detail`, plus three the review did
not list — `briefing/collector`, `secondwave/detector`,
`social/telegram/resolver`. All now route through `_get_with_backoff` or
`governed_cg_call`, and `tests/test_no_ungoverned_coingecko_paths.py` is a
STRUCTURAL tripwire (with a self-test proving it can fail) so a new direct path
cannot be added silently.

**S1 envelopes were accounting, not enforcement.** Enforcement moved to the HTTP
primitive, so a lane that never consults the budget still cannot spend. All three
buckets enforce, plus the overall allowance. `critical` is deliberately SOFT:
refusing to re-price an open position is worse than overspending a reserve
because it recreates the fabricated-$0 close; exceeding it blocks NEW opens.

**S2 `/key` reconciliation was dead code.** Now called from the hourly pass
BEFORE the snapshot is read, and positive drift feeds `effective_used()`, which
gates the allowance and is charged to DISCOVERY for cap purposes. Provider truth
changes capacity rather than producing a log line.

**S2 durability was weaker than claimed.** Persisting only hourly lost up to an
hour of spend to a crash. Added `maybe_persist` on the per-cycle hook, bounded by
CREDITS (`COINGECKO_BUDGET_FLUSH_EVERY_CREDITS=25`) because exposure scales with
spend, not clock. Tested through the runtime path, not by calling `persist()`
from the test.

**Operability/truth fixes.** `_get_with_backoff` now records exactly once via
`finally` (it previously double-counted when `resp.json()` threw after a 200).
The pace alert fires on the CROSSING with hysteresis rearm, not every hour.

**Deployment boundary.** `COINGECKO_DISCOVERY_ENABLED` defaults **False**, so
"dark until the September 1 reset" is a property of the tree rather than of
deployment timing — a pre-reset deploy starts with an empty ledger and would
otherwise resume discovery on the 5th cycle.


## Review round 4 (2026-08-22) — the model, corrected

**The 62k headline was wrong three times, each for a different reason.** Round 4
found the third: the model used FIXED per-pass estimates for consumers whose call
count is a LOOP BOUND.

`fetch_laggards` runs once per heating category; `fetch_coin_detail` runs for
every scored token AND again for every control token. Worse, the multipliers are
**learner-tunable** — `strategy_bounds` permits `max_heating_per_cycle` and
`max_picks_per_category` up to 10 each:

    1 + 1 + 10 + (10x10) + (10x10) = 212 calls/pass
    x 48 passes/day x 31 = ~315,000/month   from ONE consumer, against a 100,000 plan

Even at today's 5x5 defaults it is 84,816/month — more than the entire 65,000
discovery envelope. **No main-bundle cadence can absorb that**, so the fan-out
itself is now capped: `NARRATIVE_MAX_CG_CALLS_PER_PASS=8` covers laggards and
both detail loops, degrading the pass (fewer tokens enriched) rather than failing
it. The model then holds regardless of learner tuning.

**Measured production activation was NOT used as the bound.** 2026-08-19 shows
`narrative.observe_empty` x44 with zero heating/laggard/detail events — the pass
aborts on an empty `/coins/categories`, so today's cost is ~1 call/pass. That is
an artifact of a failing upstream and would vanish the moment categories returns
data. Modelling on it would repeat the original mistake.

### Corrected complete model (structural bounds, `COINGECKO_DISCOVERY_INTERVAL_CYCLES=8`)

| consumer | bucket | /month |
|---|---|---:|
| main discovery bundle (7 calls, every 8th cycle) | discovery | 39,060 |
| narrative (capped: 2 + 8 per pass, 48 passes/day) | discovery | 14,880 |
| secondwave (1 call, 1800s loop) | discovery | 1,488 |
| **discovery total** | | **55,428 / 65,000 — 14.7% margin** |
| ledger CG enrollment poll (every 15th cycle) | operational | 2,976 |
| /key reconciliation (hourly) | operational | 744 |
| telegram resolver (event-driven estimate) | operational | 620 |
| **operational total** | | **4,340 / 5,000 — 13.2% margin** |
| held-position re-pricing (0 open positions) | critical | 0 / 30,000 |
| **plan total** | | **59,768 / 100,000** |

### Also corrected in round 4

- **Critical reserve is provider-aware for NEW demand.** `critical_reserve_exceeded`
  compared local critical credits to the local cap only, so provider=90k/100k with
  local critical 0/30k read "reserve healthy" while just 10k of real capacity
  remained. New CG-dependent opens now also require provider-corrected remaining
  capacity to still cover the unspent reserve. Re-pricing an EXISTING position
  stays soft.
- **Tripwire is callsite-scoped.** Function-level still passed a function holding
  one governed AND one raw request. Raw `session.get` calls must now be covered by
  `governed_cg_call` wrappers specifically — `_get_with_backoff` REPLACES a raw
  request rather than wrapping one, so counting it as cover was the hole.
- **Dark-gate test is behavioural.** It now invokes
  `secondwave.fetch_current_prices` with an exploding session and asserts zero
  calls, plus a complement proving the same caller DOES reach HTTP when enabled —
  otherwise an inert caller would satisfy the zero-HTTP assertion.
- **Attempts are recorded at ISSUE time**, via `issued()` immediately before the
  HTTP op, not in the constructor. Constructing before the rate limiter and
  counting there turned a cancellation-while-waiting into a phantom attempt.
