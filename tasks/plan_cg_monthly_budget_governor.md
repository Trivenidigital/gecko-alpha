**New primitives introduced:** `CoinGeckoBudgetGovernor` (monthly-credit ledger + envelope enforcement), `cg_credit_ledger` table, `COINGECKO_DISCOVERY_INTERVAL_CYCLES` / `COINGECKO_MONTHLY_CREDIT_*` settings, `cg_pricing_live` open-gate liveness predicate.

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
- CG **discovery** lanes (`top_movers`, `trending`, `by_volume`, `deep_volume`,
  `midcap_gainers`) run every **5th** main cycle
  (`COINGECKO_DISCOVERY_INTERVAL_CYCLES=5`).
- `COINGECKO_VOLUME_SCAN_PAGES`: **3 -> 2**.
- **held-position pricing is NOT subject to the discovery cadence gate.** It
  keeps its independent cadence (`HELD_POSITION_PRICE_REFRESH_INTERVAL_CYCLES`)
  and its existing `no_open_trades` no-op.

Projected discovery: (top_movers 2 + trending 2 + by_volume 2 + deep_volume 1)
= 7 calls per discovery round x 288 rounds/day = **2,016/day ~= 62k/month**.

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

**Finding.** `price_cache` is written EXCLUSIVELY from CG lanes
(`main.py` `all_raw` = markets + trending + by_volume + midcap + deep_volume +
held_position; plus `narrative/agent.py` and `outcome_ledger.py`, both CG).
DexScreener/GeckoTerminal do NOT write it. The exit evaluator reads only
`price_cache`.

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
