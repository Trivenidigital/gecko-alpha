# BL-055 — Live Trading: Execution Core + Binance Spot + Venue Registry

**Status:** Design approved 2026-04-22. Ready for implementation plan.
**Backlog ref:** `docs/superpowers/backlog.md#BL-055`
**Depends on:** nothing
**Blocks:** BL-056 (multi-CEX), BL-057 (on-chain), BL-058 (signal→execution bridge)
**Scope boundary:** Binance spot only. No wallet execution. No other CEXes.

---

## 1. Goals and non-goals

### 1.1 Goal

Build the foundation of live trading in `gecko-alpha` without actually sending real orders in v1. Prove the end-to-end path — paper signal → venue resolution → pre-trade safety gates → shadow order — by running it in production for one week, capturing hypothetical fills and risk decisions, and exposing every failure mode the operator will need to see once real money is on the line.

### 1.2 Non-goals (v1)

- No real order submission. `LIVE_MODE=live` is gated behind a `NotImplementedError` in the balance gate — flipping it at startup causes the process to refuse to launch. Live mode lights up after BL-055's soak test passes and the balance gate is wired.
- No multi-venue support. One CEX adapter (Binance) and one override mechanism. BL-056 handles Bybit/Kraken/etc.
- No on-chain execution. BL-057 handles ETH/BASE/Solana wallet spot.
- No auto-promotion/demotion of signals to live. BL-058 handles `combo_performance`-gated enablement.
- No partial take-profit splits, trailing stops, or OCO orders. Single TP, single SL, single duration-based exit — identical to paper-trading v1.

### 1.3 The three modes

`LIVE_MODE` is a single `Literal["paper", "shadow", "live"]` env var with these semantics:

- **`paper`** (default, unchanged): Existing paper-trading path. No Binance calls. No code changes along this branch. BL-055 code never executes.
- **`shadow`**: Paper trading runs unchanged. Every paper open triggers an async side-effect that resolves the coin to a Binance pair, runs pre-trade gates, walks the orderbook for a realistic fill, and writes a `shadow_trades` row. No orders are sent. Shadow positions are independently evaluated for TP/SL/duration exits using live Binance mid-price.
- **`live`**: Everything shadow does, plus the adapter actually calls `POST /api/v3/order`. Balance is checked pre-trade (via the gate that `NotImplementedError`s in BL-055). Real P&L tracked in `live_trades`.

Shadow is the default target for BL-055 implementation. Live is a future flip.

---

## 2. Architecture

### 2.1 Package layout

```
scout/live/
    __init__.py
    config.py              # LiveConfig — resolves env → typed values, allowlist, sizing
    adapter_base.py        # ExchangeAdapter ABC (minimal: resolve_pair, fetch_depth, fetch_price, send_order)
    binance_adapter.py     # BinanceSpotAdapter — concrete impl, weight-header rate limit
    resolver.py            # VenueResolver + OverrideStore (two classes, one file — tight coupling)
    kill_switch.py         # KillSwitch — trigger/clear/auto_expired, kill_events audit trail
    gates.py               # Pre-trade safety gates: slippage, depth, exposure, cooldown, kill, balance (live only)
    engine.py              # LiveEngine.on_paper_trade_opened — the chokepoint dispatcher
    shadow_evaluator.py    # Async loop: poll open shadow rows, exit on TP/SL/duration
    reconciliation.py      # Boot-time open-row recovery
    metrics.py             # UPSERT helpers for live_metrics_daily
    cli_kill.py            # `uv run python -m scout.live.cli_kill --on/--off` CLI
```

**One responsibility per file.** Every file is independently testable, and no file except `engine.py` imports from more than two siblings.

### 2.2 Data flow

```
paper signal fires
    → PaperTrader.open_trade() commits paper_trades row
    → (chokepoint) asyncio.create_task(live_engine.on_paper_trade_opened(trade))   # fire-and-forget
            → LiveConfig.is_signal_enabled(signal_type)?             → no: log live_handoff_skipped, return
            → KillSwitch.is_active()?                                → yes: log live_handoff_skipped_killed, return
            → VenueResolver.resolve(symbol)                          → pair or None
            → Gates.evaluate(...)                                     → pass or reject_reason
            → if shadow: OrderbookWalker.simulate(depth, size_usd)    → walked_vwap, slippage_bps
            → if live:   BinanceSpotAdapter.send_order(...)           → real fill (BL-055 blocks on NotImplementedError)
            → DB: INSERT INTO shadow_trades (...) or live_trades (...)
            → metrics.inc('shadow_orders_opened' | f'shadow_rejects_{reason}')

ShadowEvaluator (async loop, TRADE_EVAL_INTERVAL_SEC)
    → SELECT * FROM shadow_trades WHERE status='open'
    → for each: fetch Binance mid-price → evaluate TP/SL/duration
    → on exit: walk sell side for realistic exit VWAP → compute realized_pnl_usd
    → transactional UPDATE + daily cap SUM check (M2 from Section 5) → maybe trigger kill
```

### 2.3 SRP chokepoint — `PaperTrader.open_trade()`

One integration point. The entire paper → live bridge lives at the tail of `open_trade()`, after the paper row is committed:

```python
# scout/trading/engine.py
class PaperTrader:
    def __init__(self, *, db, settings, live_engine: "LiveEngine | None" = None):
        self._db = db
        self._settings = settings
        self._live_engine = live_engine                        # constructor-injected, immutable
        self._pending_live_tasks: set[asyncio.Task] = set()

    async def open_trade(self, signal) -> PaperTrade:
        trade = await self._commit_paper_row(signal)            # existing path unchanged

        if self._live_engine is not None \
                and self._live_engine.is_eligible(trade.signal_type):
            if len(self._pending_live_tasks) > 50:
                logger.warning("live_handoff_backpressure",
                               pending=len(self._pending_live_tasks))
            task = asyncio.create_task(
                self._live_engine.on_paper_trade_opened(trade)
            )
            self._pending_live_tasks.add(task)
            task.add_done_callback(self._pending_live_tasks.discard)

        return trade
```

Paper latency is preserved. Any Binance hiccup stays isolated in the task. Done-callback discards completed tasks; bounded-set warning surfaces runaway accumulation.

---

## 3. Data model

### 3.1 New tables

```sql
-- Shadow / live order ledger. Append-only; one row per handoff that passes gates.
CREATE TABLE shadow_trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_trade_id      INTEGER NOT NULL REFERENCES paper_trades(id) ON DELETE RESTRICT,
    coin_id             TEXT NOT NULL,                  -- CoinGecko slug, for cross-ref
    symbol              TEXT NOT NULL,                  -- BASE asset, e.g. "WBTC"
    venue               TEXT NOT NULL,                  -- "binance" for v1
    pair                TEXT NOT NULL,                  -- "WBTCUSDT"
    signal_type         TEXT NOT NULL,
    size_usd            TEXT NOT NULL,                  -- Decimal as TEXT
    entry_walked_vwap   TEXT,                            -- null if rejected pre-fill
    mid_at_entry        TEXT,                            -- reference price for slippage audit
    entry_slippage_bps  INTEGER,
    status              TEXT NOT NULL CHECK (status IN (
        'open','closed_tp','closed_sl','closed_duration','closed_via_reconciliation',
        'rejected','needs_manual_review'
    )),
    reject_reason       TEXT CHECK (reject_reason IS NULL OR reject_reason IN (
        'no_venue','insufficient_depth','slippage_exceeds_cap','insufficient_balance',
        'daily_cap_hit','kill_switch','exposure_cap','override_disabled',
        'venue_unavailable'
    )),
    exit_walked_vwap    TEXT,
    realized_pnl_usd    TEXT,                            -- post-slippage (both legs), final
    realized_pnl_pct    TEXT,
    review_retries      INTEGER NOT NULL DEFAULT 0,
    next_review_at      TEXT,                            -- set when status='needs_manual_review'
    kill_event_id       INTEGER REFERENCES kill_events(id),  -- non-null when kill_switch was the reject reason
    created_at          TEXT NOT NULL,
    closed_at           TEXT
);

CREATE INDEX idx_shadow_status_evaluated ON shadow_trades(status, next_review_at)
    WHERE status IN ('open','needs_manual_review');
CREATE INDEX idx_shadow_closed_at_utc ON shadow_trades(closed_at) WHERE closed_at IS NOT NULL;
-- Note: no idx_shadow_paper_trade_id — FK paper_trade_id + implicit uniqueness pattern is fine for v1.

-- Live orders — same shape, tracked separately per Q3=C (three-table isolation)
CREATE TABLE live_trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_trade_id      INTEGER NOT NULL REFERENCES paper_trades(id) ON DELETE RESTRICT,
    coin_id             TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    venue               TEXT NOT NULL,
    pair                TEXT NOT NULL,
    signal_type         TEXT NOT NULL,
    size_usd            TEXT NOT NULL,
    entry_order_id      TEXT,                            -- Binance orderId
    entry_fill_price    TEXT,
    entry_fill_qty      TEXT,
    mid_at_entry        TEXT,
    entry_slippage_bps  INTEGER,
    status              TEXT NOT NULL CHECK (status IN (
        'open','closed_tp','closed_sl','closed_duration','closed_via_reconciliation',
        'rejected','needs_manual_review'
    )),
    reject_reason       TEXT CHECK (reject_reason IS NULL OR reject_reason IN (
        'no_venue','insufficient_depth','slippage_exceeds_cap','insufficient_balance',
        'daily_cap_hit','kill_switch','exposure_cap','override_disabled',
        'venue_unavailable'
    )),
    exit_order_id       TEXT,
    exit_fill_price     TEXT,
    realized_pnl_usd    TEXT,
    realized_pnl_pct    TEXT,
    kill_event_id       INTEGER REFERENCES kill_events(id),
    created_at          TEXT NOT NULL,
    closed_at           TEXT
);

-- Kill event audit log. Append-only. One row per kill event.
CREATE TABLE kill_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    triggered_at    TEXT NOT NULL,
    triggered_by    TEXT NOT NULL CHECK (triggered_by IN ('daily_loss_cap','manual','ops_maintenance')),
    reason          TEXT,
    killed_until    TEXT NOT NULL,
    cleared_at      TEXT,
    cleared_by      TEXT CHECK (cleared_by IS NULL OR cleared_by IN ('manual','auto_expired'))
);
CREATE INDEX idx_kill_events_active ON kill_events(cleared_at) WHERE cleared_at IS NULL;

-- Control row pointer. Single-row pattern (id=1 always exists after migration).
CREATE TABLE live_control (
    id                          INTEGER PRIMARY KEY CHECK (id = 1),
    active_kill_event_id        INTEGER REFERENCES kill_events(id)
);
INSERT INTO live_control (id, active_kill_event_id) VALUES (1, NULL);

-- Manual venue overrides (operator-controlled, resolver falls back here when Binance exchangeInfo missing)
CREATE TABLE venue_overrides (
    symbol          TEXT PRIMARY KEY,                    -- "WBTC"
    venue           TEXT NOT NULL,                       -- "binance"
    pair            TEXT NOT NULL,                       -- "WBTCUSDT"
    note            TEXT,                                -- operator comment, shown in alerts
    disabled        INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0,1)),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Resolver cache (persistent; survives restart)
CREATE TABLE resolver_cache (
    symbol          TEXT PRIMARY KEY,
    outcome         TEXT NOT NULL CHECK (outcome IN ('positive','negative')),
    venue           TEXT,                                -- null when outcome='negative'
    pair            TEXT,
    resolved_at     TEXT NOT NULL,
    expires_at      TEXT NOT NULL                        -- 1h for positive, 60s for negative
);

-- Daily metric counters (UPSERT pattern, no new ingestion pipeline)
CREATE TABLE live_metrics_daily (
    date    TEXT NOT NULL,                               -- UTC YYYY-MM-DD
    metric  TEXT NOT NULL,
    value   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (date, metric)
);
```

### 3.2 Constraints worth calling out

- `paper_trades` becomes **append-only** by contract (enforced by `ON DELETE RESTRICT` on the three FKs pointing to it). Document this in `scout/db.py` migration header.
- `active_kill_event_id` is a weak pointer for fast lookup; `kill_events` is the source of truth for audit. Any query that needs "is a kill active right now?" checks `live_control.active_kill_event_id IS NOT NULL`; any query that needs history pivots on `kill_events`.
- `SQLite foreign_keys=ON` pragma MUST be applied on every connection — default is off. Wire in `scout/db.py::connect()` so every callsite inherits. Without it, `ON DELETE RESTRICT` is silently a no-op.

---

## 4. Config surface

All knobs in `scout/config.py` as Pydantic `BaseSettings` fields. Nothing reads `os.getenv` directly.

```python
LIVE_MODE: Literal["paper", "shadow", "live"] = "paper"

# Sizing (M1 from Section 4 review — JSON-like CSV map, not per-signal fields)
LIVE_TRADE_AMOUNT_USD: Decimal = Decimal("100")
LIVE_SIGNAL_SIZES: str = ""                      # e.g. "first_signal=50,gainers_early=75"

# Exit rules (default to PAPER_* when unset)
LIVE_TP_PCT: Decimal | None = None
LIVE_SL_PCT: Decimal | None = None
LIVE_MAX_DURATION_HOURS: int | None = None

# Execution quality
LIVE_SLIPPAGE_BPS_CAP: int = 50
LIVE_DEPTH_HEALTH_MULTIPLIER: Decimal = Decimal("3")
LIVE_VENUE_PREFERENCE: str = "binance"           # CSV in v2; v1 is Binance-only

# Risk gates
LIVE_DAILY_LOSS_CAP_USD: Decimal = Decimal("50")
LIVE_MAX_EXPOSURE_USD: Decimal = Decimal("500")
LIVE_MAX_OPEN_POSITIONS: int = 5

# Signal allowlist — CSV, lowercased, trimmed
LIVE_SIGNAL_ALLOWLIST: str = ""                  # empty = no signals eligible

# Credentials (live mode only; never in .env.example — see §4.4)
BINANCE_API_KEY: SecretStr | None = None
BINANCE_API_SECRET: SecretStr | None = None

model_config = ConfigDict(extra="forbid")        # see F1 pre-flight below
```

### 4.1 Computed fields

```python
@computed_field
@property
def live_signal_allowlist_set(self) -> frozenset[str]:
    if not self.LIVE_SIGNAL_ALLOWLIST:
        return frozenset()
    return frozenset(
        s.strip().lower()
        for s in self.LIVE_SIGNAL_ALLOWLIST.split(",")
        if s.strip()
    )

@computed_field
@property
def live_signal_sizes_map(self) -> dict[str, Decimal]:
    if not self.LIVE_SIGNAL_SIZES:
        return {}
    out: dict[str, Decimal] = {}
    for pair in self.LIVE_SIGNAL_SIZES.split(","):
        pair = pair.strip()
        if not pair:
            continue
        k, _, v = pair.partition("=")
        k = k.strip().lower()
        if not k or not v.strip():
            raise ValueError(f"LIVE_SIGNAL_SIZES malformed entry: {pair!r}")
        out[k] = Decimal(v.strip())
    return out
```

### 4.2 `LiveConfig` resolver

Single object owns fallback logic. Every consumer goes through it:

```python
class LiveConfig:
    def __init__(self, settings: Settings):
        self._s = settings

    @property
    def mode(self) -> Literal["paper", "shadow", "live"]:
        return self._s.LIVE_MODE

    def is_signal_enabled(self, signal_type: str) -> bool:
        return signal_type.lower() in self._s.live_signal_allowlist_set

    def resolve_size_usd(self, signal_type: str) -> Decimal:
        return self._s.live_signal_sizes_map.get(
            signal_type.lower(),
            self._s.LIVE_TRADE_AMOUNT_USD,
        )

    def resolve_tp_pct(self) -> Decimal:
        return self._s.LIVE_TP_PCT if self._s.LIVE_TP_PCT is not None else self._s.PAPER_TP_PCT

    def resolve_sl_pct(self) -> Decimal:
        return self._s.LIVE_SL_PCT if self._s.LIVE_SL_PCT is not None else self._s.PAPER_SL_PCT

    def resolve_max_duration_hours(self) -> int:
        return self._s.LIVE_MAX_DURATION_HOURS or self._s.PAPER_MAX_DURATION_HOURS
```

### 4.3 Wiring into `scout/main.py`

```python
live_config = LiveConfig(settings)
live_engine: LiveEngine | None = None

if live_config.mode in ("shadow", "live"):
    # Startup guardrails (T1 — fail-loud at bootstrap)
    if live_config.mode == "live":
        if not settings.BINANCE_API_KEY or not settings.BINANCE_API_SECRET:
            raise ConfigError("LIVE_MODE=live requires BINANCE_API_KEY/SECRET")
        raise NotImplementedError(
            "balance gate not wired for live mode — cannot start live trading "
            "until scout/live/balance_gate.py is implemented"
        )

    venue_resolver = VenueResolver(
        binance_adapter=BinanceSpotAdapter(settings),
        override_store=OverrideStore(db),
        positive_ttl=timedelta(hours=1),
        negative_ttl=timedelta(seconds=60),
    )
    live_engine = LiveEngine(
        config=live_config, resolver=venue_resolver,
        db=db, kill_switch=KillSwitch(db),
    )

    # Scheduler idiom follows briefing_loop pattern — no APScheduler; hand-rolled async loops
    tasks.append(asyncio.create_task(
        shadow_evaluator_loop(live_engine, db, settings)
    ))
    tasks.append(asyncio.create_task(
        override_staleness_loop(venue_resolver, settings)     # daily at UTC 12:00
    ))
    tasks.append(asyncio.create_task(
        live_metrics_rollup_loop(db, settings)                # daily at UTC 00:30
    ))

# PaperTrader receives live_engine via constructor injection
paper_trader = PaperTrader(db=db, settings=settings, live_engine=live_engine)
```

`LIVE_MODE=paper` leaves the paper path untouched. Zero runtime cost, zero risk.

### 4.4 `.env.example` additions

```ini
# --- Live Trading (BL-055) --- default: paper (unchanged)
# LIVE_MODE=paper                          # paper | shadow | live
# LIVE_TRADE_AMOUNT_USD=100
# LIVE_SIGNAL_SIZES=                       # e.g. first_signal=50,gainers_early=75
# LIVE_TP_PCT=                             # blank = inherit PAPER_TP_PCT
# LIVE_SL_PCT=
# LIVE_MAX_DURATION_HOURS=
# LIVE_SLIPPAGE_BPS_CAP=50
# LIVE_DEPTH_HEALTH_MULTIPLIER=3
# LIVE_DAILY_LOSS_CAP_USD=50
# LIVE_MAX_EXPOSURE_USD=500
# LIVE_MAX_OPEN_POSITIONS=5
# LIVE_SIGNAL_ALLOWLIST=                   # e.g. first_signal,gainers_early
```

**Credentials intentionally NOT in `.env.example`.** API keys are documented in `docs/live-mode-setup.md` with a "DO NOT COMMIT" header warning operators away from pasting secrets into files that get committed.

### 4.5 F1 pre-flight — `extra="forbid"` rollout

Before merging the PR that sets `extra="forbid"` on `Settings`, run this one-liner on every VPS:

```bash
uv run python -c "from scout.config import Settings; Settings(); print('ok')"
```

If any VPS explodes, fix typos first. This is non-negotiable — `extra="forbid"` is what makes the `LIVE_SIGNAL_SIZES` map parse safe (typos caught at startup, not at runtime when a signal fires).

---

## 5. Pre-trade gates

Run in this order. First failure short-circuits and writes a `rejected` row with `reject_reason`. All gates are pure functions on their inputs:

1. **Kill switch** — `KillSwitch.is_active()` → reject with `kill_switch` if non-null
2. **Signal allowlist** — `LiveConfig.is_signal_enabled(signal_type)` → log `live_handoff_skipped`, no DB row (this is not a rejection, it's a no-op)
3. **Venue resolution** — `VenueResolver.resolve(symbol)` → reject with `no_venue` if None
4. **Override disabled** — if resolver returns a row with `disabled=1` → reject with `override_disabled`
5. **Depth health** — `/depth?limit=100` (weight=5). Top 10 bid+ask must have qty × price ≥ `DEPTH_HEALTH_MULTIPLIER × size_usd`. Else reject with `insufficient_depth`.
6. **Slippage projection** — walk the ask side for `size_usd`, compute walked VWAP. If `(vwap - mid) / mid × 10000 > SLIPPAGE_BPS_CAP` → reject with `slippage_exceeds_cap`.
7. **Exposure cap** — `SELECT SUM(size_usd) FROM shadow_trades WHERE status='open'`. If `+ size_usd > MAX_EXPOSURE_USD` → reject with `exposure_cap`. Also check `COUNT(*) >= MAX_OPEN_POSITIONS` → reject with `exposure_cap`.
8. **Daily cap pre-check** — if `active_kill_event_id IS NOT NULL` already, this was caught at step 1. This step is informational only — the real daily-cap enforcement happens at close time (§6).
9. **Balance check** — live mode only. `NotImplementedError` in BL-055. Reject with `insufficient_balance` when wired in BL-058.

A gate failure writes exactly one `shadow_trades` (or `live_trades`) row with `status='rejected'` and a metric increment.

---

## 6. Kill switch and daily loss cap

### 6.1 Kill switch lifecycle

```python
class KillSwitch:
    async def is_active(self) -> KillState | None:
        # Returns None if clear, else KillState(kill_event_id, killed_until, reason)
    async def trigger(self, *, triggered_by: str, reason: str, duration: timedelta) -> int:
        # Inserts kill_events row, updates live_control.active_kill_event_id, returns id
    async def clear(self, *, cleared_by: Literal["manual", "auto_expired"]) -> None:
    async def auto_clear_if_expired(self) -> bool:
        # Called at start of each eval loop pass
```

Three triggers: `daily_loss_cap` (automatic), `manual` (CLI: `uv run python -m scout.live.cli_kill --on "reason"`), `ops_maintenance` (CLI with explicit duration).

Active kill is checked inside `LiveEngine.on_paper_trade_opened()` as gate #1 — no resolver call, no gate cascade. Also checked at the top of the shadow evaluator loop before each exit-side call.

### 6.2 Daily loss cap — transactional computation (M2 from §5 review)

Two close-at-once trades could both see the pre-both-closed SUM and neither cross the cap. Fix:

```python
async with db.transaction():
    await db.execute(
        "UPDATE shadow_trades SET status=?, realized_pnl_usd=?, closed_at=? WHERE id=?",
        new_status, realized_pnl_usd, now_iso, trade_id,
    )
    daily_sum = await db.fetchval(
        "SELECT COALESCE(SUM(CAST(realized_pnl_usd AS REAL)), 0) "
        "FROM shadow_trades "
        "WHERE status LIKE 'closed_%' "
        "  AND date(closed_at) = date('now','utc')"
    )
# Trigger OUTSIDE the transaction — kill-switch writes must not roll back if trigger errors
if daily_sum <= -float(settings.LIVE_DAILY_LOSS_CAP_USD):
    already_active = await kill_switch.is_active()
    if already_active is None:
        await kill_switch.trigger(
            triggered_by="daily_loss_cap",
            reason=f"daily_sum={daily_sum:.2f} cap=-{settings.LIVE_DAILY_LOSS_CAP_USD}",
            duration=compute_kill_duration(datetime.now(timezone.utc)),
        )
```

Concurrent closes converge: the second close's transactional SUM sees the first close committed. Idempotence guard (`is_active()` check before `trigger()`) prevents double-triggering.

### 6.3 Kill duration — G2 math

```python
def compute_kill_duration(triggered_at: datetime) -> timedelta:
    """Return how long to hold the kill from triggered_at.
    Kills last until UTC midnight OR 4 hours from trigger — whichever is LATER.
    Prevents 23:55 UTC kills from clearing 5 minutes later."""
    next_midnight = (triggered_at + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    min_hold_until = triggered_at + timedelta(hours=4)
    killed_until = max(next_midnight, min_hold_until)
    return killed_until - triggered_at
```

---

## 7. Venue resolver

### 7.1 Resolution order

```python
class VenueResolver:
    async def resolve(self, symbol: str) -> ResolvedVenue | None:
        """symbol is the BASE asset (e.g. 'WBTC'), NOT the coin_id slug."""
        # 1. Cache hit (positive or negative within TTL) → return cached outcome
        # 2. Override lookup: venue_overrides table → if found and disabled=0, return it
        # 3. Binance /api/v3/exchangeInfo → find symbol with quote=USDT, status=TRADING
        # 4. Miss → write negative cache (60s), return None
```

**Single-flight lock per symbol.** Two concurrent `resolve("WBTC")` calls during a cache miss must issue one Binance request. `asyncio.Lock` per symbol prevents thundering-herd on boot.

### 7.2 TTLs

- Positive outcomes: 1 hour (listings change intraday occasionally; 6h was too long)
- Negative outcomes: 60 seconds (a coin gets listed mid-day? we want to find it within a minute)

### 7.3 Staleness sweep (T3 from §3 review, G1 from §5)

Daily at UTC 12:00: walk every `venue_overrides` row, call `BinanceSpotAdapter.fetch_exchange_info_row(pair)`. If 404/halted/delisted → WARN alert (one telegram message with a batched list). Simpler than tracking `last_404_at` — fewer than 100 rows forever, once-daily probe is cheap.

### 7.4 Operator override CLI

BL-055 ships `venue_overrides` as a DB table with direct SQL CRUD. Proper CLI (`scout.live.overrides`) is a nice-to-have for later — operators can use sqlite3 directly in v1.

---

## 8. Orderbook walker

### 8.1 Entry-side simulation (shadow + live)

```python
def walk_asks(depth: Depth, size_usd: Decimal) -> WalkResult:
    """Walk asks accumulating notional until >= size_usd. Return weighted fill price."""
    remaining = size_usd
    filled_notional = Decimal(0)
    filled_qty = Decimal(0)
    for level in depth.asks:       # already sorted ascending
        level_notional = level.price * level.qty
        take = min(level_notional, remaining)
        take_qty = take / level.price
        filled_notional += take
        filled_qty += take_qty
        remaining -= take
        if remaining <= 0:
            break
    if remaining > 0:
        return WalkResult(insufficient_liquidity=True, ...)
    vwap = filled_notional / filled_qty
    return WalkResult(vwap=vwap, qty=filled_qty, slippage_bps=(vwap - depth.mid) / depth.mid * 10000)
```

### 8.2 Exit-side simulation (T2 from §3 review)

On close, fetch one fresh `/depth` (bid side) and walk it. `entry_walked_vwap` and `exit_walked_vwap` both come from real orderbook state — not a uniform slippage constant. Cost: +1 `/depth` call per close (weight=5).

`realized_pnl_pct` is locked in post-slippage — both legs. No mid-price fantasy P&L anywhere in the ledger.

---

## 9. Rate limiting

Binance enforces per-minute **weight** limits (1200/min on spot). Token-count limiting is wrong — `/depth?limit=100` is 5 weight, not 1.

### 9.1 Header-based governor

Every request reads `X-MBX-USED-WEIGHT-1M` from the response. Update a shared counter. Two thresholds:

- **960 (80%)** → shrink the adapter's semaphore from N=10 to N=3. Throttle until a subsequent response shows weight back under 600.
- **1140 (95%)** → pause all outgoing requests for 10 seconds, log WARN, increment `binance_rate_limit_hits` metric.

On `429 Too Many Requests` or `418 IP Banned`: respect `Retry-After` header, pause entire adapter until it elapses, CRITICAL alert.

### 9.2 Budget allocation

Per minute, assuming shadow mode with ~50 handoffs/hour:

- `/exchangeInfo` — 1/hour = 20/min weight-budget (one full fetch; cached)
- `/depth?limit=100` — 1 entry-side × ~1 handoff/min + 1 exit-side × ~0.5 closes/min ≈ 7.5 weight/min
- `/ticker/price` — continuous for eval loop (weight=1 each); 1/min/open-position, capped at 5 open = 5 weight/min
- **Total typical**: ~35 weight/min. Headroom: 97%.

Rate limit is not the bottleneck; header tracking is defensive, not optimistic.

---

## 10. Error handling and observability

### 10.1 Error taxonomy

| Category | Examples | Handling |
|---|---|---|
| **Transient venue** | 5xx, ReadTimeout, connection reset | Shadow: 3× backoff (1s, 2s, 4s). Live: 1× retry (T4 flip-to-live refinement). Exhaust → `rejected` with `reject_reason='venue_unavailable'` + WARN alert |
| **Rate limit** | 429, weight ≥ 1140 | Respect `Retry-After` or sleep 10s; no immediate retry; metric `binance_rate_limit_hits` |
| **Resolver negative** | not listed, halted | Cache negative 60s, row `rejected` with `no_venue`. NO alert (normal path) |
| **Safety gate** | slippage/depth/exposure/cooldown/kill | Row `rejected` with specific reason. INFO not WARN. Metric counter per reason |
| **Config / bootstrap** | missing keys, `NotImplementedError`, `extra="forbid"` typo | Raise at startup, process exits. Systemd restarts, re-fails loudly. No graceful degradation |

Any exception outside these five is a bug: ERROR with traceback, row left `open` with `review_retries` incremented and `next_review_at = now + 24h`.

### 10.2 Structured log events (stable names)

- `live_handoff_started` — chokepoint fires, `{paper_trade_id, signal_type, mode}`
- `live_handoff_skipped` — signal not in allowlist, `{paper_trade_id, signal_type, reason}`
- `live_handoff_skipped_killed` — active kill, `{kill_event_id}`
- `live_resolver_cache_hit` / `live_resolver_cache_miss` — `{symbol, outcome, ttl_remaining_sec}`
- `live_resolver_resolved` — `{symbol, venue, pair, source: "binance_exchangeinfo" | "override_table"}`
- `live_resolver_negative` — `{symbol, reason}`
- `live_pretrade_gate_failed` — `{paper_trade_id, gate, detail}`
- `live_shadow_order_opened` — `{shadow_trade_id, walked_vwap, mid, slippage_bps, size_usd}` (F1: renamed from `_logged` to match DB status)
- `live_order_sent` / `live_order_filled` / `live_order_rejected` — live mode only
- `live_kill_event_triggered` — `{kill_event_id, trigger, killed_until}`
- `live_kill_event_cleared` — `{kill_event_id, cleared_by}`
- `live_override_stale_detected` — `{symbol, override_pair, probe_result}` (daily sweep)
- `live_boot_reconciliation_done` — `{rows_inspected, rows_closed, rows_resumed}` (ALWAYS fires, even if rows_inspected=0 per T3)
- `live_boot_reconciliation_drift_window` — `{restart_at, earliest_open_created_at}` (G1 — excludes reconciliation window from post-hoc perf analysis)

### 10.3 Metrics (8 aligned to reject_reason enum, per T1)

Stored in `live_metrics_daily`:
- `shadow_orders_opened` (happy-path counter)
- `shadow_rejects_no_venue`
- `shadow_rejects_insufficient_depth`
- `shadow_rejects_slippage_exceeds_cap`
- `shadow_rejects_insufficient_balance` (live only — stays zero in shadow mode)
- `shadow_rejects_daily_cap_hit`
- `shadow_rejects_kill_switch`
- `shadow_rejects_exposure_cap`
- `shadow_rejects_override_disabled`
- `shadow_rejects_venue_unavailable`
- `resolver_cache_hits` / `resolver_cache_misses` (T2 — separate counters, dashboard computes ratio)
- `binance_rate_limit_hits`

Adding a new `reject_reason` enum value automatically implies a new `shadow_rejects_<reason>` metric name with no code change.

### 10.4 Alerts

| Priority | Trigger | Dedup | Example |
|---|---|---|---|
| **CRITICAL** | kill triggered, startup ConfigError/NotImplementedError, 5+ consecutive 5xx | No dedup | `🚨 live_kill_event: daily_loss_cap breached (-$51.23 / -$50 cap). Halted until 2026-04-23 00:00 UTC.` |
| **WARN** | stale override, review-retry exhaustion (3×24h), sustained rate-limit pressure | 1/day per (pair, reason) | `⚠ override stale: WBTC → WBTCUSDT not found on Binance (404). Update venue_overrides or remove row.` |
| **INFO** | daily shadow roll-up | 1/day at UTC 00:30 | `📊 shadow 24h: 47 handoffs, 31 opened, 12 skipped_not_listed, 4 skipped_depth. Hypothetical P&L: +$23.40 (4.6%).` |

**Sunday staggering (G3):** Daily roll-up runs at UTC 00:30 (not 00:15) to give the weekly digest breathing room. On Sundays the daily roll-up appends "(weekly digest to follow)" to signal complementarity.

### 10.5 Restart reconciliation

On process boot, before any handoffs:
1. Query open shadow rows, log `live_boot_reconciliation_drift_window` with restart timestamp.
2. For each: fetch Binance mid-price, evaluate TP/SL/duration. Cross → close as `status='closed_via_reconciliation'` + WARN. Else leave `open`.
3. Always log `live_boot_reconciliation_done` — even with `rows_inspected=0` (T3). Absence of log ≠ success; always-on fires prove the engine came up clean.

Live mode additionally calls `GET /openOrders` — any divergence vs DB triggers CRITICAL and refuses to start until manual clearance.

### 10.6 Config reload semantics (M1 from §5 review)

`LiveConfig` reads Pydantic `Settings` at instantiation. Settings are frozen at first-instantiation — **editing `.env` requires full process restart**, not `systemctl reload`.

Ship a `--check-config` CLI flag:
```bash
uv run python -m scout.main --check-config
```
Prints the fully-resolved `LiveConfig` (mode, allowlist set, sizing map, TP/SL resolved values). Operator runs this before every live config change, diffs against expectations, then restarts.

Document in `docs/live-mode-setup.md`: "Config changes require `systemctl restart scout`, not reload."

---

## 11. Testing strategy

### 11.1 Test pyramid

| Tier | Target count | Tooling | Purpose |
|---|---|---|---|
| **Unit** | ~120 new | pytest + aioresponses | Per-module behavior; no real I/O |
| **Integration** | ~25 new | pytest + aiosqlite tmp_path + aioresponses | Full shadow loop end-to-end |
| **Smoke / live-mocked** | ~8 new | pytest + recorded fixtures | Flip-to-live checklist exercises |

No live-network tests in CI. The 7-day prod-shadow soak (§11.7) is the empirical validator.

### 11.2 Test file map

```
tests/live/
    test_config.py                # LiveConfig, resolve_size_usd, allowlist parse, extra="forbid"
    test_binance_adapter.py       # exchangeInfo, depth, ticker, X-MBX-USED-WEIGHT-1M header
    test_venue_resolver.py        # single-flight (first!), TTL, override lookup, staleness
    test_kill_switch.py           # trigger/clear/auto_expired, audit trail
    test_pretrade_gates.py        # every reject_reason enum member
    test_shadow_evaluator.py      # TP/SL/duration exits, review_retries, reconciliation
    test_live_engine.py           # on_paper_trade_opened full matrix
    test_metrics.py               # UPSERT counter semantics
    test_reconciliation.py        # boot recovery; 0-row case (T3); drift window log
tests/integration/
    test_live_shadow_loop.py      # 5 canonical flows (§11.6)
```

### 11.3 Test fixture — SQLite pragmas (T3 from §6 review)

```python
@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "test.db"
    conn = await aiosqlite.connect(db_path)
    await conn.execute("PRAGMA journal_mode=WAL")     # match production isolation
    await conn.execute("PRAGMA foreign_keys=ON")      # CHECK + FK RESTRICT enforced
    # apply migrations...
    yield Database(conn)
    await conn.close()
```

Production `scout/db.py::connect()` MUST set identical pragmas on every connection — `foreign_keys=ON` is per-connection in SQLite, off by default.

### 11.4 Gate-rejection parametrize coverage (H2 — split shadow vs live-only)

```python
SHADOW_REJECT_REASONS = [
    ("symbol_404",            "no_venue"),
    ("depth_below_3x",        "insufficient_depth"),
    ("slippage_exceeds_cap",  "slippage_exceeds_cap"),
    ("daily_cap_breached",    "daily_cap_hit"),
    ("kill_active",           "kill_switch"),
    ("exposure_cap_hit",      "exposure_cap"),
    ("override_row_disabled", "override_disabled"),
    ("venue_5xx_exhaust",     "venue_unavailable"),
]
LIVE_ONLY_REJECT_REASONS = [
    ("balance_too_low",       "insufficient_balance"),
]

CHECK_CONSTRAINT_VALUES = {
    "no_venue","insufficient_depth","slippage_exceeds_cap","insufficient_balance",
    "daily_cap_hit","kill_switch","exposure_cap","override_disabled","venue_unavailable",
}

def test_param_lists_cover_check_constraint():
    """Meta-test: adding a reject_reason enum value forces a test entry."""
    shadow = {r for _, r in SHADOW_REJECT_REASONS}
    live = {r for _, r in LIVE_ONLY_REJECT_REASONS}
    assert shadow | live == CHECK_CONSTRAINT_VALUES, (
        f"reject_reason enum drift: "
        f"missing in tests={CHECK_CONSTRAINT_VALUES - (shadow | live)}, "
        f"extra in tests={(shadow | live) - CHECK_CONSTRAINT_VALUES}"
    )
```

### 11.5 High-priority unit tests (land first)

- **Single-flight resolver** (T2 — first in `test_venue_resolver.py`, with N=10 concurrent variant): `asyncio.gather` of 10 `resolve("WBTC")` during cache miss must issue one Binance request.
- **Transactional daily-cap race** (M2 test): two concurrent closes at -$30 and -$25 → exactly one kill event, regardless of close order. Also: A+B=-$49.99 no-trigger, A+B=-$50.01 trigger.
- **Kill-duration math** (G2): 4 parametrized cases validating `compute_kill_duration` vs UTC-midnight-vs-4h-minimum.
- **TTL split**: positive 1h boundary, negative 60s boundary using `freezegun`.
- **Gate parametrize** (H2): 8 shadow cases + 1 live-only, reject row + metric increment verified.

### 11.6 Integration tests — five canonical flows

1. **Happy path** — paper opens → resolve hits → gates pass → `shadow_orders_opened` metric++ → TP hit → `status='closed_tp'` with realized_pnl_usd/pct populated.
2. **Not listed** — coin absent from Binance → `rejected` with `no_venue`, metric++.
3. **Depth starved** — thin book → `rejected` with `insufficient_depth`.
4. **Venue transient failure at handoff** (M1 — renamed from old Flow #4): resolver raises `aiohttp.ClientError` 3× → Category 1 backoff exhausts → `rejected` with `venue_unavailable` + WARN alert. `review_retries` NOT incremented — this is a handoff-time transient, not a mid-life halt.
5. **Restart mid-shadow**: open a shadow row, stop evaluator task, restart → `live_boot_reconciliation_done` fires with `rows_inspected=1, rows_resumed=1`. **T3 variant**: stop+restart with zero open rows → event still fires with `rows_inspected=0`.
6. **Mid-life halt review-retry** (M1 new flow): shadow position in `status='open'`, Binance halts the pair mid-life → evaluator catches the halt response → `status='needs_manual_review'`, `review_retries=0`, `next_review_at=now+24h`. Mock clock +24h → retry succeeds → back to `open`. Third failed retry → `rejected` + alert.

### 11.7 Shadow soak — 7 days in production (H1)

**Calendar duration is the point.** 72h misses:
- Sunday-only edge cases (lower volume, thinner books, different depth-health hit rates)
- Weekly digest interaction
- Weekend-to-weekday traffic curve

Soak length is calendar-driven, not correctness-driven. Shortening risks missing weekly seasonality AND the operator-training baseline ("what does a normal UTC 00:30 shadow roll-up look like?").

**Conservation invariant (M2 from §6 review):**

Invariant: every `live_handoff_started` must end in one of four terminal states: skipped (not-allowlisted or killed), opened shadow row, rejected with a `reject_reason`, or stuck in `needs_manual_review` at week-end.

```
live_handoff_started_count
  = live_handoff_skipped_count
  + live_handoff_skipped_killed_count
  + SUM(shadow_rejects_<reason>)   over the soak window, all reasons
  + shadow_orders_opened           over the soak window
  + COUNT(shadow_trades WHERE status='needs_manual_review' AND created_at >= soak_start)
```

Handoff counts come from structlog syslog (`journalctl -u gecko-pipeline | grep live_handoff_`). The aggregate `shadow_*` counts come from `SELECT SUM(value) FROM live_metrics_daily WHERE date BETWEEN soak_start AND soak_end AND metric = ?`. Residual from `needs_manual_review` comes from `shadow_trades` directly.

Exact shell + SQL script finalized during implementation, committed to `scripts/soak_report.sh`. Success criterion: delta = 0.

**Success criteria (all seven days):**
- Zero uncaught exceptions from `scout/live/*` (ERROR-level structlog count = 0)
- `live_boot_reconciliation_done` fires on every restart, `rows_resumed >= rows_inspected`
- Conservation invariant above returns delta = 0
- `resolver_cache_hits / (hits + misses) >= 0.80`
- No CRITICAL alerts from the live package (only INFO roll-ups)
- Median walked-VWAP slippage ≤ 25bps across logged shadow orders

**Deliverable at week-end**: one-page memory entry documenting hypothetical P&L, reject breakdown, cache ratio, and any anomalies.

### 11.8 Flip-to-live checklist (gated on §11.7 passing)

Ordering matters — config-verify BEFORE generating credentials (F1):

- [ ] §11.7 soak-test week completed with all success criteria met
- [ ] Balance gate (`scout/live/balance_gate.py`) implemented + 100% unit coverage
- [ ] Live-mode retry policy reduced from 3×backoff to 1× retry (T4)
- [ ] Exit-side `/depth` call path exercised with ≥ 50 closed shadow trades in soak
- [ ] `docs/live-mode-setup.md` reviewed
- [ ] **`uv run python -m scout.main --check-config` executed on VPS, every `LIVE_*` resolved value confirmed** (BEFORE credentials exist)
- [ ] Binance API keys generated: spot-only scope, no withdrawal, no margin, no futures; IP-restricted to VPS IP
- [ ] Manual kill-switch CLI exercised: `uv run python -m scout.live.cli_kill --on "flip test"` → shadow rejects → `--off` → resumes
- [ ] `LIVE_TRADE_AMOUNT_USD=10` on first live day (raised only after 48h clean)
- [ ] `LIVE_SIGNAL_ALLOWLIST` starts with **one** signal type (highest-performing from `combo_performance`). Others enabled one-at-a-time with 48h hold between

### 11.9 Regression protection — literal CI gate (T4)

```bash
# .github/workflows/tests.yml (or equivalent) — runs after pytest
BASELINE_COUNT=1038
ACTUAL_COUNT=$(uv run pytest --collect-only -q | tail -1 | awk '{print $1}')
test "$ACTUAL_COUNT" -ge "$BASELINE_COUNT" || {
    echo "ERROR: test count dropped from $BASELINE_COUNT to $ACTUAL_COUNT"
    exit 1
}
```

Baseline bumps only via deliberate PR to CI config. Closes the "accidentally deleted a paper-path test" gap with zero human discipline required.

BL-055 PRs add tests ONLY under `tests/live/` and `tests/integration/live_*.py`. Existing test files under `tests/test_trading_signals.py`, `tests/test_combo_*.py`, `tests/test_narrative_*.py`, etc. are frozen during BL-055.

---

## 12. Migrations and rollout

### 12.1 Single migration

`scout/db.py` migration adds all §3.1 tables in one atomic `CREATE TABLE` block. No backfill — shadow_trades / live_trades / kill_events start empty. `live_control (id=1)` row is the only seed.

### 12.2 Rollout order

1. **PR A** — schema + `scout/live/config.py` + `LiveConfig` + pragmas in `scout/db.py`. No behavior change; `LIVE_MODE=paper` path is untouched. `extra="forbid"` F1 pre-flight on every production VPS (currently just 89.167.116.187) before merge.
2. **PR B** — `scout/live/binance_adapter.py` + `scout/live/resolver.py` + unit tests. Still no behavior change — nothing imports them yet.
3. **PR C** — `kill_switch.py` + `gates.py` + `metrics.py`. Still no behavior change.
4. **PR D** — `engine.py` + `shadow_evaluator.py` + `reconciliation.py` + `cli_kill.py`. Still no behavior change (LIVE_MODE stays paper).
5. **PR E** — wire into `scout/main.py`. Flip VPS `.env` to `LIVE_MODE=shadow`. Start the 7-day soak clock.

Each PR is independently reviewable and revertable. Schema in PR A is forward-compatible — even if PR B-E are abandoned, paper mode continues unaffected.

### 12.3 What's explicitly deferred

- BL-056: additional CEX adapters (Bybit, Kraken, MEXC, Kucoin, Coinbase)
- BL-057: on-chain (ETH/BASE/Solana) wallet spot via aggregators
- BL-058: `combo_performance`-gated auto-enable/auto-demote of live signals
- Partial TP splits, trailing stops, OCO orders
- Multi-venue routing (v1 is Binance-only)

---

## 13. Open implementation tickets (carried to the plan)

- **I1** — `scout/db.py::connect()` sets `PRAGMA foreign_keys=ON` and `PRAGMA journal_mode=WAL` on every connection (§3.2, §11.3). Confirm no existing migration depends on foreign_keys being off.
- **I2** — `F1 pre-flight`: before merging PR A, run `uv run python -c "from scout.config import Settings; Settings()"` on every production VPS (currently 89.167.116.187). Fix any typos before flipping `extra="forbid"` (§4.5).
- **I3** — `docs/live-mode-setup.md` new file: DO NOT COMMIT header, API key creation steps, IP-allowlist, `--check-config` usage, "restart not reload" semantics (§4.4, §10.6).
- **I4** — Systemd unit: `gecko-pipeline.service` already exists; confirm `Restart=on-failure` policy so startup `NotImplementedError` in live mode produces a visible loop, not silent crash.
- **I5** — Weekly soak summary template: `docs/superpowers/specs/2026-04-XX-bl055-soak-report.md` posted at week-end; includes conservation-invariant query output.

---

## 14. References

- Backlog entry: `docs/superpowers/backlog.md#BL-055`
- Existing paper-trading engine: `scout/trading/engine.py` (chokepoint lives here)
- Binance spot API docs: https://binance-docs.github.io/apidocs/spot/en/
- Related prior specs:
  - `2026-04-19-paper-trading-engine-design.md` — paper-path foundation
  - `2026-04-18-paper-trading-feedback-loop-design.md` — `combo_performance` table used by BL-058

---

**End of spec.** Implementation plan follows via `superpowers:writing-plans`.
