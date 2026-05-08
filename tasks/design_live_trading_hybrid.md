**New primitives introduced:** v2 architectural redesign (2026-05-08) — supersedes v1 (2026-05-06) single-venue framing per operator constraint "design for ≥10 venues, target 20-30." NEW PRIMITIVES IN V2: (1) Three-tier adapter pattern under shared `ExchangeAdapter` ABC — Tier 1 AI-native CLI (kraken-cli), Tier 2 Aggregator (Minara DEX), Tier 3a Bespoke (BL-055/Binance — KEPT, not retired), Tier 3b CCXT-backed (`CCXTAdapter` parameterized by venue, new). (2) Routing layer (`scout/live/routing.py`) producing ranked candidate list per signal-fire — NEW module. (3) Per-venue services framework — `VenueService` ABC + `venue_health` table + `wallet_snapshots` table (PROMOTED from Tier-2 in v1) + service-runner harness. (4) `symbol_aliases` table + `canonical_symbol(venue, venue_pair)` lookup. (5) Operator-in-loop scaling rules pre-registered (new-venue-gate <30 fills + trade-size-gate >2× median + venue-health-gate degraded-24h + 24h Telegram override). (6) Pre-registered architectural commitment: "venue #N onboard = adapter-config + per-venue service spin-up only; rework is escalation, not silent absorption." (7) Reconciliation per-venue cadence design (CEX fast-poll, EVM-L1 block-time, EVM-L2 short-poll, Solana short-poll). (8) Listing-coverage interpretation note (route across 20-30, fund 5-10, fund 1-2 at M1 launch — operator-flagged drift trigger). KEPT FROM V1: 4-layer kill stack (`LIVE_TRADING_ENABLED`, `LIVE_MODE`, per-signal opt-in, kill_switch), hard capital caps, idempotency contracts (Binance `client_order_id`, Minara intent_uuid), pre-registered approval-removal criteria (now extended to per-venue), Phase 3 trigger criteria, Minara liveness probe + circuit-breaker, OverrideStore decoupling.

# Live Trading — Multi-Venue Hybrid Execution Architecture (v2)

**Date:** 2026-05-08 (v2 — supersedes v1 dated 2026-05-06)
**Status:** DESIGN — pending three-vector reviewer pass + operator review
**Companions:**
- `tasks/findings_minara_verification_2026_05_06.md`
- `tasks/findings_ccxt_verification_2026_05_08.md`
- `tasks/design_live_trading_hybrid_v1_archived.md` (v1, retained for diff/audit)
**Decision-of-record for:** BL-055 (CEX live) + BL-074 (DEX via Minara) + CCXTAdapter long tail + kraken-cli Tier-1 + multi-venue routing + per-venue services framework

## Why v2

V1 (2026-05-06) was scoped for 1-2 venues based on the operator's then-implicit assumption. On 2026-05-08, operator clarified: **"out of 20-30 exchanges we are going to trade in future"** — this is a hard constraint, not a forecast.

At N≥10 venues, the v1 architecture doesn't survive cleanly:
- Per-venue bespoke adapters become the dominant cost (10-30 × ~1500 LOC = ~30k LOC of Binance-style debt)
- Capital allocation flips from "look at the balance" to a non-trivial multi-venue query
- Routing becomes a real layer, not an `OverrideStore` hand-edit
- Per-venue health monitoring becomes blocking infrastructure
- Reconciliation latency varies by 100×+ across venues (CEX ~100ms vs Ethereum ~12s)

V2 inverts the adapter pattern: instead of "an adapter per venue," it ships **a small number of adapter PATTERNS that each cover many venues**, plus a routing layer + per-venue services framework that stays venue-agnostic.

## Hermes-first analysis (v2)

| Domain | Hermes/library coverage? | Decision |
|---|---|---|
| Multi-chain DEX execution + wallet custody | Minara (`Minara-AI/skills`) — verified 2026-05-06 | **Tier 2 — Minara via CLI shell-out for DEX** |
| Hyperliquid perps | Minara built-in | **Tier 2 — same Minara adapter** |
| Kraken spot/perps + tokenized US equities | kraken-cli (`krakenfx/kraken-cli`) — verified today | **Tier 1 — kraken-cli AI-native CLI / MCP-stdio** |
| Binance USDT-margined perps | CCXT (verified today) capability ✅ / maturity ⚠️ | **Tier 3a — KEEP BL-055/binance_adapter; do NOT retire wholesale.** CCXT has known WS reliability gaps. |
| Long-tail CEX (Bybit/OKX/Coinbase/MEXC/Gate/etc.) | CCXT covers all | **Tier 3b — `CCXTAdapter` parameterized per venue** |
| Routing layer (signal → ranked candidates) | None found | Build from scratch (gecko-side) |
| Per-venue services framework | None found | Build from scratch (gecko-side) |
| Operator-in-loop Telegram approval at scale | SMB-Agents reference unverified; will design from scratch if needed | Build minimal, optional pattern reuse if SMB-Agents repo URL surfaces |

**Awesome-hermes-agent ecosystem:** confirmed Minara is the relevant entry; kraken-cli emerged as a separate AI-native official Kraken tool not via Hermes hub. CCXT is library-tier, not Hermes.

**Drift-check:** v1 in tree, archived alongside v2. `scout/live/` has the BL-055 work (~1500 LOC). New primitives in v2 don't conflict — additive.

**Verdict:** four-tier adapter pattern + routing layer + per-venue services. Three of four tiers reuse external tools (kraken-cli, Minara, CCXT); one tier (BL-055) stays bespoke for the validated first venue. The architectural commitment: **adding venue #N is adapter-config + service spin-up, not architectural rework.**

---

## Layered architecture (top-down)

### Layer 1 — Routing layer (NEW in v2)

When a signal fires, the routing layer produces a **ranked candidate list** of `(venue, pair, expected_fill_price, expected_slippage_bps, available_capital_usd, venue_health_score)` tuples. The execution layer takes the top candidate (or asks operator approval per the operator-in-loop scaling rules — see Layer 5).

Inputs:
- **Signal payload** — token (canonical symbol), chain hint, signal_type, intended USD size
- **Listing metadata** — `venue_listings` table; per-venue list of supported tokens, refreshed daily by per-venue listing-fetch jobs
- **Venue health** — `venue_health` table (live, updated by per-venue health probes)
- **Capital availability** — `wallet_snapshots` table (latest snapshot per venue per asset)
- **Expected fill quality** — orderbook depth at trade size + historical (signal_type × venue) slippage from shadow-mode data

Output: ranked list. Routing logic implemented in `scout/live/routing.py` (NEW module). For M1 the routing layer is shipped with a single hardcoded venue (Binance via BL-055) — but the abstraction is in place so venue #2 doesn't require routing-layer rework.

**Override path retained from v1**: `OverrideStore` (simplified schema `(symbol, primary_chain)`) lets operator force a route for cross-listed tokens like the BILL example.

### Layer 2 — Adapter tiers under shared `ExchangeAdapter` ABC

The existing `ExchangeAdapter` ABC in `scout/live/adapter_base.py` is the unified interface. Four sub-types:

#### Tier 1 — AI-native CLI adapter (`CLIBackedAdapter` subclass)

- Wraps subprocess + JSON parse OR MCP-stdio invocation
- **Currently:** kraken-cli (Kraken spot + Kraken `PF_*` perp + xStocks + forex + futures contracts)
- **Future:** other AI-native CLIs as they emerge
- Auth: per-CLI (kraken-cli uses local API keys; Minara uses device-code login)
- Verification baseline: capability ✅, operational maturity per-CLI mixed

#### Tier 2 — Aggregator skill (Minara — DEX + Hyperliquid)

- One adapter covers multiple venues internally
- Minara → Solana + 17 EVM chains + Hyperliquid perps
- Built-in operator-in-loop confirmation (free Phase 0/1 gate when called without `--yes`)
- Verification 2026-05-06: capability ✅, operational layer opaque ⚠️ (closed CLI)

#### Tier 3a — Bespoke per-venue (`BinanceAdapter` — BL-055)

- Direct REST/WS wrapper, ~1500 LOC of tested Python
- **KEPT, not retired** — verification result on CCXT showed websocket reliability gaps that BL-055 already handles
- Single venue (Binance USDT-margined perps + spot)
- Pattern: only justified for venues where (a) we already have working code AND (b) library-backed alternatives have observed operational gaps
- Future bespoke adapters added ONLY by exception, not by default

#### Tier 3b — CCXT-backed long tail (`CCXTAdapter` — NEW in v2)

- One adapter parameterized by venue name (Bybit, OKX, Coinbase, MEXC, Gate, etc.)
- Constructor: `CCXTAdapter(venue_name="bybit", ccxt_options={...})`
- Delegates to `ccxt.<venue>` instance for order placement / balance / depth
- Per-venue subclasses ONLY for advanced features CCXT doesn't expose cleanly (rare; documented as exceptions)
- Pinned CCXT version (no auto-bump) — release evaluation cadence quarterly
- For M1: scaffold only (the class + tests); first wired CCXT venue is M1.x or M2

### Layer 3 — Per-venue services framework (PROMOTED from v1 Tier-2)

At N≥10, these services are infrastructure, not Tier-2 conveniences. Each is venue-agnostic in implementation, parameterized by adapter:

| Service | Writes to | M1 ship? | M1.5+ |
|---|---|---|---|
| **Health probe** | `venue_health` (NEW) | ✅ blocker | scale per venue |
| **Balance snapshot job** | `wallet_snapshots` (PROMOTED from v1) | ✅ blocker | per-asset breakdown |
| **Per-venue kill switch** | `live_control` (existing) | ✅ verify | scale |
| **Rate-limit accountant** | `venue_rate_state` (NEW) | ⚠️ framework only | full impl at N=3 |
| **Reconciliation worker** | reconciles `paper_trades` ↔ `live_trades` | ⚠️ framework only | full impl at N=3 |

**Critical contract: M1 ships the FRAMEWORK** even when only health-probe + balance-snapshot + per-venue-kill workers are implemented. The framework consists of:

1. Concrete `VenueService` ABC with method signatures (`async def run_once(self, adapter: ExchangeAdapter, db: Database) -> None`)
2. Concrete `venue_health` and `wallet_snapshots` table schemas (used now)
3. Concrete `venue_rate_state` table schema (used by stub at M1, real worker at M1.5/M2)
4. Service-runner harness (cron / async-loop register/unregister; like `scout/main._run_feedback_schedulers`)
5. Type stubs / no-op implementations for deferred services so the routing layer doesn't branch on `if service_implemented`

If these aren't in M1, the staged-rollout argument collapses (deferred workers will reshape the framework, forcing rework). Lock these specifics; do not defer the framework itself.

### Layer 4 — Cross-venue accounting

Above per-venue services, a global accounting layer:
- Total deployed capital across all venues (from `wallet_snapshots` + open `live_trades`)
- Per-(signal_type × venue) PnL
- Per-venue PnL trajectory
- Per-venue health summary (uptime, fill quality, slippage attribution)

For M1: **`cross_venue_exposure` SQL view** (already in v1 design, kept) PLUS a new `cross_venue_pnl` view summing realized + unrealized PnL. View-only at M1; materialized aggregations at M2.

### Layer 5 — Operator surfaces

- **Telegram approval gateway** — supports operator-in-loop scaling rules (below)
- **Per-venue + global kill switches** — `LIVE_TRADING_ENABLED` global, `live_control.venue_kill[venue]` per-venue
- **Daily digest** — reads cross-venue accounting layer; covers per-venue PnL + health summary
- **Per-venue health dashboard** — reads `venue_health`; surfaces probe status + recent failures

For M1, daily digest + dashboard are enhancement-class; the kill switches + Telegram gateway are blocker-class.

---

## Routing key (chain-field default + override + listing metadata)

`paper_trades.chain` is the primary default routing hint:
- `chain == "coingecko"` → ranked candidate list prefers CEX venues (Binance, Kraken, CCXT-tier)
- `chain in {"solana","base","ethereum","arbitrum",...}` → ranked candidate list prefers DEX (Minara) AND any CEX that lists the token (per `venue_listings` lookup)

Override path (from v1, kept): `OverrideStore.lookup(symbol)` → `(primary_chain, primary_venue_hint)`. Override fires BEFORE chain-field default. Schema simplified — NO venue-internal pair format leakage (`@raydium` etc. removed).

**Cross-listed token handling (the BILL case):**
- Routing layer queries `venue_listings` for ALL venues that list BILL.
- Returns ranked candidate list including: Binance (if listed), Kraken (if listed), Solana DEX (always for chain="solana" tokens), etc.
- Operator override can pin or de-prioritize specific venues.
- Phase 0/1: operator confirms the chosen venue per trade. Phase 2+: routing layer auto-selects top candidate by ranking score.

---

## Layered kill switches (4-layer, kept from v1)

```
.env LIVE_TRADING_ENABLED=True ──┐
LIVE_MODE in {"shadow","live"} ──┤
signal_params.live_eligible=1 ──┼── (AND) ── live execution proceeds
exposure_cap not breached ──────┤
trade_notional ≤ cap ───────────┤
kill_switch_active=False ───────┤
venue_health=ok ────────────────┘ (NEW in v2: per-venue health gate)
```

Layer 1 (`LIVE_TRADING_ENABLED`), Layer 2 (`LIVE_MODE`), Layer 3 (per-signal `live_eligible`), Layer 4 (`kill_switch`) all from v1. **NEW in v2:** per-venue health gate as part of Layer 4 — venue with degraded health is excluded from routing-layer ranking.

ANY one False → `live_orders_skipped_<reason>` counter + log event + paper_trade unaffected.

---

## Hard capital caps (kept from v1)

```python
LIVE_TRADING_ENABLED: bool = False
LIVE_MAX_TRADE_NOTIONAL_USD: float = 100.0
LIVE_MAX_OPEN_EXPOSURE_USD: float = 1000.0  # SUM across ALL venues
```

Aggregate cap query: `cross_venue_exposure` view sum (already specified in v1; kept).

---

## Per-venue services framework — concrete schemas

### `venue_health` table

```sql
CREATE TABLE venue_health (
    venue                   TEXT NOT NULL,
    probe_at                TEXT NOT NULL,
    rest_responsive         INTEGER NOT NULL,    -- 0/1
    rest_latency_ms         INTEGER,
    ws_connected            INTEGER NOT NULL,    -- 0/1; NULL if N/A for adapter
    rate_limit_headroom_pct REAL,                -- 0-100
    auth_ok                 INTEGER NOT NULL,    -- 0/1
    last_balance_fetch_ok   INTEGER NOT NULL,    -- 0/1
    error_text              TEXT,
    PRIMARY KEY (venue, probe_at)
);
CREATE INDEX idx_venue_health_recent ON venue_health(venue, probe_at DESC);
```

Routing layer reads `MAX(probe_at)` per venue; venues with `auth_ok=0` OR `rest_responsive=0` for last N probes are excluded from candidates.

### `wallet_snapshots` table (PROMOTED from v1 Tier-2)

```sql
CREATE TABLE wallet_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    venue         TEXT NOT NULL,
    asset         TEXT NOT NULL,        -- 'USDT', 'USDC', 'SOL', ...
    balance       REAL NOT NULL,
    balance_usd   REAL,                 -- NULL for non-USD assets if price lookup fails
    snapshot_at   TEXT NOT NULL
);
CREATE INDEX idx_wallet_snapshots_venue_recent ON wallet_snapshots(venue, snapshot_at DESC);
```

Balance-snapshot worker writes one row per venue per asset per snapshot interval (default 5min). Routing layer uses latest per `(venue, asset)`.

### `venue_listings` table (NEW in v2)

```sql
CREATE TABLE venue_listings (
    venue         TEXT NOT NULL,
    canonical     TEXT NOT NULL,        -- "BTC", "BILL", "PEPE"
    venue_pair    TEXT NOT NULL,        -- "BTCUSDT", "PF_XBTUSD", "BTC-USD"
    quote         TEXT NOT NULL,        -- "USDT", "USD"
    asset_class   TEXT NOT NULL CHECK (asset_class IN ('spot','perp','option','equity','forex')),
    listed_at     TEXT,
    delisted_at   TEXT,
    refreshed_at  TEXT NOT NULL,
    PRIMARY KEY (venue, canonical, asset_class)
);
```

Per-venue listing-fetch jobs refresh daily (cheap; delistings are operationally rare). Routing layer JOINs against this for "which venues list this token?"

### `symbol_aliases` table (NEW in v2 — symbol normalization)

```sql
CREATE TABLE symbol_aliases (
    canonical    TEXT NOT NULL,         -- "BTC"
    venue        TEXT NOT NULL,
    venue_symbol TEXT NOT NULL,         -- venue's pair string
    PRIMARY KEY (canonical, venue)
);
```

For Tier-3b CCXTAdapter, this is mostly populated automatically from CCXT's `markets[symbol]` structure. For Tier-1 (kraken-cli) and Tier-2 (Minara), populated by adapter-specific normalization (e.g., kraken's `XBT/USD` ↔ canonical `BTC`).

### `venue_rate_state` table (NEW in v2 — framework only at M1)

```sql
CREATE TABLE venue_rate_state (
    venue                TEXT PRIMARY KEY,
    last_updated_at      TEXT NOT NULL,
    requests_per_min_cap INTEGER NOT NULL,
    requests_seen_60s    INTEGER NOT NULL DEFAULT 0,
    headroom_pct         REAL NOT NULL DEFAULT 100.0
);
```

Rate-limit accountant writes; routing layer reads. M1 ships the table + a stub accountant that reports static 100% headroom (so routing layer can call it without conditional). Real accountant lands in M1.5/M2.

### `VenueService` ABC

```python
# scout/live/services/base.py
from abc import ABC, abstractmethod

class VenueService(ABC):
    @abstractmethod
    async def run_once(self, *, adapter, db, venue: str) -> None:
        """Single iteration of this service for the given venue.

        Implementations: HealthProbe, BalanceSnapshot, RateLimitAccountant,
        ReconciliationWorker. Each writes to its own table. Service-runner
        harness invokes `run_once` on configured cadence per venue.
        """
        ...

    @abstractmethod
    def cadence_seconds(self) -> int:
        """How often the runner should call `run_once`. CEX adapters
        typically 30-60s; Solana adapter 30s; Ethereum-mainnet adapter
        60-120s (block-time aware)."""
        ...
```

### Service-runner harness

```python
# scout/live/services/runner.py
async def run_venue_services(
    *, db, adapters: list[ExchangeAdapter], services: list[VenueService]
) -> None:
    """Long-running async loop. Each (adapter, service) pair runs at the
    service's declared cadence. Failures isolated per (adapter, service);
    a failing health-probe on one venue doesn't tear down the loop or
    block other venues' services. Failures logged + tracked in
    venue_health.error_text."""
    ...
```

This is the harness M1 ships. Workers register into it; they don't have to write their own loops.

---

## Operator-in-loop scaling rules (PRE-REGISTERED in v2)

> **Per-trade approval gate fires when ANY hold:**
>
> 1. **New-venue gate:** the (signal_type × venue) pair has < 30 successful autonomous fills.
> 2. **Trade-size gate:** trade notional > 2× the median trade size for this (signal_type × venue) pair (rolling 30-trade window).
> 3. **Venue-health gate:** the target venue had ANY health-probe metric in caution-range (rest_latency_ms > p99_baseline OR rate_limit_headroom_pct < 30 OR auth_ok=0 in last probe) within the past 24 hours.
> 4. **Operator-set explicit-approval flag:** Telegram command `/approval-required venue=X` sets a flag with 24h expiry. Auto-clears.
>
> ALL FOUR FALSE → trade auto-executes (true autonomous).
> ANY ONE TRUE → trade requires operator approval via Telegram before execution.

**Telegram approval gateway:**
- gecko-alpha sends formatted intent to operator chat: token, venue, size, expected fill, capital impact
- Inline buttons: ✅ approve / ❌ abort / ⏭ skip-this-only / 🔁 try-different-venue
- Approval has 5-minute timeout (auto-abort if no response — protects against missed alerts)
- `/approval-required venue=X` and `/auto-approve venue=X` commands toggle gates per venue

**Pre-registration discipline:** thresholds (30 trades, 2× median, 24h, 5min) are committed numbers. Operator can raise via Telegram for 24h windows but cannot lower below pre-registered floors. Same SQL-UPDATE-phantom-prevention discipline as the approval-removal criteria.

---

## Pre-registered approval-removal criteria (v1 kept, extended)

(All 6 criteria from v1 still apply. Re-stated for completeness.)

> **Operator gate is removed from a (signal_type × venue) pair when ALL of the following hold:**
>
> 1. **Trade-count gate:** ≥ 30 live trades on this (signal_type × venue) pair WITHOUT a "correction." A correction RESETS the running counter to 0.
> 2. **Duration floor:** ≥ 14 calendar days have elapsed since Phase 0 activated for this (signal_type × venue) pair.
> 3. **Slippage-fit gate (per-venue, basis points):**
>    - **Binance perp / spot:** ≥ 80% of fills within ±100 bps of mid-price at submit
>    - **Minara DEX:** ≥ 80% of fills within ±600 bps of quoted price at submit
>    - **kraken-cli:** ≥ 80% of fills within ±150 bps of mid-price at submit (slightly looser than Binance reflecting Kraken's typical book depth — adjusted from 100bps in v1; per-venue-empirical)
>    - **CCXT-backed venues:** start at ±200 bps; refine per venue after 100+ fills
> 4. **Reconciliation-clean gate:** No unresolved paper_trades / live_trades discrepancies in past 14 days.
> 5. **Idempotency-clean gate:** No double-fill incidents in past 14 days.
> 6. **Per-venue-uptime gate:** venue_health probe-green ≥ 99% over past 14 days.

### Definition of "correction" (v1 kept)

EITHER (a) operator aborts confirmation OR (b) operator manually unwinds within 24h. Approving-but-judging-it-off does NOT reset.

---

## Reconciliation per-venue cadence (NEW in v2)

Per the advisor's reconciliation-latency-varies miss in v1: reconciliation worker MUST use per-venue cadences. Single global polling interval is N=1-shaped reasoning.

| Venue class | Reconciliation cadence | Confirmation depth | Reconciliation timeout |
|---|---|---|---|
| **Binance / CEX (CCXT-backed)** | 30s poll | n/a (account update is instant) | 5min |
| **Hyperliquid (via Minara)** | 30s poll | n/a | 5min |
| **Solana (via Minara)** | 30s poll | ~32 slots (~13s) | 10min |
| **EVM L2 (Base, Arbitrum, Optimism via Minara)** | 60s poll | 12-30 blocks (~3-5min @ 1-4s blocks) | 30min |
| **EVM L1 (Ethereum mainnet via Minara)** | 120s poll | 12 blocks (~150s) | 2h (during congestion) |

This is the architectural spec for the reconciliation worker (NOT M1 code; M1 ships the `VenueService` ABC + framework + table schemas; full reconciliation worker is M2). Pre-specifying it prevents M2 from drifting.

---

## Listing-coverage interpretation (NEW in v2)

Three plausible operator-intent interpretations:

1. **Literal:** 20-30 funded accounts
2. **Aspirational:** capable of 20-30, fund 5-8
3. **Listing-coverage:** route across 20-30, fund 3-5, smart routing layer

**Working assumption (per advisor's read of operator's "FYI" tone):** interpretation **(3) — listing-coverage**. Architecture supports 20-30 venues for routing/listing-metadata; selective funding of 5-10; M1 launch ships with 1-2 funded venues wired.

**Operator-flagged drift trigger:** if interpretation drifts toward (1) (need to fund 20+ accounts), operator must explicitly raise that — design assumes (3) and would need rework for (1). Architecture supports all three; (3) is the working scope.

---

## Pre-registered architectural commitment (NEW in v2)

> When venue #N (N>2) onboards, the addition is:
> - Adapter-config work (instantiate `CCXTAdapter("bybit")` or write a thin Tier-1 CLI wrapper, etc.)
> - Per-venue service spin-up (register the venue's adapter with the running service-runner harness)
> - Listing-fetch job per-venue config
> - Symbol-aliases population (mostly auto for CCXT)
>
> If venue #N requires architectural rework (rewriting routing layer, redesigning service framework, schema changes), that's a **signal the architecture is wrong — escalate, don't quietly absorb the rework into venue #N's scope.**

This is the architectural equivalent of operator-removal criteria. Pre-registered failure mode that escalates rather than silently drifts.

---

## Hybrid (C) routing — kept from v1

Same chain-field-based routing at the top level. What changed: the CEX path is no longer "single Binance adapter" but "ranked candidate list across all CEX-tier adapters" via the routing layer.

---

## Serial milestones (kept from v1, scope-revised for v2)

### Milestone 1 — vertical-slice architectural M1 (REVISED)

V1 scope: "ship BL-055/Binance live; ~11 tasks." V2 scope: "ship the multi-venue architecture with 1 venue (Binance via BL-055) wired live; ~25-35 tasks."

**M1 ships:**
1. Layer 1 — routing layer with hardcoded Binance candidate (abstraction in place; only one venue listed in `venue_listings`)
2. Layer 2 — `ExchangeAdapter` ABC kept; BL-055/binance_adapter wired (Tier-3a); `CCXTAdapter` SCAFFOLD (no venue wired yet); `MinaraAdapter` deferred to M2; `KrakenCliAdapter` deferred to M1.5
3. Layer 3 — `VenueService` ABC + service-runner harness + 3 workers (HealthProbe, BalanceSnapshot, per-venue kill switch)
4. Layer 4 — `cross_venue_exposure` view (kept) + new `cross_venue_pnl` view scaffold
5. Layer 5 — Telegram approval gateway + global kill switch + `LIVE_TRADING_ENABLED` master kill
6. All v1 carryovers: 4-layer kill stack, hard capital caps, idempotency contracts, pre-registered approval-removal criteria, master kill .env flag, balance_gate.py, client_order_id contract
7. NEW: operator-in-loop scaling rules pre-registration; symbol_aliases table + lookup; venue_listings + venue_health + wallet_snapshots tables; per-venue reconciliation cadence design (architecture only, worker in M2); kraken-cli paper-mode validation surface

**M1 explicitly does NOT ship:**
- Wired Minara adapter (M2)
- Wired kraken-cli adapter (M1.5)
- Wired CCXT venue (M1.5)
- Reconciliation worker (M2; framework + ABC ships M1)
- Rate-limit accountant beyond stub (M2)
- Materialized cross-venue PnL aggregations (M2)

**Milestone 2 trigger criteria (v1 kept, refined):**

> Milestone 2 (DEX-live + multi-venue expansion) activates when ALL hold:
> 1. M1 has been live ≥ 30 days
> 2. Approval-removal criteria (1)–(6) met for ≥ 1 (signal_type × Binance) pair
> 3. Net live PnL on M1 ≥ 70% of paper-projected PnL (one-sided gate)
> 4. Minara verification re-confirmed if version changed
> 5. CCXT verification re-confirmed if version changed
> 6. No unresolved reconciliation issues in past 14 days
> 7. **NEW v2:** Per-venue services framework has run cleanly for 14 consecutive days (health probe + balance snapshot writing without errors)

### Milestone 1.5 — second venue (NEW in v2)

Between M1 and M2: wire venue #2. Likely Kraken (via kraken-cli) OR a CCXT-backed venue (Bybit / OKX). This validates the architectural commitment (no rework needed).

---

## kraken-cli paper-mode validation surface (PROMOTED in v2)

Per advisor's recommendation, kraken-cli's built-in paper-trading mode is promoted from "interesting follow-up" to **architectural M1 validation surface**.

How it's used: gecko-alpha's signals are dispatched to BOTH (a) BL-055 Binance shadow-mode (existing) AND (b) kraken-cli paper-mode for any signal whose token is listed on Kraken. Two independent shadow-validation surfaces means we can compare paper-mode fill predictions across venues without funded accounts on either side.

Implementation: `scout/live/kraken_cli_paper.py` — thin wrapper around `kraken futures paper buy/sell` and `kraken paper buy/sell` (no auth needed for paper mode). M1 ships read-only; data flows into the `cross_venue_pnl` view but doesn't drive any execution decisions.

Cost: ~1 day of work for the wrapper + listing-fetch job. Value: independent validation of execution model assumptions before any real money lands anywhere.

---

## Operational maturity caveats (kept from v1, extended)

- **Minara CLI:** opaque slippage / RPC / gas / partial-fill handling. Operator-in-loop catches anomalies in Phase 0/1; circuit-breaker on excess timeouts.
- **CCXT (per 2026-05-08 verification):** websocket reliability gaps (#10754, #26945); recent inheritance fix (PR #28493 fragility signal); pin version, no auto-bump.
- **kraken-cli:** AI-native official Kraken tool; agent-side responsibility for slippage/balance gates (limited built-in safeguards beyond `--validate` dry-run and "dangerous" command tagging).
- **BL-055/binance_adapter:** in-house, ~1500 LOC, tested in shadow mode. Maintenance burden is ours.

---

## What's explicitly NOT in scope (kept + extended)

- **Withdrawals / deposits / cross-chain bridging** — Minara CLI supports these; we assume venues stay funded.
- **Multi-asset trading (xStocks, forex, futures contracts beyond perps)** — kraken-cli supports these; gecko-alpha is crypto-only.
- **Smart routing across venues at sub-second latency** — routing layer is pre-trade ranking; not a sub-second execution router.
- **Hyperliquid `perps-autopilot`** — Minara feature, deferred to post-M2 evaluation.
- **CCXT-replaces-BL-055 retirement migration** — design retains BL-055; migration would be a separate decision based on observed BL-055 maintenance burden post-M1.
- **CCXT bumping / release evaluation tooling** — quarterly manual review, no automation.

---

## Open questions for the operator

1. **Confirm listing-coverage interpretation (3).** Architecture supports 20-30 venues for routing; 5-10 funded; 1-2 funded at M1 launch. Confirm or correct.
2. **Funded accounts now or imminently** (Binance, Kraken, Bybit, OKX, Coinbase, other, none-yet). Determines M1's wired venue + M1.5's second venue.
3. **CCXT migration appetite for Binance.** Verification result: keep BL-055 for Binance (websocket reliability gaps in CCXT). Default: NO (don't retire BL-055). Confirm or override.
4. **SMB-Agents Telegram approval gateway pattern repo URL** — still unverified from v1. If repo URL surfaces, design Phase 5 reuses; otherwise designed from scratch (~1-2 days).
5. **balance_gate.py status** — was missing as of 2026-05-03. Plan rewrites assume we implement it. Confirm.

---

## References

- `tasks/findings_minara_verification_2026_05_06.md`
- `tasks/findings_ccxt_verification_2026_05_08.md`
- `tasks/design_live_trading_hybrid_v1_archived.md` (v1, retained for diff)
- BL-055 spec
- BL-074 vision capture (`memory/project_bl074_minara_vision_2026_05_03.md`)
- kraken-cli verification (this session, captured inline)
