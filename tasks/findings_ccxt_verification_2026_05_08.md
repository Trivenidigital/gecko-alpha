**New primitives introduced:** NONE (findings-only document; informs `tasks/design_live_trading_hybrid.md` v2 architectural redesign).

# Findings: CCXT Verification 2026-05-08

**Date:** 2026-05-08
**Status:** COMPLETE
**Trigger:** operator constraint clarified — design for ≥10 venues, target 20-30. Single-venue per-venue bespoke adapters (BL-055 pattern) don't scale. Verification needed before committing to CCXT as the Tier-3 long-tail solution.
**Method:** GitHub repo metadata + binanceusdm.py source survey + open-issue scan + release-cadence + recent-PR-velocity check.

## 1. Project framing

`ccxt/ccxt` — multi-language (JS/TS/Python/C#/PHP/Go) crypto trading API library wrapping 100+ exchanges. **42,341 stars**, 8,652 forks, weekly release cadence (latest v4.5.52 on 2026-05-05, three days ago). Active maintenance — pushed today (2026-05-08).

This is the canonical "unified library" for the long-tail Tier-3 adapter pattern. No serious competitor at this scale.

## 2. Capability surface (binanceusdm-specific)

`ccxt.binanceusdm` is a **65-LOC Python subclass** of `ccxt.binance`. Inheritance: `class binanceusdm(binance, ImplicitAPI)`. Overrides `describe()` to configure USDⓈ-M settings; the actual order/balance/leverage logic is inherited from the parent.

Per CCXT documentation + general knowledge of the parent `binance.py` module:

| Feature | Supported? |
|---|---|
| `set_leverage(leverage, symbol)` | ✅ |
| `set_position_mode(hedged, symbol)` (HEDGE / ONE-WAY) | ✅ |
| `create_order(reduceOnly=...)` | ✅ |
| `STOP_MARKET` / `STOP` (stop-limit) | ✅ |
| `timeInForce='GTX'` (postOnly on perps) | ✅ |
| `fetch_balance()` distinguishing spot vs futures | ✅ |
| Websocket order updates (`watch_orders`) | ⚠️ — see operational-maturity issues |

**Capability verdict: comprehensive.** No structural gaps preventing migration.

## 3. Operational maturity

### Active maintenance signals (positive)

- Weekly releases — v4.5.46 (2026-03-31) → v4.5.52 (2026-05-05). Six releases in ~5 weeks.
- 102 historical merged PRs touching binanceusdm — meaningful fix density.
- Recent fix landed 2026-05-03 (PR #28493 `fix(binanceusdm): inheritance`) — issues caught + fixed within days.
- 1322 total open issues for a project this size is reasonable; ratio of activity to backlog is healthy.

### Operational-maturity concerns (yellow flags)

- **22 open issues** mentioning binanceusdm. Subset:
  - **#10754 (open since ~2024):** "watch_orders incremental data structures and lost updates" — websocket reliability gap, multi-year. Architectural class issue.
  - **#26945 (2026-02-17):** "watchTrades does not backfill after WS reconnect (code 1006); feature request for built-in gap-fill" — feature missing, not a regression.
  - **#25666 (2026-01-28):** "-2015 Error Code: Invalid API-key, IP, or permissions for action" — auth-flow edge case, unresolved.
  - **#27544 (2025-12-21):** "Binance Demo lot of exceptions suddenly" — recent disruption.
  - **#23081 (2026-04-16):** "Authenticate exchange:" — open auth issue.
- **Recent inheritance fix (5 days ago)** — suggests parent-child contract is fragile under refactoring. Not a deal-breaker; signals "expect occasional breakage on bumps."
- **Standard CCXT pattern**: library lags exchange API changes by days/weeks. Operator should expect to hold a CCXT version stable rather than auto-bump.

### Maturity verdict per advisor's framing

> "Capability vs. operational maturity. Capability says 'feature exists.' Operational maturity says 'feature works at scale, fails predictably, and recovers cleanly.'"

- ✅ **Capability:** comprehensive across all relevant perp + spot operations.
- ⚠️ **Operational maturity: MIXED.** REST surface is solid; websocket reliability has known multi-year gaps; recent inheritance fix is a fragility signal. Not catastrophic but not "drop in and forget."

**Verdict: partial savings, not clean win.** Same architectural-shape as Minara verification was. Acceptable for venues we're going to use CCXT for; unwise for venues where we already have working bespoke code (Binance via BL-055).

## 4. Architectural decision — hybrid, NOT full retirement of BL-055

The advisor's framing was binary: *"BL-055 either retires in favor of `CCXTAdapter('binance')`, or kept as thin Binance-specific subclass on top."*

**Verification supports a third option: keep BL-055 for Binance; CCXTAdapter for the Tier-3 long tail.**

Reasoning:

1. **BL-055 is already tested.** ~1500 LOC + shadow-mode validation. Replacing tested code with a less-tested CCXT integration adds risk without clear reward.
2. **CCXT's websocket reliability gaps would force us to write our OWN websocket reconnect-and-backfill logic on top of CCXT** — reintroducing the bespoke-code anti-pattern we're supposedly escaping.
3. **For new venues (Bybit, OKX, Coinbase, MEXC, etc.), CCXTAdapter is clearly right** — custom-per-venue at N≥10 is the textbook anti-pattern.
4. **Selective bespoke escape hatch:** if a future venue's CCXT integration shows similar maturity gaps (websocket reliability, exchange API drift), we go custom for that one venue without architectural rework.

This mirrors the established hybrid pattern in this project:
- Minara for DEX (Solana + EVM) — works there, doesn't work for Binance perps
- Kraken-cli for Kraken specifically — AI-native, official Kraken
- BL-055 for Binance — tested, mature
- CCXT for everyone else — long-tail uniformly

The architecture is intrinsically multi-vendor at the adapter layer.

## 5. Implications for design doc + M1 plan

### Design doc additions required

- **Three-tier adapter pattern** — not "two-tier (BL-055 + Minara)" as v1 framed it.
  - Tier 1 — AI-native CLI (kraken-cli; future others)
  - Tier 2 — Aggregator skill (Minara DEX + Hyperliquid)
  - Tier 3a — Bespoke per-venue (BL-055/Binance — first venue, tested)
  - Tier 3b — CCXT-backed (Bybit, OKX, Coinbase, MEXC, etc. — long tail)
- **`ExchangeAdapter` ABC remains the abstraction** — all four sub-types implement it. Routing layer is adapter-agnostic.
- **CCXTAdapter as a NEW primitive** — `class CCXTAdapter(ExchangeAdapter)` parameterized by venue name. Implementation is thin (delegate to `ccxt.<venue>` instance) + per-venue subclasses for advanced features CCXT doesn't expose cleanly.

### M1 plan implications

- **BL-055/binance_adapter stays as Tier-3a** — first wired venue. No retirement.
- **CCXTAdapter scaffolding ships in M1** as a new primitive — not wired to any venue yet, but the abstraction is in place so venue #2 is adapter-config work.
- **CCXT version pinning policy** — pin a known-good CCXT release; don't auto-bump. Document in deploy runbook.

### What changes in the architectural M1 vs the original M1 plan

The original M1 plan (`tasks/plan_live_trading_milestone_1_cex.md`) was 11 tasks / ~50 steps, focused on enabling Binance live execution via BL-055 + the 4-layer kill stack + capital caps + balance gate + idempotency contract.

The architectural M1 adds:
- Routing layer (~5-8 tasks)
- `CCXTAdapter` ABC scaffold (~2-3 tasks)
- Per-venue health probe service (~3-4 tasks)
- `wallet_snapshots` job + table (now Tier-1) (~2-3 tasks)
- Cross-venue accounting layer (~3-4 tasks)
- Per-venue kill switch (already in BL-055; verify, don't rewrite) (~1 task)
- Symbol normalization layer (~3 tasks)
- Operator-in-loop scaling rules + Telegram approval gateway (~4-6 tasks)
- Reconciliation worker scaffold (per-venue cadence; M2 fills it in) (~2 tasks)

Order-of-magnitude: ~25-35 additional tasks. ~2x the original M1, not 3x. The advisor's "2-3x" estimate was honest at order-of-magnitude; reality is closer to 2x.

## 6. Open issues to monitor (post-M1)

- CCXT websocket reliability — if we end up using CCXT for venues with significant order-state-via-WS dependencies, plan for our own reconnect-and-backfill wrapper (or use REST polling instead). Likely M2.5 work.
- CCXT version pinning — establish a process for evaluating CCXT releases against breaking-change risk before bumping. Quarterly is probably right cadence.
- CCXT auth-error handling — open issue #25666 (-2015 Invalid API-key) is the kind of error we should normalize across venues. Add to gecko-side adapter wrapper.

## 7. Questions for the operator (still pending answers)

1. **Listing-coverage interpretation correct?** Architecture supports 20-30 venues for routing; 5-10 funded over time; 1-2 funded at M1 launch.
2. **Funded accounts now or imminently?** Determines which 1-2 venues M1 wires initially.
3. **CCXT migration for Binance specifically?** Verification result: keep BL-055 for Binance, use CCXTAdapter for long tail. **Recommend default-NO on retiring BL-055; default-YES on shipping CCXTAdapter scaffold + first usage at venue #2.**

These don't block design-doc redesign or M1 plan rewrite; they DO block which venues get wired in M1's vertical slice.
