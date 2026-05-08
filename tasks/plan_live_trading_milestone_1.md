**New primitives introduced:** Three new `Settings` fields — `LIVE_TRADING_ENABLED: bool = False` (master kill, Layer 1 of 4-layer kill stack), `LIVE_MAX_OPEN_POSITIONS_PER_TOKEN: int = 1` (per-token concurrency cap — M1-blocker live-position-aggregator guard; distinct from existing `LIVE_MAX_OPEN_POSITIONS: int = 5` which is total-across-venues cap), `LIVE_OVERRIDE_REPLACE_ONLY: bool = False` (PREPEND-vs-REPLACE semantics for OverrideStore). **Drift-check 2026-05-08 / Option A reconciliation:** the original v2.1 plan claimed 5 new fields including `LIVE_MAX_TRADE_NOTIONAL_USD` and `LIVE_MAX_OPEN_EXPOSURE_USD`; build-stage drift-check found existing equivalents in `scout/config.py:335,350` (`LIVE_TRADE_AMOUNT_USD: Decimal = Decimal("100")` per-trade cap; `LIVE_MAX_EXPOSURE_USD: Decimal = Decimal("500")` aggregate cap) shipped under BL-055. Plan now reuses those existing fields by name (Option A); 6 existing call sites — `scout/live/gates.py:216-217`, `scout/main.py:988-989`, `tests/integration/test_live_shadow_loop.py:67-68`, `tests/live/test_config.py:80-81`, `tests/live/test_live_engine.py:67-68,324-325`, `tests/live/test_pretrade_gates.py:66-67,329,351-352` — remain untouched. New `signal_params.live_eligible INTEGER NOT NULL DEFAULT 0` column (migration `bl_live_eligible_v1`, schema_version 20260508 — Layer 3 per-signal opt-in). Five new tables (one combined migration `bl_per_venue_services_v1`, schema_version 20260510): `venue_health` (with `last_quote_mid_price` + `last_depth_at_size_bps` pre-fetched routing inputs + `fills_30d_count` + `is_dormant`), `wallet_snapshots`, `venue_listings`, `venue_rate_state`, `symbol_aliases`. New SQL view `cross_venue_exposure` (M1-blocker — Gate 7 queries this not `shadow_trades`). New SQL view `cross_venue_pnl` scaffold (returns 0s at M1; M2 adds aggregations). `ExchangeAdapter` ABC reshape (M1-included, structural-reviewer MUST-FIX) — split `send_order` → `place_order_request` + `await_fill_confirmation`; generalize `fetch_exchange_info_row` → `fetch_venue_metadata(canonical) → VenueMetadata | None`; `resolve_pair_for_symbol` becomes delegate-able. New scaffold class `CCXTAdapter` (parameterized by venue name; not wired to any venue at M1; M1.5 wires first venue). New module `scout/live/balance_gate.py` (was missing per BL-055 prereq). New idempotency contract on `scout/live/binance_adapter.py`: `client_order_id = f"gecko-{paper_trade_id}-{intent_uuid}"` + pre-retry dedup query (migration `bl_live_client_order_id_v1`, schema_version 20260509). New module `scout/live/routing.py` — routing layer producing ranked candidate list with <200ms p95 budget, live-position-aggregator guard (M1-blocker), on-demand venue_listings fetch (M1-blocker), chain="coingecko" enrichment, OverrideStore PREPEND semantics, delisting fallback. New `scout/live/services/` package — `VenueService` ABC (typed `adapter: ExchangeAdapter` + concurrency contract), service-runner harness, three concrete services (`HealthProbe`, `BalanceSnapshot`, `KillSwitchEnforcer`), one stub (`RateLimitAccountantStub` returns CONSERVATIVE 50% headroom). New `live_orders_skipped_*` metric family (`master_kill`, `mode_paper`, `signal_disabled`, `kill_switch`, `exposure_cap`, `notional_cap`, `token_aggregate`, `no_venue`, `all_candidates_failed`, `dual_signal_aggregate`, `approval_new_venue_gate`, `approval_trade_size_gate`, `approval_venue_health_gate`, `approval_operator_flag`). New telemetry columns on `live_trades` per plan-stage policy reviewer: `fill_slippage_bps REAL` (computed at fill confirmation as `(fill_price/mid_at_entry - 1) * 10000`), `correction_at TEXT`, `correction_reason TEXT`. New `signal_venue_correction_count` table (running counter for approval-removal gate 1, migration `bl_live_trades_telemetry_v1`, schema_version 20260511). New module `scout/live/approval_thresholds.py` with `should_require_approval(db, settings, signal_type, venue, size_usd) → tuple[bool, gate_name | None]` implementing the 4 pre-registered operator-in-loop gates (new-venue <30 fills, trade-size >2× rolling-30 median, venue-health degraded-24h, /approval-required flag). New Telegram startup notification when `LIVE_TRADING_ENABLED=True` (`scout/main.py` startup hook). New Telegram approval gateway with `/allow-stack <token>`, `/auto-approve venue=<name>`, `/approval-required venue=<name>`, `/venue-revive name=<name>` commands. Operator-in-loop scaling rules pre-registered in design doc (no runtime knob). canonical-extraction rule for `symbol_aliases` explicit (CCXT `markets[symbol]` split-on-`/` taking `[0]`; perp suffix `:USDT` stripped). Dormancy daily job sets `venue_health.is_dormant=1` for venues with `fills_30d_count=0`.

# Live Trading Milestone 1 — Multi-Venue Architecture, Binance Wired

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the multi-venue execution architecture (per `tasks/design_live_trading_hybrid.md` v2.1, committed `dad59ed`) as a vertical slice with Binance as the single wired venue. The M1 ship is "ready to soak on Binance via BL-055 with the architectural framework in place" — operator can flip `LIVE_TRADING_ENABLED=True` after answering the 4 design open questions + funding the Binance account.

**Architecture:** v2.1 four-tier adapter pattern (Tier 1 AI-CLI / Tier 2 aggregator / Tier 3a bespoke / Tier 3b CCXT-backed) under shared `ExchangeAdapter` ABC + routing layer + per-venue services framework + cross-venue accounting + operator surfaces. M1 wires Tier 3a (BL-055/Binance) only; Tier 3b CCXTAdapter ships as scaffold; Tier 1/2 adapters deferred to M1.5/M2. The architectural commitment: adding venue #2 (M1.5) is adapter-config + service spin-up, not architectural rework.

**Tech Stack:** Python 3.12, aiosqlite, pydantic v2 BaseSettings + field_validator, pytest-asyncio (auto mode), structlog (PrintLoggerFactory — tests use `structlog.testing.capture_logs`, NOT pytest caplog), black formatting. NEW dependency: `ccxt` Python library (pinned version, scaffold-only at M1; first wired CCXT venue is M1.5).

**Test reference snippets omit `_REQUIRED` for brevity** — `Settings(_env_file=None)` calls in this plan must add `**_REQUIRED` (or use `tests/conftest.py:60` `settings_factory` fixture) to satisfy the 3 mandatory fields (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ANTHROPIC_API_KEY`). Module-level convention from `tests/test_config.py:11`: `_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")`. Apply to all `Settings(...)` constructions in tests created under this plan. Captured during Task 1 build-stage code review (`798cd99`).

**Total scope:** ~80 steps across 16 tasks. Per advisor's sizing this is ~2x the original v1 plan (50 steps), reflecting the architectural framework shipping at M1 vs the v1 single-venue-throwaway shape.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `scout/config.py` | Modify | Add 3 Settings fields + 1 field_validator (Option A: reuses existing `LIVE_TRADE_AMOUNT_USD` + `LIVE_MAX_EXPOSURE_USD`) |
| `scout/db.py` | Modify | Add 3 migrations (bl_live_eligible_v1, bl_live_client_order_id_v1, bl_per_venue_services_v1) + 2 SQL views in `_create_tables` |
| `scout/trading/params.py` | Modify | Add `live_eligible: bool = False` to SignalParams + extend SELECT to row[13] |
| `scout/live/adapter_base.py` | Modify (M1-include reshape) | Split `send_order` → `place_order_request` + `await_fill_confirmation`; generalize to `fetch_venue_metadata`; `resolve_pair_for_symbol` delegate-able. Add `VenueMetadata` dataclass. |
| `scout/live/binance_adapter.py` | Modify | Migrate to new ABC shape; add `client_order_id` generation + dedup; add `fetch_account_balance` |
| `scout/live/ccxt_adapter.py` | **Create** | CCXTAdapter scaffold parameterized by venue name; not wired at M1 |
| `scout/live/balance_gate.py` | **Create** | `check_sufficient_balance(adapter, required_usd, margin_factor)` |
| `scout/live/routing.py` | **Create** | Routing layer; <200ms p95; live-position-aggregator guard; on-demand venue_listings fetch; chain enrichment; OverrideStore PREPEND |
| `scout/live/gates.py` | Modify | Update Gate 7 to query `cross_venue_exposure`; add Gate 8 (notional), Gate 9 (signal opt-in) |
| `scout/live/engine.py` | Modify | Wire master-kill check + per-trade-notional-cap check + per-signal-opt-in + live-position-aggregator at entry |
| `scout/live/services/__init__.py` | **Create** | Package marker |
| `scout/live/services/base.py` | **Create** | `VenueService` ABC + concurrency contract |
| `scout/live/services/runner.py` | **Create** | Service-runner harness |
| `scout/live/services/health_probe.py` | **Create** | HealthProbe worker |
| `scout/live/services/balance_snapshot.py` | **Create** | BalanceSnapshot worker |
| `scout/live/services/rate_limit_stub.py` | **Create** | Stub returning conservative 50% |
| `scout/live/services/dormancy.py` | **Create** | Daily dormancy-flagging job |
| `scout/live/telegram_approval.py` | **Create** | Telegram approval gateway with command handlers |
| `scout/live/symbol_normalize.py` | **Create** | `canonical_from_ccxt_market` + `lookup_canonical(venue, venue_pair)` |
| `scout/main.py` | Modify | Startup hook for `LIVE_TRADING_ENABLED=True` Telegram notification + register service-runner |
| `tests/test_live_master_kill.py` | **Create** | Settings + master kill enforcement + capital caps + per-signal opt-in + live-position-aggregator |
| `tests/test_live_eligible_migration.py` | **Create** | live_eligible migration + dataclass field |
| `tests/test_live_balance_gate.py` | **Create** | balance_gate behavior |
| `tests/test_live_idempotency.py` | **Create** | client_order_id contract (uses inspect-source pattern, NOT runtime — Windows OpenSSL) |
| `tests/test_live_per_venue_services_migration.py` | **Create** | 5 new tables + indexes |
| `tests/test_live_adapter_abc_reshape.py` | **Create** | place_order_request / await_fill_confirmation split; fetch_venue_metadata |
| `tests/test_live_routing.py` | **Create** | Routing layer: candidate list, aggregator guard, on-demand fetch, chain enrichment, override semantics, delisting fallback |
| `tests/test_live_services_framework.py` | **Create** | VenueService ABC + harness + HealthProbe + BalanceSnapshot + dormancy |
| `tests/test_live_ccxt_adapter_scaffold.py` | **Create** | CCXTAdapter constructor + ABC compliance (no venue wired) |
| `tests/test_live_telegram_approval.py` | **Create** | Approval gateway commands; uses `structlog.testing.capture_logs` |
| `tests/test_live_symbol_normalize.py` | **Create** | CCXT canonical extraction; Tier 1/2 custom |

**Schema versions reserved:** 20260508 (bl_live_eligible_v1), 20260509 (bl_live_client_order_id_v1), 20260510 (bl_per_venue_services_v1), 20260511 (bl_live_trades_telemetry_v1 — added per plan-stage policy reviewer).

---

## Task 0: Setup — branch + prerequisite verification

- [ ] **Step 1: Create feature branch**

```bash
git checkout master
git pull
git checkout -b feat/live-trading-m1-multi-venue
```

- [ ] **Step 2: Verify prerequisite state**

```bash
ls scout/live/balance_gate.py 2>&1                                  # No such file (created in Task 8)
ls scout/live/routing.py 2>&1                                       # No such file (created in Task 9)
ls scout/live/services/ 2>&1                                        # No such directory (created in Task 11+)
grep -n "live_eligible\|LIVE_MAX_OPEN_POSITIONS" scout/config.py    # empty
grep -n "client_order_id" scout/live/binance_adapter.py             # empty
```

- [ ] **Step 3: Verify ccxt is installable** (do NOT install yet; just verify in pyproject.toml or available)

```bash
uv add --dry-run ccxt
```

If errors, that's a Task 5 prereq — flag and continue.

---

## Task 1: Settings — 3 new fields + validator (Option A reconciliation)

**Files:**
- Modify: `scout/config.py` (after existing `LIVE_SIGNAL_ALLOWLIST` ~line 354)
- Test: `tests/test_live_master_kill.py` (NEW)

**Drift-check note:** Build-stage check on 2026-05-08 found `scout/config.py:332-354` already contains BL-055-shipped `LIVE_*` config block (LIVE_MODE, LIVE_TRADE_AMOUNT_USD, LIVE_MAX_EXPOSURE_USD, LIVE_MAX_OPEN_POSITIONS, LIVE_DAILY_LOSS_CAP_USD, etc.). Original v2.1 plan's `LIVE_MAX_TRADE_NOTIONAL_USD` and `LIVE_MAX_OPEN_EXPOSURE_USD` were rename-conflicts with existing `LIVE_TRADE_AMOUNT_USD` (line 335) and `LIVE_MAX_EXPOSURE_USD` (line 350). Per Option A reconciliation: reuse existing names, add only 3 truly new fields. 6 existing call sites untouched. Cap-relation validator omitted (existing code has 5+ months of production use without it; adding is separate hardening proposal).

- [ ] **Step 1: Failing test for defaults**

Create `tests/test_live_master_kill.py`:

```python
"""BL-NEW-LIVE-HYBRID M1: master kill + per-token aggregator + override."""
from __future__ import annotations

import pytest

from scout.config import Settings


class TestLiveTradingSettings:
    def test_master_kill_defaults_off(self):
        assert Settings(_env_file=None).LIVE_TRADING_ENABLED is False

    def test_max_open_positions_per_token_default(self):
        assert Settings(_env_file=None).LIVE_MAX_OPEN_POSITIONS_PER_TOKEN == 1

    def test_override_replace_only_default(self):
        assert Settings(_env_file=None).LIVE_OVERRIDE_REPLACE_ONLY is False


class TestLiveTradingValidators:
    def test_max_open_positions_per_token_must_be_at_least_1(self):
        with pytest.raises(ValueError, match="must be >= 1"):
            Settings(_env_file=None, LIVE_MAX_OPEN_POSITIONS_PER_TOKEN=0)
```

- [ ] **Step 2: Run — expect 4 FAILs**

```bash
uv run pytest tests/test_live_master_kill.py::TestLiveTradingSettings tests/test_live_master_kill.py::TestLiveTradingValidators -v
```

- [ ] **Step 3: Add Settings fields**

In `scout/config.py` after `LIVE_SIGNAL_ALLOWLIST` (~line 354) — i.e. AFTER the existing BL-055 LIVE_* block, NOT replacing it — add:

```python
    # -------- BL-NEW-LIVE-HYBRID M1 (design v2.1, 2026-05-08) --------
    # Layer 1 of 4-layer kill stack. Master kill — when False, all live
    # execution short-circuits at engine entry regardless of LIVE_MODE /
    # per-signal opt-in / kill_switch state. Operator via .env edit + restart.
    # Distinct from LIVE_MODE (paper/shadow/live tri-state, Layer 2).
    LIVE_TRADING_ENABLED: bool = False

    # Per-token concurrency cap. Routing layer's live-position-aggregator
    # guard rejects intents when live_trades.count(canonical_symbol, status='open')
    # >= this value. Default 1 covers BILL dual-signal pattern.
    # Distinct from existing LIVE_MAX_OPEN_POSITIONS (total-across-venues cap, default 5).
    LIVE_MAX_OPEN_POSITIONS_PER_TOKEN: int = 1

    # OverrideStore semantics: False = PREPEND chain's venues to candidate list
    # (graceful fallback if override chain has no healthy venue); True = REPLACE
    # (only override chain's venues; abort if none healthy). Default False.
    LIVE_OVERRIDE_REPLACE_ONLY: bool = False
```

- [ ] **Step 4: Add validator**

In `scout/config.py` after the `_validate_revival_min_soak_days` validator (search for it):

```python
    @field_validator("LIVE_MAX_OPEN_POSITIONS_PER_TOKEN")
    @classmethod
    def _validate_live_max_open_positions_per_token(cls, v: int) -> int:
        if v < 1:
            raise ValueError(
                f"LIVE_MAX_OPEN_POSITIONS_PER_TOKEN must be >= 1; got={v}"
            )
        return v
```

- [ ] **Step 5: Run — expect 4 PASS**

- [ ] **Step 6: Commit**

```bash
git add scout/config.py tests/test_live_master_kill.py
git commit -m "feat(live-m1): 3 Settings fields + validator (BL-NEW-LIVE-HYBRID v2.1, Option A reconciliation)"
```

---

## Task 2: signal_params.live_eligible column migration

**Files:**
- Modify: `scout/db.py` (add migration after `_migrate_moonshot_opt_out_column`)
- Modify: `scout/trading/params.py` (add field + extend SELECT to row[13])
- Test: `tests/test_live_eligible_migration.py` (NEW)

- [ ] **Step 1: Failing test**

Create `tests/test_live_eligible_migration.py`:

```python
"""BL-NEW-LIVE-HYBRID M1: live_eligible column migration."""
from __future__ import annotations

import pytest
from scout.db import Database


@pytest.mark.asyncio
async def test_signal_params_has_live_eligible_column(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(signal_params)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "live_eligible" in cols
    await db.close()


@pytest.mark.asyncio
async def test_live_eligible_defaults_to_0_for_seed_signals(tmp_path):
    """Default fail-closed: every seed row gets live_eligible=0."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT signal_type, live_eligible FROM signal_params"
    )
    for sig, opt in await cur.fetchall():
        assert opt == 0, f"{sig} should default to 0; got {opt}"
    await db.close()


@pytest.mark.asyncio
async def test_migration_idempotent_on_rerun(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._migrate_live_eligible_column()
    cur = await db._conn.execute("PRAGMA table_info(signal_params)")
    cols = [row[1] for row in await cur.fetchall()]
    assert cols.count("live_eligible") == 1
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_migrations WHERE name = ?",
        ("bl_live_eligible_v1",),
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()
```

- [ ] **Step 2: Run — expect 3 FAILs**

- [ ] **Step 3: Add migration to scout/db.py**

After `_migrate_moonshot_opt_out_column`, add `_migrate_live_eligible_column` mirroring the BL-NEW-MOONSHOT-OPT-OUT shape (PRAGMA-guarded ALTER + paper_migrations stamp + schema_version 20260508 + post-assertion INSIDE try). Migration body:

```python
    async def _migrate_live_eligible_column(self) -> None:
        """BL-NEW-LIVE-HYBRID M1: per-signal live-execution opt-in.
        Adds signal_params.live_eligible INTEGER NOT NULL DEFAULT 0.
        Layer 3 of the 4-layer kill stack."""
        import structlog
        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS paper_migrations (
                    name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL
                )""")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL,
                    description TEXT NOT NULL
                )""")
            cur_pragma = await conn.execute("PRAGMA table_info(signal_params)")
            existing_cols = {row[1] for row in await cur_pragma.fetchall()}
            if "live_eligible" not in existing_cols:
                await conn.execute(
                    "ALTER TABLE signal_params "
                    "ADD COLUMN live_eligible INTEGER NOT NULL DEFAULT 0"
                )
            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)", ("bl_live_eligible_v1", now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260508, now_iso, "bl_live_eligible_v1"),
            )
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name = ?",
                ("bl_live_eligible_v1",),
            )
            if (await cur.fetchone()) is None:
                raise RuntimeError(
                    "bl_live_eligible_v1 cutover row missing"
                )
            await conn.commit()
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            _log.error("SCHEMA_DRIFT_DETECTED", migration="bl_live_eligible_v1")
            raise
```

Wire into `Database.initialize()` after `await self._migrate_moonshot_opt_out_column()`:

```python
        await self._migrate_live_eligible_column()
```

- [ ] **Step 4: Add `live_eligible` to SignalParams dataclass**

In `scout/trading/params.py`, after `moonshot_enabled: bool = True`:

```python
    # BL-NEW-LIVE-HYBRID M1 — Layer 3 per-signal opt-in. Default 0 fail-closed.
    live_eligible: bool = False
```

- [ ] **Step 5: Extend SELECT + constructor**

Find the SELECT around line 167 (search `moonshot_enabled` in SELECT). Add `live_eligible` as 14th column:

```python
    cursor = await db._conn.execute(
        """SELECT leg_1_pct, leg_1_qty_frac, leg_2_pct, leg_2_qty_frac,
                  trail_pct, trail_pct_low_peak, low_peak_threshold_pct,
                  sl_pct, max_duration_hours, enabled,
                  conviction_lock_enabled, high_peak_fade_enabled,
                  moonshot_enabled, live_eligible
           FROM signal_params WHERE signal_type = ?""",
        (signal_type,),
    )
```

In constructor: `live_eligible=bool(row[13])` after `moonshot_enabled=bool(row[12])`.

- [ ] **Step 6: Run — expect 3 PASS**

- [ ] **Step 7: Commit**

```bash
git add scout/db.py scout/trading/params.py tests/test_live_eligible_migration.py
git commit -m "feat(live-m1): signal_params.live_eligible column (Layer 3 opt-in)"
```

---

## Task 3: Per-venue services tables migration (combined: venue_health + wallet_snapshots + venue_listings + venue_rate_state + symbol_aliases)

**Files:**
- Modify: `scout/db.py` (add `_migrate_per_venue_services` migration)
- Test: `tests/test_live_per_venue_services_migration.py` (NEW)

- [ ] **Step 1: Failing test**

Create `tests/test_live_per_venue_services_migration.py`:

```python
"""BL-NEW-LIVE-HYBRID M1: 5 per-venue services tables migration."""
from __future__ import annotations
import pytest
from scout.db import Database


@pytest.mark.asyncio
async def test_all_5_tables_exist(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    expected = {"venue_health", "wallet_snapshots", "venue_listings",
                "venue_rate_state", "symbol_aliases"}
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    actual = {row[0] for row in await cur.fetchall()}
    missing = expected - actual
    assert not missing, f"missing tables: {missing}"
    await db.close()


@pytest.mark.asyncio
async def test_venue_health_has_expected_columns(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(venue_health)")
    cols = {row[1] for row in await cur.fetchall()}
    expected = {
        "venue", "probe_at", "rest_responsive", "rest_latency_ms",
        "ws_connected", "rate_limit_headroom_pct", "auth_ok",
        "last_balance_fetch_ok", "last_quote_mid_price", "last_quote_at",
        "last_depth_at_size_bps", "fills_30d_count", "is_dormant",
        "error_text",
    }
    assert expected <= cols, f"missing: {expected - cols}"
    await db.close()


@pytest.mark.asyncio
async def test_venue_listings_unique_per_venue_canonical_class(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute(
        """INSERT INTO venue_listings
           (venue, canonical, venue_pair, quote, asset_class, refreshed_at)
           VALUES ('binance', 'BTC', 'BTCUSDT', 'USDT', 'perp', '2026-05-08T00:00:00+00:00')"""
    )
    await db._conn.commit()
    with pytest.raises(Exception):  # IntegrityError on duplicate
        await db._conn.execute(
            """INSERT INTO venue_listings
               (venue, canonical, venue_pair, quote, asset_class, refreshed_at)
               VALUES ('binance', 'BTC', 'BTCUSDT', 'USDT', 'perp', '2026-05-08T00:00:01+00:00')"""
        )
        await db._conn.commit()
    await db.close()


@pytest.mark.asyncio
async def test_migration_idempotent(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._migrate_per_venue_services()  # second call
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_migrations WHERE name = ?",
        ("bl_per_venue_services_v1",),
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()
```

- [ ] **Step 2: Run — expect FAILs**

- [ ] **Step 3: Add migration**

After `_migrate_live_eligible_column`, add `_migrate_per_venue_services`. The migration creates 5 tables + indexes per the design's schemas. Wrap in BEGIN EXCLUSIVE; PRAGMA-guarded; paper_migrations marker `bl_per_venue_services_v1`; schema_version 20260510. Five `CREATE TABLE IF NOT EXISTS` statements per design v2.1 §"Per-venue services framework — concrete schemas":

```python
    async def _migrate_per_venue_services(self) -> None:
        """BL-NEW-LIVE-HYBRID M1: 5 per-venue services tables.
        venue_health, wallet_snapshots, venue_listings, venue_rate_state,
        symbol_aliases. All idempotent via CREATE TABLE IF NOT EXISTS."""
        import structlog
        _log = structlog.get_logger()
        if self._conn is None:
            raise RuntimeError("Database not initialized.")
        conn = self._conn
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            await conn.execute("BEGIN EXCLUSIVE")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS paper_migrations (
                    name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)""")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL,
                    description TEXT NOT NULL)""")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS venue_health (
                    venue                   TEXT NOT NULL,
                    probe_at                TEXT NOT NULL,
                    rest_responsive         INTEGER NOT NULL,
                    rest_latency_ms         INTEGER,
                    ws_connected            INTEGER NOT NULL,
                    rate_limit_headroom_pct REAL,
                    auth_ok                 INTEGER NOT NULL,
                    last_balance_fetch_ok   INTEGER NOT NULL,
                    last_quote_mid_price    REAL,
                    last_quote_at           TEXT,
                    last_depth_at_size_bps  REAL,
                    fills_30d_count         INTEGER NOT NULL DEFAULT 0,
                    is_dormant              INTEGER NOT NULL DEFAULT 0,
                    error_text              TEXT,
                    PRIMARY KEY (venue, probe_at)
                )""")
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_venue_health_recent "
                "ON venue_health(venue, probe_at DESC)"
            )

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS wallet_snapshots (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    venue         TEXT NOT NULL,
                    asset         TEXT NOT NULL,
                    balance       REAL NOT NULL,
                    balance_usd   REAL,
                    snapshot_at   TEXT NOT NULL
                )""")
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_wallet_snapshots_venue_recent "
                "ON wallet_snapshots(venue, snapshot_at DESC)"
            )

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS venue_listings (
                    venue         TEXT NOT NULL,
                    canonical     TEXT NOT NULL,
                    venue_pair    TEXT NOT NULL,
                    quote         TEXT NOT NULL,
                    asset_class   TEXT NOT NULL CHECK (
                        asset_class IN ('spot','perp','option','equity','forex')),
                    listed_at     TEXT,
                    delisted_at   TEXT,
                    refreshed_at  TEXT NOT NULL,
                    PRIMARY KEY (venue, canonical, asset_class)
                )""")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS venue_rate_state (
                    venue                TEXT PRIMARY KEY,
                    last_updated_at      TEXT NOT NULL,
                    requests_per_min_cap INTEGER NOT NULL,
                    requests_seen_60s    INTEGER NOT NULL DEFAULT 0,
                    headroom_pct         REAL NOT NULL DEFAULT 100.0
                )""")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS symbol_aliases (
                    canonical    TEXT NOT NULL,
                    venue        TEXT NOT NULL,
                    venue_symbol TEXT NOT NULL,
                    PRIMARY KEY (canonical, venue)
                )""")

            await conn.execute(
                "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
                "VALUES (?, ?)", ("bl_per_venue_services_v1", now_iso),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260510, now_iso, "bl_per_venue_services_v1"),
            )
            cur = await conn.execute(
                "SELECT 1 FROM paper_migrations WHERE name = ?",
                ("bl_per_venue_services_v1",),
            )
            if (await cur.fetchone()) is None:
                raise RuntimeError("bl_per_venue_services_v1 cutover row missing")
            await conn.commit()
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception as rb_err:
                _log.exception("schema_migration_rollback_failed", err=str(rb_err))
            _log.error("SCHEMA_DRIFT_DETECTED", migration="bl_per_venue_services_v1")
            raise
```

Wire into `initialize()` after `_migrate_live_eligible_column`.

- [ ] **Step 4: Run — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add scout/db.py tests/test_live_per_venue_services_migration.py
git commit -m "feat(live-m1): per-venue services framework tables (5 tables, 1 migration)"
```

---

## Task 4: cross_venue_exposure SQL view (M1-blocker for Gate 7) + cross_venue_pnl scaffold

**Files:**
- Modify: `scout/db.py` (add view creation in `_create_tables`)
- Test: extend `tests/test_live_per_venue_services_migration.py` with the cross_venue_exposure view-existence test (no separate file — orphan removed per plan-stage structural reviewer)

- [ ] **Step 1: Add view creation in `_create_tables` (one of the SQL statements list)**

```python
            """
            CREATE VIEW IF NOT EXISTS cross_venue_exposure AS
            SELECT
                'binance' AS venue,
                COALESCE(SUM(CAST(size_usd AS REAL)), 0) AS open_exposure_usd,
                COUNT(*) AS open_count
            FROM live_trades
            WHERE status = 'open'
            UNION ALL
            SELECT
                'minara_' || COALESCE(chain, 'unknown') AS venue,
                COALESCE(SUM(amount_usd), 0) AS open_exposure_usd,
                COUNT(*) AS open_count
            FROM paper_trades
            WHERE status = 'open' AND chain != 'coingecko'
            GROUP BY chain
            """,
            """
            CREATE VIEW IF NOT EXISTS cross_venue_pnl AS
            SELECT
                'placeholder_m1' AS venue,
                0.0 AS realized_pnl_usd,
                0.0 AS unrealized_pnl_usd
            """,
```

(M1 ships cross_venue_pnl as scaffold; M2 fills it in.)

- [ ] **Step 2: Test view exists (combine into existing test file)**

In `tests/test_live_per_venue_services_migration.py`, add:

```python
@pytest.mark.asyncio
async def test_cross_venue_exposure_view_exists(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='view' AND name='cross_venue_exposure'"
    )
    assert (await cur.fetchone()) is not None
    await db.close()
```

- [ ] **Step 3: Run + commit**

```bash
git add scout/db.py tests/test_live_per_venue_services_migration.py
git commit -m "feat(live-m1): cross_venue_exposure + cross_venue_pnl SQL views"
```

---

## Task 5: ExchangeAdapter ABC reshape (M1-included per structural-reviewer)

**Files:**
- Modify: `scout/live/adapter_base.py` (split `send_order`; generalize `fetch_exchange_info_row`; add `VenueMetadata` dataclass)
- Modify: `scout/live/binance_adapter.py` (migrate to new ABC)
- Test: `tests/test_live_adapter_abc_reshape.py` (NEW)

- [ ] **Step 1: Failing test**

Create `tests/test_live_adapter_abc_reshape.py`:

```python
"""BL-NEW-LIVE-HYBRID M1: ExchangeAdapter ABC reshape."""
from __future__ import annotations

import inspect
from pathlib import Path

# Read source as text — avoids Windows OpenSSL Applink crash from
# importing scout.live.adapter_base (which transitively pulls aiohttp).
_ADAPTER_BASE = (
    Path(__file__).resolve().parent.parent
    / "scout" / "live" / "adapter_base.py"
).read_text(encoding="utf-8")


def test_abc_has_place_order_request_method():
    assert "def place_order_request" in _ADAPTER_BASE


def test_abc_has_await_fill_confirmation_method():
    assert "def await_fill_confirmation" in _ADAPTER_BASE


def test_abc_has_fetch_venue_metadata_method():
    assert "def fetch_venue_metadata" in _ADAPTER_BASE


def test_abc_defines_venue_metadata_dataclass():
    assert "class VenueMetadata" in _ADAPTER_BASE
    assert "@dataclass" in _ADAPTER_BASE


def test_old_send_order_method_removed():
    """The old single-call send_order is removed; callers use
    place_order_request + await_fill_confirmation."""
    # Allow `send_order` in comments/docstrings (referencing legacy)
    # but not as a method definition.
    lines = [l for l in _ADAPTER_BASE.split("\n") if not l.strip().startswith("#")]
    method_defs = "\n".join(lines)
    assert "def send_order" not in method_defs
```

- [ ] **Step 2: Run — expect 5 FAILs**

- [ ] **Step 3: Reshape ABC**

Replace `scout/live/adapter_base.py` body. Keep existing module-level imports + `venue_name` class var. New shape:

```python
"""ExchangeAdapter ABC — supports Tier 1 (CLI), Tier 2 (aggregator),
Tier 3a (bespoke), Tier 3b (CCXT-backed). Reshaped 2026-05-08 per
design v2.1 §"ABC reshape" — split send_order + generalize metadata."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class VenueMetadata:
    """Generic venue metadata — Tier-1/2/3a/3b adapters all populate this
    from their venue-specific source (REST exchangeInfo, CCXT markets,
    CLI subprocess output, aggregator skill response). Routing layer
    consumes this without knowing the source."""
    venue: str
    canonical: str           # e.g. "BTC"
    venue_pair: str          # e.g. "BTCUSDT" / "PF_XBTUSD" / "BTC-USD"
    quote: str               # e.g. "USDT"
    asset_class: str         # 'spot' | 'perp' | 'option' | 'equity' | 'forex'
    min_size: float | None
    tick_size: float | None
    lot_size: float | None


@dataclass(frozen=True)
class OrderRequest:
    paper_trade_id: int
    canonical: str
    venue_pair: str
    side: str                # 'buy' | 'sell'
    size_usd: float
    intent_uuid: str         # gecko-side; populates client_order_id


@dataclass(frozen=True)
class OrderConfirmation:
    venue: str
    venue_order_id: str | None
    client_order_id: str | None
    status: str              # 'filled' | 'partial' | 'rejected' | 'pending' | 'timeout'
    filled_qty: float | None
    fill_price: float | None
    raw_response: dict[str, Any] | None


class ExchangeAdapter(ABC):
    venue_name: str

    @abstractmethod
    async def fetch_venue_metadata(self, canonical: str) -> VenueMetadata | None:
        """Return VenueMetadata for canonical symbol or None if not listed
        on this venue. Generalizes the old fetch_exchange_info_row which
        was Binance-REST-shaped."""
        ...

    @abstractmethod
    async def resolve_pair_for_symbol(self, canonical: str) -> str | None:
        """Return venue-side pair string for canonical (e.g. 'BTC' -> 'BTCUSDT'
        for Binance, 'PF_XBTUSD' for Kraken futures, 'BTC/USDT' for CCXT).
        Default impl: query fetch_venue_metadata + return venue_pair."""
        meta = await self.fetch_venue_metadata(canonical)
        return meta.venue_pair if meta is not None else None

    @abstractmethod
    async def fetch_depth(self, pair: str) -> dict[str, Any]:
        """Return orderbook depth at trade size."""
        ...

    @abstractmethod
    async def place_order_request(self, request: OrderRequest) -> str:
        """Submit order; return venue_order_id immediately. For two-step
        venues (Minara quote-then-confirm), this submits the request
        portion. For single-call venues (Binance REST), this submits +
        returns the order ID synchronously."""
        ...

    @abstractmethod
    async def await_fill_confirmation(
        self, *, venue_order_id: str, client_order_id: str, timeout_sec: float
    ) -> OrderConfirmation:
        """Wait for order to reach terminal state (filled/partial/rejected)
        or timeout. For single-call venues this can return immediately
        from place_order_request's response. For async venues, this polls
        or awaits a websocket event."""
        ...

    @abstractmethod
    async def fetch_account_balance(self, asset: str = "USDT") -> float:
        """Return free balance in `asset`. Used by balance_gate."""
        ...
```

- [ ] **Step 4: Migrate binance_adapter.py to new ABC**

In `scout/live/binance_adapter.py`:
1. Replace `send_order` with `place_order_request` (returns order_id) + `await_fill_confirmation` (polls /fapi/v1/order until terminal or timeout).
2. Replace `fetch_exchange_info_row` with `fetch_venue_metadata` returning `VenueMetadata` populated from /fapi/v1/exchangeInfo response.
3. Add `fetch_account_balance` (Task 8 prereq — implement here).

This is a substantive change; see existing code in `binance_adapter.py` for the underlying API call shape. Reference test fixture at `tests/integration/test_binance_adapter.py` if it exists.

- [ ] **Step 5: Run — expect ABC tests PASS + existing binance tests PASS**

```bash
uv run pytest tests/test_live_adapter_abc_reshape.py -v
uv run pytest tests/integration/test_binance_adapter.py -v 2>&1 | tail -3
```

(If integration tests don't exist on Windows — they pull aiohttp/OpenSSL — that's expected. Run on VPS post-deploy.)

- [ ] **Step 6: Commit**

```bash
git add scout/live/adapter_base.py scout/live/binance_adapter.py tests/test_live_adapter_abc_reshape.py
git commit -m "feat(live-m1): ABC reshape — place_order/await_fill split + fetch_venue_metadata"
```

---

## Task 6: CCXTAdapter scaffold

**Files:**
- Create: `scout/live/ccxt_adapter.py`
- Test: `tests/test_live_ccxt_adapter_scaffold.py` (NEW)

- [ ] **Step 1: Add ccxt to dependencies**

```bash
uv add "ccxt==4.5.52"
```

`uv add` updates `uv.lock` automatically — no special flag needed. The pinned version matches the CCXT verification's latest stable release; bump cadence is operator-driven quarterly per the design's "no auto-bump" policy.

- [ ] **Step 2: Failing test**

```python
"""BL-NEW-LIVE-HYBRID M1: CCXTAdapter scaffold."""
from __future__ import annotations

import inspect
from pathlib import Path

_CCXT_ADAPTER_PATH = (
    Path(__file__).resolve().parent.parent / "scout" / "live" / "ccxt_adapter.py"
)


def test_module_exists():
    assert _CCXT_ADAPTER_PATH.exists()


def test_class_has_required_methods():
    src = _CCXT_ADAPTER_PATH.read_text(encoding="utf-8")
    assert "class CCXTAdapter" in src
    for method in [
        "fetch_venue_metadata", "place_order_request",
        "await_fill_confirmation", "fetch_depth", "fetch_account_balance",
    ]:
        assert f"async def {method}" in src, f"missing: {method}"


def test_class_takes_venue_name_kwarg():
    src = _CCXT_ADAPTER_PATH.read_text(encoding="utf-8")
    assert "venue_name" in src and "ccxt." in src
```

- [ ] **Step 3: Create scout/live/ccxt_adapter.py**

```python
"""CCXTAdapter — Tier 3b for the long tail (Bybit, OKX, Coinbase, MEXC,
Gate, etc.). Parameterized by venue name. Delegates to ccxt.<venue>.
Per design v2.1 — scaffolded at M1, NOT wired to any venue. M1.5 wires
the first CCXT venue."""
from __future__ import annotations

from typing import Any

import ccxt.async_support as ccxt_async  # async variant for asyncio
import structlog

from scout.live.adapter_base import (
    ExchangeAdapter, OrderConfirmation, OrderRequest, VenueMetadata,
)

log = structlog.get_logger(__name__)


class CCXTAdapter(ExchangeAdapter):
    """Generic CCXT-backed adapter. Constructor: CCXTAdapter('bybit', api_key=..., secret=...)."""

    def __init__(
        self, venue_name: str, *, api_key: str | None = None,
        secret: str | None = None, **ccxt_options: Any,
    ) -> None:
        self.venue_name = venue_name
        ccxt_class = getattr(ccxt_async, venue_name)
        self._client = ccxt_class({
            "apiKey": api_key, "secret": secret, **ccxt_options,
        })

    async def fetch_venue_metadata(self, canonical: str) -> VenueMetadata | None:
        # Load markets if not yet loaded; CCXT caches this internally.
        await self._client.load_markets()
        # Try common variations: BTC/USDT, BTC/USD, BTC/USDT:USDT (perp)
        for symbol in [f"{canonical}/USDT", f"{canonical}/USD", f"{canonical}/USDT:USDT"]:
            if symbol in self._client.markets:
                m = self._client.markets[symbol]
                return VenueMetadata(
                    venue=self.venue_name, canonical=canonical,
                    venue_pair=m["id"], quote=m["quote"],
                    asset_class="perp" if m.get("contract") else "spot",
                    min_size=m.get("limits", {}).get("amount", {}).get("min"),
                    tick_size=m.get("precision", {}).get("price"),
                    lot_size=m.get("precision", {}).get("amount"),
                )
        return None

    async def resolve_pair_for_symbol(self, canonical: str) -> str | None:
        meta = await self.fetch_venue_metadata(canonical)
        return meta.venue_pair if meta is not None else None

    async def fetch_depth(self, pair: str) -> dict[str, Any]:
        return await self._client.fetch_l2_order_book(pair)

    async def place_order_request(self, request: OrderRequest) -> str:
        client_order_id = f"gecko-{request.paper_trade_id}-{request.intent_uuid}"
        # Convert size_usd → quantity using current price; this is venue-specific.
        # M1 scaffold returns NotImplementedError; M1.5 wires it.
        raise NotImplementedError(
            "CCXTAdapter is M1 scaffold; first wired venue is M1.5."
        )

    async def await_fill_confirmation(
        self, *, venue_order_id: str, client_order_id: str, timeout_sec: float
    ) -> OrderConfirmation:
        raise NotImplementedError(
            "CCXTAdapter is M1 scaffold; first wired venue is M1.5."
        )

    async def fetch_account_balance(self, asset: str = "USDT") -> float:
        balance = await self._client.fetch_balance()
        return float(balance.get(asset, {}).get("free", 0.0))

    async def close(self) -> None:
        await self._client.close()
```

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/test_live_ccxt_adapter_scaffold.py -v
git add scout/live/ccxt_adapter.py tests/test_live_ccxt_adapter_scaffold.py pyproject.toml uv.lock
git commit -m "feat(live-m1): CCXTAdapter scaffold (Tier 3b — not wired at M1)"
```

---

## Task 7: Update Gate 7 (cross-venue exposure) + add Gate 8 (notional cap) + Gate 9 (signal opt-in) + extend reject_reason CHECK

**Files:**
- Modify: `scout/live/gates.py`
- Modify: `scout/db.py` (extend reject_reason CHECK constraint via migration if shadow_trades/live_trades have rows; otherwise update CREATE TABLE)
- Test: `tests/test_live_master_kill.py` (extend) + extend `tests/test_live_per_venue_services_migration.py` for view-related coverage

- [ ] **Step 1: Extend reject_reason CHECK constraint**

In `scout/db.py`, update both `live_trades` and `shadow_trades` CREATE TABLE CHECK lists. **Per plan-stage policy reviewer: include `dual_signal_aggregate` in the list (was missing in initial draft — would cause silent INSERT failure on the BILL pattern).**

```python
                reject_reason TEXT CHECK (reject_reason IS NULL OR reject_reason IN (
                    'no_venue','insufficient_depth','slippage_exceeds_cap','insufficient_balance',
                    'daily_cap_hit','kill_switch','exposure_cap','override_disabled',
                    'venue_unavailable',
                    'notional_cap_exceeded','signal_disabled','token_aggregate',
                    'dual_signal_aggregate','all_candidates_failed',
                    'master_kill','mode_paper'
                )),
```

(If the tables exist with old constraint: write a `_migrate_reject_reason_extend` migration with table-rename pattern. Skip for M1 if no live_trades/shadow_trades rows exist yet — likely true since LIVE_MODE was paper.)

**Note on `token_aggregate` vs `dual_signal_aggregate`:** these are DISTINCT reasons. `token_aggregate` fires when `LIVE_MAX_OPEN_POSITIONS_PER_TOKEN` cap reached (1 by default — the M1-blocker guard). `dual_signal_aggregate` fires when two signals on the same token+venue race the same routing call (Phase-2 aggregator deferred per design v2.1; reserved as a CHECK value at M1 so the column is forward-compatible when the Phase-2 service ships).

- [ ] **Step 2: Failing tests for Gate 7/8/9**

(See v1 plan Task 4-6 for test scaffolding pattern; replicate here.)

- [ ] **Step 3: Update Gate 7 to query cross_venue_exposure view**

In `scout/live/gates.py` around line 207-231 (current Gate 7 query), replace:

```python
        # Gate 7: cross-venue exposure cap (BL-NEW-LIVE-HYBRID M1 v2.1).
        cur = await conn.execute(
            "SELECT COALESCE(SUM(open_exposure_usd), 0) FROM cross_venue_exposure"
        )
        open_total = float((await cur.fetchone())[0])
        # Reuses existing Settings field (BL-055-shipped) per Option A drift-check.
        cap = float(settings.LIVE_MAX_EXPOSURE_USD)
        if open_total + amount_usd > cap:
            await inc(db, "live_orders_skipped_exposure_cap")
            return GateResult(
                approved=False, reject_reason="exposure_cap",
                detail=(f"open=${open_total:.2f} + new=${amount_usd:.2f} > "
                        f"cap=${cap:.2f}"),
            )
```

- [ ] **Step 4: Add Gate 8 (per-trade notional cap)** + **Gate 9 (per-signal opt-in)**

After Gate 7, add:

```python
        # Gate 8: per-trade notional cap.
        # Reuses existing Settings field LIVE_TRADE_AMOUNT_USD (BL-055-shipped) per Option A drift-check.
        notional_cap = float(settings.LIVE_TRADE_AMOUNT_USD)
        if amount_usd > notional_cap:
            await inc(db, "live_orders_skipped_notional_cap")
            return GateResult(
                approved=False, reject_reason="notional_cap_exceeded",
                detail=(f"amount_usd=${amount_usd:.2f} > "
                        f"cap=${notional_cap:.2f}"),
            )

        # Gate 9: per-signal opt-in (Layer 3 of 4-layer kill stack).
        cur = await conn.execute(
            "SELECT live_eligible FROM signal_params WHERE signal_type = ?",
            (paper_trade.signal_type,),
        )
        row = await cur.fetchone()
        if not (row and bool(row[0])):
            await inc(db, "live_orders_skipped_signal_disabled")
            return GateResult(
                approved=False, reject_reason="signal_disabled",
                detail=f"signal_params.live_eligible=0 for {paper_trade.signal_type}",
            )
```

- [ ] **Step 5: Run + commit**

---

## Task 7.5: Approval-removal telemetry plumbing (per plan-stage policy reviewer)

**Files:**
- Modify: `scout/db.py` — add columns to `live_trades`: `fill_slippage_bps REAL` (computed at fill-confirmation time), `correction_at TEXT` (when operator unwinds within 24h), `correction_reason TEXT`. Add new table `signal_venue_correction_count` (per-pair running counter that the approval-removal gate reads).
- Modify: `scout/live/binance_adapter.py` — populate `fill_slippage_bps` in `await_fill_confirmation` (compute `(fill_price / mid_at_submit - 1) * 10000`).
- Modify: `scout/live/engine.py` — increment / reset `signal_venue_correction_count` on trade close events.
- Test: extend `tests/test_live_master_kill.py` with telemetry tests.

Without these columns, the design's approval-removal criteria (1) trade-count gate, (3) slippage-fit gate per-(signal × venue) basis-points are TEXT-ONLY in the design — unenforceable. M1 needs the data plumbing the moment the first live trade fires; otherwise we'd have to backfill counters at autonomy-evaluation time.

- [ ] **Step 1: Failing tests**

```python
@pytest.mark.asyncio
async def test_live_trades_has_fill_slippage_bps_column(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(live_trades)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "fill_slippage_bps" in cols
    assert "correction_at" in cols
    assert "correction_reason" in cols
    await db.close()


@pytest.mark.asyncio
async def test_signal_venue_correction_count_table_exists(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='signal_venue_correction_count'"
    )
    assert (await cur.fetchone()) is not None
    cur = await db._conn.execute(
        "PRAGMA table_info(signal_venue_correction_count)"
    )
    cols = {row[1] for row in await cur.fetchall()}
    expected = {"signal_type", "venue", "consecutive_no_correction",
                "last_corrected_at", "last_updated_at"}
    assert expected <= cols
    await db.close()
```

- [ ] **Step 2: Add migration `bl_live_trades_telemetry_v1` (schema_version 20260511)** — adds the three columns to live_trades + creates `signal_venue_correction_count` table:

```python
    async def _migrate_live_trades_telemetry(self) -> None:
        """BL-NEW-LIVE-HYBRID M1 v2.1: telemetry plumbing for
        approval-removal criteria (per plan-stage policy reviewer).
        Adds:
          - live_trades.fill_slippage_bps REAL
          - live_trades.correction_at TEXT
          - live_trades.correction_reason TEXT
          - signal_venue_correction_count table (running counters)"""
        # ... full migration body mirroring bl_live_eligible_v1 shape ...
        # PRAGMA-guarded ALTERs + CREATE TABLE IF NOT EXISTS +
        # paper_migrations marker `bl_live_trades_telemetry_v1` +
        # schema_version 20260511 + post-assertion INSIDE try.
```

Schema for the new table:

```sql
CREATE TABLE IF NOT EXISTS signal_venue_correction_count (
    signal_type              TEXT NOT NULL,
    venue                    TEXT NOT NULL,
    consecutive_no_correction INTEGER NOT NULL DEFAULT 0,
    last_corrected_at        TEXT,
    last_updated_at          TEXT NOT NULL,
    PRIMARY KEY (signal_type, venue)
);
```

Wire into `Database.initialize()` after `_migrate_per_venue_services`.

- [ ] **Step 3: Wire `fill_slippage_bps` in `binance_adapter.await_fill_confirmation`**

```python
        # Compute slippage bps relative to mid_at_entry recorded at submit.
        if mid_at_entry and fill_price:
            slippage_bps = (
                (float(fill_price) / float(mid_at_entry) - 1.0) * 10000.0
            )
            await db._conn.execute(
                "UPDATE live_trades SET fill_slippage_bps = ? WHERE id = ?",
                (round(slippage_bps, 2), live_trade_id),
            )
```

- [ ] **Step 4: Wire correction-counter increment/reset in engine**

In `scout/live/engine.py`, on every trade close event:
- If `correction_at IS NULL` (no operator unwound it) → `consecutive_no_correction += 1`
- If `correction_at IS NOT NULL` → reset to 0 + record `last_corrected_at`

```python
async def _update_correction_counter(
    db, signal_type: str, venue: str, was_corrected: bool
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    if was_corrected:
        await db._conn.execute(
            """INSERT INTO signal_venue_correction_count
               (signal_type, venue, consecutive_no_correction,
                last_corrected_at, last_updated_at)
               VALUES (?, ?, 0, ?, ?)
               ON CONFLICT (signal_type, venue) DO UPDATE SET
                  consecutive_no_correction = 0,
                  last_corrected_at = excluded.last_corrected_at,
                  last_updated_at = excluded.last_updated_at""",
            (signal_type, venue, now_iso, now_iso),
        )
    else:
        await db._conn.execute(
            """INSERT INTO signal_venue_correction_count
               (signal_type, venue, consecutive_no_correction, last_updated_at)
               VALUES (?, ?, 1, ?)
               ON CONFLICT (signal_type, venue) DO UPDATE SET
                  consecutive_no_correction = consecutive_no_correction + 1,
                  last_updated_at = excluded.last_updated_at""",
            (signal_type, venue, now_iso),
        )
    await db._conn.commit()
```

- [ ] **Step 5: Run + commit**

```bash
git add scout/db.py scout/live/binance_adapter.py scout/live/engine.py tests/test_live_master_kill.py
git commit -m "feat(live-m1): approval-removal telemetry plumbing (slippage_bps + correction counter)"
```

**Schema versions reserved (updated):** 20260508 (bl_live_eligible_v1), 20260509 (bl_live_client_order_id_v1), 20260510 (bl_per_venue_services_v1), **20260511 (bl_live_trades_telemetry_v1 — NEW per policy reviewer).**

---

## Task 8: balance_gate.py implementation

**Files:** Create `scout/live/balance_gate.py` + `tests/test_live_balance_gate.py`. Implementation mirrors v1 archived plan Task 7 — function `check_sufficient_balance(adapter, required_usd, margin_factor=1.1) → BalanceGateResult`. See `tasks/plan_live_trading_milestone_1_v1_archived.md` Task 7 for full code blocks; copy verbatim. Wire into gates.py AFTER depth check, BEFORE order submission.

- [ ] Steps 1-6: see archived v1 Task 7 (3 tests + module + binance_adapter integration via existing `fetch_account_balance` from Task 5).

- [ ] **Commit:**

```bash
git commit -m "feat(live-m1): balance_gate.py (BL-055 prereq, was missing 2026-05-03)"
```

---

## Task 9: Routing layer (`scout/live/routing.py`) — M1-blocker live-position-aggregator + on-demand fetch + chain enrichment + override + delisting fallback

**Files:**
- Create: `scout/live/routing.py`
- Test: `tests/test_live_routing.py` (NEW)

- [ ] **Step 1: Failing tests** (5+ tests covering each routing-layer behavior)

Create `tests/test_live_routing.py`:

```python
"""BL-NEW-LIVE-HYBRID M1: routing layer."""
from __future__ import annotations

import pytest

from scout.db import Database


@pytest.mark.asyncio
async def test_live_position_aggregator_rejects_when_token_already_open(
    tmp_path, settings_factory
):
    """Layer-1 M1-blocker: routing must refuse a candidate list when
    live_trades has >= LIVE_MAX_OPEN_POSITIONS_PER_TOKEN open positions
    on the canonical symbol (BILL dual-signal pattern)."""
    from scout.live.routing import RoutingLayer
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute(
        """INSERT INTO live_trades
           (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
            size_usd, status, created_at)
           VALUES (1, 'bill', 'BILL', 'binance', 'BILLUSDT', 'gainers_early',
                   '50.0', 'open', '2026-05-08T00:00:00+00:00')"""
    )
    await db._conn.commit()
    s = settings_factory(LIVE_MAX_OPEN_POSITIONS_PER_TOKEN=1)
    routing = RoutingLayer(db=db, settings=s, adapters={})
    candidates = await routing.get_candidates(
        canonical="BILL", chain_hint="solana", signal_type="chain_completed",
        size_usd=50.0,
    )
    assert candidates == [], (
        "expected aggregator-guard to reject; got " + str(candidates)
    )
    await db.close()


@pytest.mark.asyncio
async def test_live_position_aggregator_uses_symbol_not_coin_id(
    tmp_path, settings_factory
):
    """Per plan-stage structural reviewer: aggregator-guard must query by
    SYMBOL (ticker), NOT by coin_id (CoinGecko slug). For BTC, the
    CoinGecko slug is 'bitcoin' but the canonical ticker is 'BTC'.
    Querying coin_id with canonical.lower() would compare 'bitcoin' to
    'btc' and silently fail to fire the guard. This test pins the
    correct column choice."""
    from scout.live.routing import RoutingLayer
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Insert with REAL CoinGecko slug semantics: coin_id="bitcoin", symbol="BTC"
    await db._conn.execute(
        """INSERT INTO live_trades
           (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
            size_usd, status, created_at)
           VALUES (1, 'bitcoin', 'BTC', 'binance', 'BTCUSDT', 'gainers_early',
                   '50.0', 'open', '2026-05-08T00:00:00+00:00')"""
    )
    await db._conn.commit()
    s = settings_factory(LIVE_MAX_OPEN_POSITIONS_PER_TOKEN=1)
    routing = RoutingLayer(db=db, settings=s, adapters={})
    # canonical is the TICKER, not the slug
    candidates = await routing.get_candidates(
        canonical="BTC", chain_hint=None, signal_type="gainers_early",
        size_usd=50.0,
    )
    assert candidates == [], (
        "guard must fire when symbol matches, regardless of coin_id slug "
        f"divergence; got {candidates}"
    )
    await db.close()


@pytest.mark.asyncio
async def test_on_demand_listing_fetch_when_venue_listings_empty(
    tmp_path, settings_factory, monkeypatch
):
    """Routing must trigger on-demand listing-fetch when canonical has
    zero rows in venue_listings."""
    # ... test stub: monkeypatch the listing-fetch function to a recorder,
    # call routing.get_candidates, assert the recorder was invoked.
    ...


@pytest.mark.asyncio
async def test_chain_coingecko_enrichment_falls_to_dex_when_no_cex_match(
    tmp_path, settings_factory
):
    """When chain='coingecko' and venue_listings has only DEX rows for
    canonical, routing prefers DEX over default CEX."""
    ...


@pytest.mark.asyncio
async def test_override_prepend_keeps_other_candidates_as_fallback(
    tmp_path, settings_factory
):
    """OverrideStore PREPEND semantics: other healthy candidates remain
    in list lower-ranked."""
    ...


@pytest.mark.asyncio
async def test_delisting_fallback_re_evaluates_on_reject(
    tmp_path, settings_factory
):
    """When top candidate's adapter rejects with 'delisted', routing
    marks venue_listings.delisted_at and submits next candidate."""
    ...
```

- [ ] **Step 2: Implement scout/live/routing.py**

Module structure (verbose; trim noisy comments per project preference):

```python
"""Routing layer (BL-NEW-LIVE-HYBRID M1 v2.1).

Per signal fire: takes (canonical, chain_hint, signal_type, size_usd),
returns ranked candidate list of (venue, pair, expected_fill_price,
expected_slippage_bps, available_capital_usd, venue_health_score).

Layer-1 M1-blocker guards:
- live-position-aggregator: rejects when LIVE_MAX_OPEN_POSITIONS_PER_TOKEN met
- on-demand venue_listings fetch: triggered when canonical has 0 rows
- chain="coingecko" enrichment: queries ALL tiers before defaulting CEX
- OverrideStore PREPEND: forces chain's venues to top of candidate list
- delisting fallback: re-evaluates on adapter reject with 'delisted'

Latency budget: <200ms p95. Quote/depth metrics are pre-fetched into
venue_health by the HealthProbe service; routing reads, does NOT
compute live."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from scout.db import Database
from scout.live.adapter_base import ExchangeAdapter

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RouteCandidate:
    venue: str
    venue_pair: str
    expected_fill_price: float | None
    expected_slippage_bps: float | None
    available_capital_usd: float | None
    venue_health_score: float


class RoutingLayer:
    def __init__(
        self, *, db: Database, settings, adapters: dict[str, ExchangeAdapter],
    ) -> None:
        self._db = db
        self._settings = settings
        self._adapters = adapters

    async def get_candidates(
        self, *, canonical: str, chain_hint: str | None,
        signal_type: str, size_usd: float,
    ) -> list[RouteCandidate]:
        # Step 1 — live-position-aggregator guard (M1-BLOCKER)
        # CONTRACT: `canonical` is the uppercase TICKER ("BTC", "BILL"),
        # NOT the CoinGecko slug ("bitcoin"). live_trades.symbol stores
        # the ticker; live_trades.coin_id stores the CoinGecko slug.
        # We query by SYMBOL because:
        #   1. routing.py inputs are canonical tickers (per RouteCandidate.venue_pair contract)
        #   2. CoinGecko slugs differ from tickers (bitcoin vs BTC), so a
        #      coin_id query with canonical.lower() silently fails for
        #      every coin where slug != ticker.lower().
        # UPPER() comparison guarantees case-insensitive match.
        cur = await self._db._conn.execute(
            "SELECT COUNT(*) FROM live_trades "
            "WHERE UPPER(symbol) = UPPER(?) AND status = 'open'",
            (canonical,),
        )
        open_count = (await cur.fetchone())[0]
        if open_count >= self._settings.LIVE_MAX_OPEN_POSITIONS_PER_TOKEN:
            log.info(
                "routing_skipped_token_aggregate",
                canonical=canonical, open_count=open_count,
            )
            return []

        # Step 2 — fetch venue_listings rows for this canonical
        cur = await self._db._conn.execute(
            "SELECT venue, venue_pair, asset_class FROM venue_listings "
            "WHERE canonical = ? AND delisted_at IS NULL",
            (canonical,),
        )
        listings = list(await cur.fetchall())

        # Step 3 — on-demand fetch if empty
        if not listings:
            log.info("venue_listings_miss", canonical=canonical)
            await self._on_demand_listings_fetch(canonical)
            cur = await self._db._conn.execute(
                "SELECT venue, venue_pair, asset_class FROM venue_listings "
                "WHERE canonical = ? AND delisted_at IS NULL",
                (canonical,),
            )
            listings = list(await cur.fetchall())
        if not listings:
            log.info("routing_skipped_no_venue", canonical=canonical)
            return []

        # Step 4 — chain="coingecko" enrichment + OverrideStore prepend
        # ... (see test cases for required behaviors)

        # Step 5 — query venue_health for each candidate; filter dormant
        candidates = []
        for venue, venue_pair, asset_class in listings:
            cur = await self._db._conn.execute(
                "SELECT auth_ok, rest_responsive, is_dormant, "
                "       last_quote_mid_price, last_depth_at_size_bps, "
                "       fills_30d_count "
                "FROM venue_health WHERE venue = ? "
                "ORDER BY probe_at DESC LIMIT 1",
                (venue,),
            )
            health = await cur.fetchone()
            if health is None or not health[0] or not health[1] or health[2]:
                continue  # auth/rest fail or dormant
            # query wallet_snapshots for available capital
            # ... (similar pattern)
            candidates.append(RouteCandidate(
                venue=venue, venue_pair=venue_pair,
                expected_fill_price=health[3],
                expected_slippage_bps=health[4],
                available_capital_usd=None,  # TODO Task 10 wallet integration
                venue_health_score=1.0,
            ))

        # Step 6 — apply override
        # Step 7 — rank candidates by score
        # ...
        return candidates

    async def _on_demand_listings_fetch(self, canonical: str) -> None:
        """Sync REST call per adapter to populate venue_listings rows."""
        for venue, adapter in self._adapters.items():
            try:
                meta = await adapter.fetch_venue_metadata(canonical)
                if meta is not None:
                    from datetime import datetime, timezone
                    now_iso = datetime.now(timezone.utc).isoformat()
                    await self._db._conn.execute(
                        "INSERT OR REPLACE INTO venue_listings "
                        "(venue, canonical, venue_pair, quote, asset_class, "
                        " refreshed_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (venue, canonical, meta.venue_pair, meta.quote,
                         meta.asset_class, now_iso),
                    )
            except Exception:
                log.exception("on_demand_listing_fetch_failed", venue=venue)
        await self._db._conn.commit()
```

- [ ] **Step 3: Run + commit**

```bash
git add scout/live/routing.py tests/test_live_routing.py
git commit -m "feat(live-m1): routing layer (M1-blocker aggregator guard + on-demand fetch + chain enrichment)"
```

---

## Task 10: VenueService ABC + service-runner harness + 3 workers + dormancy job + rate-limit stub

**Files:**
- Create: `scout/live/services/__init__.py`
- Create: `scout/live/services/base.py` — `VenueService` ABC
- Create: `scout/live/services/runner.py` — service-runner harness
- Create: `scout/live/services/health_probe.py`
- Create: `scout/live/services/balance_snapshot.py`
- Create: `scout/live/services/rate_limit_stub.py`
- Create: `scout/live/services/dormancy.py`
- Test: `tests/test_live_services_framework.py` (NEW)

- [ ] **Step 1: Failing tests** (~6-8 tests)

```python
@pytest.mark.asyncio
async def test_venue_service_abc_has_required_methods():
    from scout.live.services.base import VenueService
    assert hasattr(VenueService, "run_once")
    assert hasattr(VenueService, "cadence_seconds")


@pytest.mark.asyncio
async def test_health_probe_writes_venue_health_row(tmp_path):
    from scout.live.services.health_probe import HealthProbe
    # ... stub adapter returning healthy state ...
    # ... call probe.run_once ...
    # ... assert venue_health row exists with rest_responsive=1


@pytest.mark.asyncio
async def test_balance_snapshot_writes_wallet_snapshots_row(tmp_path):
    from scout.live.services.balance_snapshot import BalanceSnapshot
    # ... stub adapter returning balance ...
    # ... call snapshot.run_once ...
    # ... assert wallet_snapshots row exists


@pytest.mark.asyncio
async def test_rate_limit_stub_returns_50_pct(tmp_path):
    """Per design v2.1 — stub returns 50% headroom (not 100%) — fail-safe."""
    from scout.live.services.rate_limit_stub import RateLimitAccountantStub
    stub = RateLimitAccountantStub()
    await stub.run_once(adapter=None, db=..., venue="binance")
    # ... assert venue_rate_state.headroom_pct = 50.0


@pytest.mark.asyncio
async def test_dormancy_flags_zero_fill_venues(tmp_path):
    from scout.live.services.dormancy import DormancyJob
    # ... insert venue_health row with fills_30d_count=0 ...
    # ... call job.run_once ...
    # ... assert is_dormant=1


@pytest.mark.asyncio
async def test_runner_harness_serializes_per_pair(tmp_path):
    """Per design v2.1 concurrency contract: at most one run_once per
    (adapter, service) pair at a time."""
    from scout.live.services.runner import run_venue_services
    # ... stub service that sleeps + records call timestamps ...
    # ... assert no overlapping calls per pair
```

- [ ] **Step 2: Implement modules** per design v2.1 §"Per-venue services framework"

The HealthProbe runs `await adapter.fetch_account_balance(...)` + measures latency, writes a venue_health row. BalanceSnapshot writes wallet_snapshots rows for relevant assets. RateLimitAccountantStub writes 50% headroom unconditionally. DormancyJob runs daily, sets is_dormant=1 for venues with fills_30d_count=0. Service-runner harness uses `asyncio.create_task` per (adapter, service) pair with an `asyncio.Lock` per pair.

(Bodies follow patterns from the existing `_run_feedback_schedulers` async function in `scout/main.py` for the harness — module-level function, not a method.)

- [ ] **Step 3: Run + commit**

```bash
git add scout/live/services/ tests/test_live_services_framework.py
git commit -m "feat(live-m1): VenueService framework + 3 workers + stub + dormancy job"
```

---

## Task 11: Engine wiring — master kill + notional cap + opt-in + aggregator at entry

**Files:**
- Modify: `scout/live/engine.py`
- Test: `tests/test_live_master_kill.py` (extend with engine-entry tests)

- [ ] **Step 1: Wire master-kill check** at the top of `LiveEngine.execute_intent`:

```python
        if not self._settings.LIVE_TRADING_ENABLED:
            from scout.live.metrics import inc
            log.info("live_execution_skipped_master_kill",
                     trade_id=paper_trade.id, signal_type=paper_trade.signal_type)
            await inc(self._db, "live_orders_skipped_master_kill")
            return
```

- [ ] **Step 2: Wire routing-layer call** for candidate selection (replaces hardcoded resolver call). Engine uses RoutingLayer to get candidates, picks top, dispatches to that adapter.

- [ ] **Step 3-5: Tests + run + commit**

---

## Task 12: client_order_id idempotency on Binance adapter (migration + dedup)

Per v1 archived plan Task 8 — copy verbatim. Migration `bl_live_client_order_id_v1`, schema_version 20260509.

---

## Task 13: Telegram approval gateway

**Files:**
- Create: `scout/live/telegram_approval.py`
- Test: `tests/test_live_telegram_approval.py` (uses `structlog.testing.capture_logs`)

- [ ] **Step 1-3: Implement command handlers**: `/allow-stack <token>`, `/auto-approve venue=<name>`, `/approval-required venue=<name>`, `/venue-revive name=<name>`. Each command updates a `live_operator_overrides` table (subset of overrides; ephemeral with 24h expiry).

- [ ] **Step 4: Wire to telegram bot framework** (existing alerter integration; reuse the bot token wired up 2026-05-06).

- [ ] **Step 5-6: Tests + commit**

---

## Task 13.5: Operator-in-loop threshold evaluation function (per plan-stage policy reviewer)

**Files:**
- Create: `scout/live/approval_thresholds.py` — `_should_require_approval()` function implementing the four pre-registered threshold gates
- Modify: `scout/live/engine.py` — call `_should_require_approval()` BEFORE adapter dispatch; if True, route through Telegram approval gateway
- Test: `tests/test_live_approval_thresholds.py` (NEW)

The design v2.1 pre-registers 4 thresholds (new-venue gate <30 fills, trade-size gate >2× median, venue-health gate degraded-24h, /approval-required flag). Task 13 ships command handlers. Without the threshold-evaluation function, the gates are decorative — the gateway only fires on the explicit `/approval-required` flag. Implement the autonomous threshold evaluation:

- [ ] **Step 1: Failing tests**

```python
"""BL-NEW-LIVE-HYBRID M1 v2.1: operator-in-loop threshold gates."""
from __future__ import annotations

import pytest
from scout.db import Database


@pytest.mark.asyncio
async def test_new_venue_gate_fires_below_30_fills(tmp_path, settings_factory):
    """When (signal_type × venue) has < 30 successful autonomous fills,
    require operator approval per design v2.1 §"Operator-in-loop scaling rules"."""
    from scout.live.approval_thresholds import should_require_approval
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    require, reason = await should_require_approval(
        db=db, settings=s, signal_type="first_signal", venue="binance",
        size_usd=50.0,
    )
    assert require is True
    assert reason == "new_venue_gate"
    await db.close()


@pytest.mark.asyncio
async def test_new_venue_gate_clears_at_30_fills(tmp_path, settings_factory):
    """At 30+ no-correction fills on a (signal × venue) pair, the
    new-venue gate clears."""
    from scout.live.approval_thresholds import should_require_approval
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute(
        """INSERT INTO signal_venue_correction_count
           (signal_type, venue, consecutive_no_correction, last_updated_at)
           VALUES ('first_signal', 'binance', 30, '2026-05-08T00:00:00+00:00')"""
    )
    await db._conn.commit()
    s = settings_factory()
    require, reason = await should_require_approval(
        db=db, settings=s, signal_type="first_signal", venue="binance",
        size_usd=50.0,
    )
    # Returns False if other gates also clear; here we only insert
    # consecutive_no_correction=30 + no other gate triggers, so should be False.
    assert require is False, f"new_venue_gate should clear; reason={reason}"
    await db.close()


@pytest.mark.asyncio
async def test_trade_size_gate_fires_above_2x_median(tmp_path, settings_factory):
    """Trade size > 2× median for (signal × venue) → approval required.
    Median is computed from recent live_trades (rolling 30-trade window)."""
    from scout.live.approval_thresholds import should_require_approval
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Seed 30 fills at $50 (clears new-venue gate; sets median = 50)
    for i in range(30):
        await db._conn.execute(
            """INSERT INTO live_trades
               (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
                size_usd, status, created_at)
               VALUES (?, 'btc', 'BTC', 'binance', 'BTCUSDT', 'first_signal',
                       '50.0', 'closed_tp', ?)""",
            (i + 1, f"2026-05-0{i % 9 + 1}T00:00:00+00:00"),
        )
    await db._conn.execute(
        """INSERT INTO signal_venue_correction_count
           (signal_type, venue, consecutive_no_correction, last_updated_at)
           VALUES ('first_signal', 'binance', 30, '2026-05-08T00:00:00+00:00')"""
    )
    await db._conn.commit()
    s = settings_factory()
    # Trade size $150 (3× median 50) → trade-size gate fires
    require, reason = await should_require_approval(
        db=db, settings=s, signal_type="first_signal", venue="binance",
        size_usd=150.0,
    )
    assert require is True
    assert reason == "trade_size_gate"
    await db.close()


@pytest.mark.asyncio
async def test_venue_health_gate_fires_when_degraded(tmp_path, settings_factory):
    """venue_health degraded in past 24h → approval required regardless
    of new-venue + trade-size gates."""
    # ... insert venue_health row with rate_limit_headroom_pct=20
    #     (caution range) within past 24h ...
    # ... assert require=True, reason='venue_health_gate'
    pass  # Stub — implementer fills in given the contract above
```

- [ ] **Step 2: Implement `scout/live/approval_thresholds.py`**

```python
"""BL-NEW-LIVE-HYBRID M1 v2.1: operator-in-loop threshold evaluation.

Pre-registered thresholds per design v2.1 §"Operator-in-loop scaling rules":
  1. new-venue gate: < 30 successful autonomous fills on this pair
  2. trade-size gate: > 2× median trade size for this pair
  3. venue-health gate: any caution-range metric in past 24h on the venue
  4. operator-set /approval-required flag (via Telegram command, 24h expiry)

ALL FOUR FALSE → trade auto-executes (autonomous).
ANY ONE TRUE → trade requires operator approval via Telegram.

Thresholds (30, 2×, 24h) are pre-registered and NOT runtime-tunable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

from scout.db import Database

log = structlog.get_logger(__name__)

NEW_VENUE_FILL_THRESHOLD = 30  # pre-registered; not runtime-tunable
TRADE_SIZE_MEDIAN_MULTIPLIER = 2.0  # pre-registered
VENUE_HEALTH_LOOKBACK_HOURS = 24  # pre-registered
RATE_LIMIT_CAUTION_PCT = 30.0  # below this is "caution range"


async def should_require_approval(
    *, db: Database, settings, signal_type: str, venue: str, size_usd: float,
) -> tuple[bool, str | None]:
    """Returns (require_approval, gate_name_if_required).
    gate_name is one of: 'new_venue_gate', 'trade_size_gate',
    'venue_health_gate', 'operator_flag', None."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    # Gate 1: new-venue
    cur = await db._conn.execute(
        "SELECT consecutive_no_correction FROM signal_venue_correction_count "
        "WHERE signal_type = ? AND venue = ?",
        (signal_type, venue),
    )
    row = await cur.fetchone()
    fills = row[0] if row else 0
    if fills < NEW_VENUE_FILL_THRESHOLD:
        return True, "new_venue_gate"

    # Gate 2: trade-size
    cur = await db._conn.execute(
        """SELECT CAST(size_usd AS REAL) FROM live_trades
           WHERE signal_type = ? AND venue = ? AND status LIKE 'closed%'
           ORDER BY created_at DESC LIMIT 30""",
        (signal_type, venue),
    )
    sizes = [row[0] for row in await cur.fetchall()]
    if sizes:
        sizes_sorted = sorted(sizes)
        median = sizes_sorted[len(sizes_sorted) // 2]
        if size_usd > TRADE_SIZE_MEDIAN_MULTIPLIER * median:
            return True, "trade_size_gate"

    # Gate 3: venue-health
    lookback_iso = (
        datetime.now(timezone.utc) - timedelta(hours=VENUE_HEALTH_LOOKBACK_HOURS)
    ).isoformat()
    cur = await db._conn.execute(
        """SELECT auth_ok, rest_responsive, rate_limit_headroom_pct
           FROM venue_health
           WHERE venue = ? AND probe_at >= ?
           ORDER BY probe_at DESC LIMIT 30""",
        (venue, lookback_iso),
    )
    for auth_ok, rest_resp, headroom in await cur.fetchall():
        if not auth_ok or not rest_resp or (
            headroom is not None and headroom < RATE_LIMIT_CAUTION_PCT
        ):
            return True, "venue_health_gate"

    # Gate 4: operator /approval-required flag
    cur = await db._conn.execute(
        """SELECT 1 FROM live_operator_overrides
           WHERE override_type = 'approval_required' AND venue = ?
             AND expires_at > ?""",
        (venue, datetime.now(timezone.utc).isoformat()),
    )
    if await cur.fetchone() is not None:
        return True, "operator_flag"

    return False, None
```

- [ ] **Step 3: Wire into engine**

In `scout/live/engine.py` after master-kill check, BEFORE adapter dispatch:

```python
        from scout.live.approval_thresholds import should_require_approval
        require, gate = await should_require_approval(
            db=self._db, settings=self._settings,
            signal_type=paper_trade.signal_type,
            venue=top_candidate.venue, size_usd=paper_trade.amount_usd,
        )
        if require:
            from scout.live.telegram_approval import request_operator_approval
            approved = await request_operator_approval(
                db=self._db, paper_trade=paper_trade, candidate=top_candidate,
                gate=gate, timeout_sec=300,
            )
            if not approved:
                await inc(self._db, f"live_orders_skipped_approval_{gate}")
                return
```

- [ ] **Step 4: Run + commit**

```bash
git add scout/live/approval_thresholds.py scout/live/engine.py tests/test_live_approval_thresholds.py
git commit -m "feat(live-m1): operator-in-loop threshold evaluation function (4 gates)"
```

---

## Task 14: Telegram startup notification + scout/main wiring

Per v1 archived plan Task 9 — copy `_emit_live_trading_startup_notification` + register service-runner harness in main loop.

---

## Task 15: symbol_normalize module + canonical-extraction rule

**Files:**
- Create: `scout/live/symbol_normalize.py`
- Test: `tests/test_live_symbol_normalize.py`

- [ ] **Step 1: Failing tests**

```python
def test_canonical_from_ccxt_market_strips_quote():
    """BTC/USDT → BTC"""
    from scout.live.symbol_normalize import canonical_from_ccxt_market
    assert canonical_from_ccxt_market("BTC/USDT") == "BTC"


def test_canonical_from_ccxt_market_strips_perp_suffix():
    """BTC/USDT:USDT → BTC"""
    from scout.live.symbol_normalize import canonical_from_ccxt_market
    assert canonical_from_ccxt_market("BTC/USDT:USDT") == "BTC"


def test_canonical_handles_1inch_style():
    """1INCH/USDT → 1INCH"""
    from scout.live.symbol_normalize import canonical_from_ccxt_market
    assert canonical_from_ccxt_market("1INCH/USDT") == "1INCH"
```

- [ ] **Step 2: Implement**

```python
def canonical_from_ccxt_market(symbol: str) -> str:
    """Extract canonical ticker from CCXT market symbol string.

    Examples:
        BTC/USDT → BTC
        BTC/USDT:USDT (perp) → BTC
        1INCH/USDT → 1INCH

    Splits on '/'; takes [0]; strips ':USDT' / ':USD' settlement suffix
    if present (CCXT perp notation)."""
    return symbol.split("/")[0].split(":")[0]
```

---

## Task 16: Full regression + black + PR + 3-vector reviewers + merge

- [ ] **Step 1: Full regression**

```bash
uv run pytest tests/test_live_*.py -q
```

All new test files pass + existing tests still green.

- [ ] **Step 2: Black**

```bash
uv run black scout/ tests/
```

- [ ] **Step 3: Open PR** + dispatch 3-vector reviewers (statistical/policy + code/structural + strategy/blast-radius). Per CLAUDE.md §8, this change touches money flows; multi-vector review is required.

- [ ] **Step 4: Apply MUST-FIX findings** + commit

- [ ] **Step 5: Mark ready + squash-merge + delete-branch**

- [ ] **Step 6: Deploy to VPS** (LIVE_TRADING_ENABLED stays False default; no behavior change)

```bash
ssh root@89.167.116.187 'systemctl stop gecko-pipeline && cd /root/gecko-alpha && git pull && find . -name __pycache__ -exec rm -rf {} + ; systemctl start gecko-pipeline && sleep 5 && systemctl is-active gecko-pipeline' > .ssh_deploy_live_m1.txt 2>&1
```

- [ ] **Step 7: Verify migrations ran**

```bash
ssh root@89.167.116.187 'sqlite3 /root/gecko-alpha/scout.db ".schema venue_health" | head -20' > .ssh_verify_health.txt 2>&1
ssh root@89.167.116.187 'sqlite3 /root/gecko-alpha/scout.db "SELECT * FROM cross_venue_exposure"' > .ssh_verify_view.txt 2>&1
```

- [ ] **Step 8: Memory + todo update**

Write `project_live_m1_shipped_<DATE>.md` memory entry. Update `tasks/todo.md` with M1 prerequisites status + soak windows tied to approval-removal criteria.

---

## Done criteria

- All new tests pass; full regression clean; black clean
- PR merged via squash; deployed to VPS
- 3 migrations ran cleanly (live_eligible, client_order_id, per_venue_services); 5 new tables exist; 2 new SQL views queryable
- LIVE_TRADING_ENABLED defaults False on prod; no signal has live_eligible=1 yet
- BL-055 retirement gate pre-registered in design + commit history
- Operator has all 4 design open questions answered + Binance funded + first signal selected before flipping master kill
- Memory + todo updated
- M1.5 plan can be drafted (next venue: Kraken via kraken-cli OR a CCXT-backed venue) without architectural rework, per pre-registered architectural commitment
