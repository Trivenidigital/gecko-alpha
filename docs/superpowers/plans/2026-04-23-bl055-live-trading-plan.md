# BL-055 Live Trading Execution Core — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the execution-core plumbing for live trading on Binance spot: venue resolution, orderbook walk, pre-trade safety gates, kill switch, shadow evaluator, and a one-week soak in `LIVE_MODE=shadow` before any real orders are sent.

**Architecture:** New `scout/live/` package. Chokepoint at `PaperTrader.open_trade()` fires `asyncio.create_task(LiveEngine.on_paper_trade_opened(trade))` — paper latency preserved, Binance hiccups isolated. Three modes: `paper` (default, no-op), `shadow` (gates + walked-VWAP, no orders), `live` (BL-055 raises NotImplementedError at startup — flip deferred until BL-058). Schema is forward-compatible; aborting PRs B-E does not disturb the paper path.

**Tech Stack:** Python 3.12, aiohttp, aiosqlite, Pydantic v2 BaseSettings (`extra="forbid"`), `asyncio` loops matching existing `briefing_loop` / `overnight_loop` idioms (no APScheduler), structlog, pytest + aioresponses + freezegun.

**Source of truth:** `docs/superpowers/specs/2026-04-22-bl055-live-trading-execution-core-design.md`. Section references below (e.g. §3.1, §5, §10.2) point into that spec — the plan intentionally delegates detailed code bodies to the spec rather than duplicating them. When a step says "per spec §X.Y", read that section before writing code.

---

## File structure

New files in `scout/live/`:

| File | Responsibility |
|---|---|
| `__init__.py` | Package marker, exports `LiveEngine`, `LiveConfig`. |
| `config.py` | `LiveConfig` wrapper — reads typed values from `Settings`, computes fallbacks. |
| `types.py` | Dataclasses/TypedDicts: `ResolvedVenue`, `WalkResult`, `Depth`, `KillState`, `GateResult`. |
| `adapter_base.py` | `ExchangeAdapter` ABC: `resolve_pair`, `fetch_depth`, `fetch_price`, `send_order`, `fetch_exchange_info_row`. |
| `binance_adapter.py` | Concrete `BinanceSpotAdapter`: aiohttp, weight-header governor, retry taxonomy. |
| `orderbook.py` | Pure: `walk_asks(depth, size_usd)` and `walk_bids(depth, qty)`. |
| `resolver.py` | `VenueResolver` + `OverrideStore` — single-flight per-symbol locks, TTL cache, override/exchange-info fallback. |
| `kill_switch.py` | `KillSwitch`: trigger/clear/auto-expired, `compute_kill_duration`. |
| `metrics.py` | UPSERT helpers for `live_metrics_daily`. |
| `gates.py` | Eight pre-trade gates; composes via a single `evaluate()` fn that short-circuits on first fail. |
| `engine.py` | `LiveEngine.on_paper_trade_opened(trade)` — chokepoint dispatcher + DB writer. |
| `shadow_evaluator.py` | Async loop: poll open rows, fetch price, TP/SL/duration exits, transactional daily-cap close. |
| `reconciliation.py` | Boot-time open-row recovery; `live_boot_reconciliation_done` always fires. |
| `cli_kill.py` | `python -m scout.live.cli_kill --on/--off` manual trigger. |
| `loops.py` | `shadow_evaluator_loop`, `override_staleness_loop`, `live_metrics_rollup_loop`. |

Modified files:

| File | Change |
|---|---|
| `scout/db.py` | New migration adding §3.1 tables + indexes; `connect()` sets `foreign_keys=ON` + `journal_mode=WAL` on every connection. |
| `scout/config.py` | Add `LIVE_MODE` + ~15 `LIVE_*` fields, two `@computed_field` properties, `model_config = ConfigDict(extra="forbid")`. |
| `scout/trading/paper.py` | Constructor accepts `live_engine: LiveEngine | None = None`; `execute_buy` tail dispatches async task when engine is non-None and signal is allowlisted. Bounded pending-task set. |
| `scout/main.py` | Build `LiveConfig`, guard live-mode startup (raise NotImplementedError for balance gate), construct `LiveEngine` when mode ∈ {shadow, live}, schedule three loops, inject engine into `PaperTrader`. Add `--check-config` CLI flag. |
| `.env.example` | Append commented-out LIVE_* block (no credentials). |
| `docs/live-mode-setup.md` | New doc: API-key creation, IP allowlist, `--check-config`, "restart not reload" semantics. |

Test files:

| File | Targets |
|---|---|
| `tests/live/test_config.py` | LiveConfig fallback logic, size map parse, allowlist set, `extra="forbid"` rejection. |
| `tests/live/test_binance_adapter.py` | exchangeInfo happy/404, depth, ticker, `X-MBX-USED-WEIGHT-1M` header → semaphore shrink, 429 Retry-After. |
| `tests/live/test_orderbook.py` | `walk_asks` VWAP, insufficient-liquidity, slippage bps math; symmetrical `walk_bids`. |
| `tests/live/test_venue_resolver.py` | Single-flight (N=10 concurrent resolve → 1 HTTP call), positive/negative TTL with freezegun, override lookup with `disabled=1` respected. |
| `tests/live/test_kill_switch.py` | trigger persists kill_events + flips live_control; clear/auto-expired transitions; `compute_kill_duration` 4 parametrized cases. |
| `tests/live/test_pretrade_gates.py` | Parametrized over `SHADOW_REJECT_REASONS`; meta-test asserts test-list ⟷ CHECK constraint symmetry. |
| `tests/live/test_shadow_evaluator.py` | TP / SL / duration exits; review_retries; transactional daily-cap race (two concurrent closes → one kill). |
| `tests/live/test_live_engine.py` | Handoff matrix: not-allowlisted (skip no-row), killed (skip no-row), resolver miss (reject `no_venue`), each gate rejection, happy path opens row. |
| `tests/live/test_metrics.py` | UPSERT idempotency, counter increment, date boundary rollover. |
| `tests/live/test_reconciliation.py` | Zero-row boot fires `live_boot_reconciliation_done`; open row with crossed TP → closed_via_reconciliation + WARN. |
| `tests/live/test_db_migration.py` | Fresh DB builds all tables/indexes; FK RESTRICT blocks `DELETE FROM paper_trades`; `foreign_keys=ON` persisted per connection. |
| `tests/integration/test_live_shadow_loop.py` | Six canonical flows from spec §11.6. |

---

## Rollout (PR sequence from spec §12.2)

This plan produces ONE feature branch `feat/bl055-live-trading-core` that will ship as a **single PR**, but tasks are grouped into the five logical PRs from the spec so implementers can easily carve the branch into stacked PRs if they prefer. (Given single-maintainer repo and the autonomous execution directive, single PR is the default.)

- **Group A** (Tasks 1-5): Schema + config + pragmas. Forward-compatible. Zero behavior change.
- **Group B** (Tasks 6-9): Adapter base + Binance adapter + orderbook walker + resolver. Not imported yet.
- **Group C** (Tasks 10-13): Kill switch + metrics + gates. Not imported yet.
- **Group D** (Tasks 14-18): Engine + shadow evaluator + reconciliation + CLI + async loops. Not imported yet.
- **Group E** (Tasks 19-22): Main wiring + PaperTrader chokepoint + `--check-config` + integration tests + CI test-count gate.

Every task is: write failing test → run to see it fail → implement → run to see it pass → commit. Default TDD rhythm.

---

## Group A — Schema foundation

### Task 1: Add BL-055 schema migration

**Files:**
- Modify: `scout/db.py` (add `_migrate_live_trading_schema` per spec §3.1)
- Create: `tests/live/__init__.py` (empty)
- Create: `tests/live/test_db_migration.py`

- [ ] **Step 1: Create tests/live/__init__.py**

Create empty file so pytest discovers the directory.

```bash
: > tests/live/__init__.py
```

- [ ] **Step 2: Write failing tests for schema**

```python
# tests/live/test_db_migration.py
"""Tests for BL-055 live-trading schema migration (spec §3.1)."""

from __future__ import annotations

import aiosqlite
import pytest

from scout.db import Database


async def _tables(conn) -> set[str]:
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    return {row[0] for row in await cur.fetchall()}


async def _indexes(conn) -> set[str]:
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    )
    return {row[0] for row in await cur.fetchall()}


async def test_migration_creates_all_bl055_tables(tmp_path):
    db = Database(tmp_path / "gecko.db")
    await db.initialize()
    tables = await _tables(db._conn)
    assert {
        "shadow_trades",
        "live_trades",
        "kill_events",
        "live_control",
        "venue_overrides",
        "resolver_cache",
        "live_metrics_daily",
    } <= tables, f"missing BL-055 tables; got {tables}"
    await db.close()


async def test_migration_creates_bl055_indexes(tmp_path):
    db = Database(tmp_path / "gecko.db")
    await db.initialize()
    idx = await _indexes(db._conn)
    assert "idx_shadow_status_evaluated" in idx
    assert "idx_shadow_closed_at_utc" in idx
    assert "idx_kill_events_active" in idx
    await db.close()


async def test_live_control_has_singleton_row(tmp_path):
    db = Database(tmp_path / "gecko.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT id, active_kill_event_id FROM live_control"
    )
    rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 1
    assert rows[0][1] is None
    await db.close()


async def test_live_control_rejects_non_one_id(tmp_path):
    db = Database(tmp_path / "gecko.db")
    await db.initialize()
    with pytest.raises(aiosqlite.IntegrityError):
        await db._conn.execute("INSERT INTO live_control (id) VALUES (2)")
        await db._conn.commit()
    await db.close()


async def test_shadow_trades_check_constraints(tmp_path):
    db = Database(tmp_path / "gecko.db")
    await db.initialize()
    with pytest.raises(aiosqlite.IntegrityError):
        await db._conn.execute(
            "INSERT INTO shadow_trades "
            "(paper_trade_id, coin_id, symbol, venue, pair, signal_type, "
            " size_usd, status, created_at) "
            "VALUES (1, 'c', 's', 'binance', 'SUSDT', 'first_signal', "
            "'100', 'BAD_STATUS', '2026-04-23T00:00:00Z')"
        )
        await db._conn.commit()
    await db.close()


async def test_paper_trades_fk_restrict(tmp_path):
    """Spec §3.2: paper_trades is append-only via FK ON DELETE RESTRICT.
    Attempt to delete a paper_trades row while a shadow_trades row references
    it must fail."""
    db = Database(tmp_path / "gecko.db")
    await db.initialize()
    # Seed a paper_trades row and a shadow_trades row pointing to it.
    await db._conn.execute(
        "INSERT INTO paper_trades "
        "(token_id, symbol, name, chain, signal_type, signal_data, "
        " entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price, "
        " status, opened_at) "
        "VALUES ('c','S','N','eth','first_signal','{}',1,100,100,40,20,1.4,0.8,"
        "'open','2026-04-23T00:00:00Z')"
    )
    paper_id = (await (await db._conn.execute("SELECT last_insert_rowid()")).fetchone())[0]
    await db._conn.execute(
        "INSERT INTO shadow_trades "
        "(paper_trade_id, coin_id, symbol, venue, pair, signal_type, size_usd, "
        " status, created_at) "
        "VALUES (?, 'c','S','binance','SUSDT','first_signal','100','open', "
        "'2026-04-23T00:00:00Z')",
        (paper_id,),
    )
    await db._conn.commit()
    with pytest.raises(aiosqlite.IntegrityError):
        await db._conn.execute(
            "DELETE FROM paper_trades WHERE id = ?", (paper_id,)
        )
        await db._conn.commit()
    await db.close()
```

- [ ] **Step 3: Run tests — expect failure**

```bash
uv run pytest tests/live/test_db_migration.py -v
```
Expected: all tests fail with `sqlite3.OperationalError: no such table: shadow_trades` (or similar).

- [ ] **Step 4: Implement the migration**

Append a new migration method. Add exactly the tables from spec §3.1. Paste each `CREATE TABLE` block from the spec verbatim into `await self._conn.executescript(...)` inside `_migrate_live_trading_schema`. Wire the call from `initialize()` AFTER `_migrate_feedback_loop_schema`.

```python
# scout/db.py — inside class Database

async def initialize(self) -> None:
    self._conn = await aiosqlite.connect(self._db_path)
    self._conn.row_factory = aiosqlite.Row
    self._txn_lock = asyncio.Lock()
    await self._conn.execute("PRAGMA journal_mode=WAL")
    await self._conn.execute("PRAGMA foreign_keys=ON")   # Task 2 also needs this
    await self._create_tables()
    await self._migrate_feedback_loop_schema()
    await self._migrate_live_trading_schema()            # NEW

async def _migrate_live_trading_schema(self) -> None:
    """BL-055: shadow/live ledgers, kill events, venue overrides, resolver cache,
    daily metrics. One atomic migration. Idempotent via IF NOT EXISTS.

    Note: paper_trades becomes append-only by contract (FK RESTRICT). Existing
    rows are untouched; only new DELETE attempts are blocked.
    """
    if self._conn is None:
        raise RuntimeError("Database not initialized.")
    assert self._txn_lock is not None
    async with self._txn_lock:
        try:
            await self._conn.executescript(
                # Paste §3.1 blocks verbatim here — shadow_trades, live_trades,
                # kill_events, live_control, venue_overrides, resolver_cache,
                # live_metrics_daily, plus the three indexes.
                # Wrap every table with CREATE TABLE IF NOT EXISTS and every
                # index with CREATE INDEX IF NOT EXISTS so re-runs are no-ops.
                """<<< paste spec §3.1 CREATE TABLE + CREATE INDEX blocks, s/CREATE TABLE /CREATE TABLE IF NOT EXISTS /g, s/CREATE INDEX /CREATE INDEX IF NOT EXISTS /g >>>"""
            )
            # Seed live_control with id=1 ONLY if not already present (idempotent).
            await self._conn.execute(
                "INSERT OR IGNORE INTO live_control (id, active_kill_event_id) "
                "VALUES (1, NULL)"
            )
            await self._conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (20260423, datetime.now(timezone.utc).isoformat(),
                 "bl055_live_trading_v1"),
            )
            await self._conn.commit()
        except Exception:
            try:
                await self._conn.rollback()
            except Exception:
                pass
            raise
```

**CRITICAL — learned from BL-060 (commit a422ef7):** When a new column references are involved, index creation on that column must live in the migration step, never in `_create_tables`. For BL-055 all new tables are added in this single migration — no index contamination risk.

- [ ] **Step 5: Run tests — expect pass**

```bash
uv run pytest tests/live/test_db_migration.py -v
```
Expected: all 6 tests pass.

- [ ] **Step 6: Commit**

```bash
git add scout/db.py tests/live/__init__.py tests/live/test_db_migration.py
git commit -m "feat(bl055): schema migration for shadow/live/kill/override tables"
```

---

### Task 2: Enforce `PRAGMA foreign_keys=ON` on every connection

**Files:**
- Modify: `scout/db.py::initialize()` (ensure pragma is set)
- Modify: `tests/live/test_db_migration.py` (assert FK enforcement)

- [ ] **Step 1: Write failing test that requires foreign_keys pragma**

```python
# tests/live/test_db_migration.py — append
async def test_connect_sets_foreign_keys_pragma(tmp_path):
    db = Database(tmp_path / "gecko.db")
    await db.initialize()
    cur = await db._conn.execute("PRAGMA foreign_keys")
    row = await cur.fetchone()
    assert row[0] == 1, "foreign_keys must be ON after initialize()"
    await db.close()
```

- [ ] **Step 2: Run — may already pass if Task 1 added the pragma**

```bash
uv run pytest tests/live/test_db_migration.py::test_connect_sets_foreign_keys_pragma -v
```
Expected: pass after Task 1. If not, add `PRAGMA foreign_keys=ON` to `initialize()`.

- [ ] **Step 3: Audit callers of `aiosqlite.connect` outside `Database.initialize`**

```bash
grep -n "aiosqlite.connect" scout/ -r
```
Document findings. For any helper that opens a raw connection and does DDL/DML: add `await conn.execute("PRAGMA foreign_keys=ON")` immediately after connect. Most code goes through `Database`, so usually only tests/scripts are affected. If nothing outside Database exists, skip this step and note it in the commit message.

- [ ] **Step 4: Commit (if any callers changed)**

```bash
git add -u
git commit -m "fix(db): PRAGMA foreign_keys=ON on every connection (spec §3.2)"
```

---

### Task 3: `LIVE_*` settings + `extra="forbid"` + computed fields

**Files:**
- Modify: `scout/config.py`
- Create: `tests/live/test_config.py`

- [ ] **Step 1: Write failing tests for Settings fields**

```python
# tests/live/test_config.py
"""Tests for BL-055 LIVE_* settings (spec §4)."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from scout.config import Settings


def _base_kwargs(**over):
    kw = dict(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
    )
    kw.update(over)
    return kw


def test_live_mode_defaults_to_paper():
    s = Settings(**_base_kwargs())
    assert s.LIVE_MODE == "paper"


def test_live_mode_accepts_shadow_and_live():
    for mode in ("paper", "shadow", "live"):
        s = Settings(**_base_kwargs(LIVE_MODE=mode))
        assert s.LIVE_MODE == mode


def test_live_mode_rejects_unknown_value():
    with pytest.raises(ValidationError):
        Settings(**_base_kwargs(LIVE_MODE="yolo"))


def test_live_sizing_defaults():
    s = Settings(**_base_kwargs())
    assert s.LIVE_TRADE_AMOUNT_USD == Decimal("100")
    assert s.LIVE_SIGNAL_SIZES == ""
    assert s.live_signal_sizes_map == {}


def test_live_signal_sizes_map_parses_csv():
    s = Settings(**_base_kwargs(
        LIVE_SIGNAL_SIZES="first_signal=50,gainers_early=75"
    ))
    assert s.live_signal_sizes_map == {
        "first_signal": Decimal("50"),
        "gainers_early": Decimal("75"),
    }


def test_live_signal_sizes_map_rejects_malformed():
    s = Settings(**_base_kwargs(LIVE_SIGNAL_SIZES="broken_no_equals"))
    with pytest.raises(ValueError, match="malformed"):
        _ = s.live_signal_sizes_map


def test_live_signal_allowlist_set_lowercased_and_trimmed():
    s = Settings(**_base_kwargs(
        LIVE_SIGNAL_ALLOWLIST=" First_Signal , gainers_early "
    ))
    assert s.live_signal_allowlist_set == frozenset(
        {"first_signal", "gainers_early"}
    )


def test_live_risk_gate_defaults():
    s = Settings(**_base_kwargs())
    assert s.LIVE_SLIPPAGE_BPS_CAP == 50
    assert s.LIVE_DEPTH_HEALTH_MULTIPLIER == Decimal("3")
    assert s.LIVE_DAILY_LOSS_CAP_USD == Decimal("50")
    assert s.LIVE_MAX_EXPOSURE_USD == Decimal("500")
    assert s.LIVE_MAX_OPEN_POSITIONS == 5


def test_settings_extra_forbid_rejects_typo():
    """Spec §4.5: extra='forbid' catches LIVE_* typos at startup."""
    with pytest.raises(ValidationError):
        Settings(**_base_kwargs(LIVE_MDOE="shadow"))  # typo: MDOE
```

- [ ] **Step 2: Run — expect failures (fields missing)**

```bash
uv run pytest tests/live/test_config.py -v
```

- [ ] **Step 3: Add the fields, computed fields, and `extra="forbid"`**

Paste the full field block from spec §4 into `scout/config.py`, placing it near the other trading-related settings. Add both `@computed_field` properties verbatim from §4.1. Change `model_config` on Settings to set `extra="forbid"` (may need merge with existing config).

```python
# scout/config.py — add near existing PAPER_* fields
from typing import Literal
from decimal import Decimal
from pydantic import SecretStr, computed_field, ConfigDict

# Inside Settings:
LIVE_MODE: Literal["paper", "shadow", "live"] = "paper"

LIVE_TRADE_AMOUNT_USD: Decimal = Decimal("100")
LIVE_SIGNAL_SIZES: str = ""

LIVE_TP_PCT: Decimal | None = None
LIVE_SL_PCT: Decimal | None = None
LIVE_MAX_DURATION_HOURS: int | None = None

LIVE_SLIPPAGE_BPS_CAP: int = 50
LIVE_DEPTH_HEALTH_MULTIPLIER: Decimal = Decimal("3")
LIVE_VENUE_PREFERENCE: str = "binance"

LIVE_DAILY_LOSS_CAP_USD: Decimal = Decimal("50")
LIVE_MAX_EXPOSURE_USD: Decimal = Decimal("500")
LIVE_MAX_OPEN_POSITIONS: int = 5

LIVE_SIGNAL_ALLOWLIST: str = ""

BINANCE_API_KEY: SecretStr | None = None
BINANCE_API_SECRET: SecretStr | None = None

# Add BOTH computed_field properties from spec §4.1 verbatim
@computed_field
@property
def live_signal_allowlist_set(self) -> frozenset[str]:
    ...   # paste from spec §4.1

@computed_field
@property
def live_signal_sizes_map(self) -> dict[str, "Decimal"]:
    ...   # paste from spec §4.1

# Update model_config — MERGE with whatever's already there:
model_config = ConfigDict(
    # ... existing keys ...
    extra="forbid",
)
```

- [ ] **Step 4: F1 pre-flight (spec §4.5) — run on VPS BEFORE merge**

```bash
ssh srilu-vps 'cd /root/gecko-alpha && uv run python -c "from scout.config import Settings; Settings(); print(\"ok\")"' > .ssh_f1.txt 2>&1
```
Read `.ssh_f1.txt`. If `ok` prints, safe to ship. If ValidationError fires with an unknown-field name → **STOP**: edit `/root/gecko-alpha/.env` on VPS first, then re-run. Record the result in the PR description.

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/live/test_config.py tests/test_config.py -v
```
Expected: new tests pass. Existing `tests/test_config.py` must still pass — `extra="forbid"` can regress existing fixtures if they pass unknown kwargs. Fix any breakage by removing stray kwargs in fixtures.

- [ ] **Step 6: Commit**

```bash
git add scout/config.py tests/live/test_config.py
git commit -m "feat(bl055): LIVE_* settings + extra=forbid + computed_field helpers"
```

---

### Task 4: `scout/live/config.py` — `LiveConfig` wrapper

**Files:**
- Create: `scout/live/__init__.py`
- Create: `scout/live/config.py`
- Modify: `tests/live/test_config.py`

- [ ] **Step 1: Create package marker**

```bash
: > scout/live/__init__.py
```

- [ ] **Step 2: Write failing tests for `LiveConfig`**

```python
# tests/live/test_config.py — append
from decimal import Decimal

from scout.config import Settings
from scout.live.config import LiveConfig


def _make(**over):
    return LiveConfig(Settings(**{
        "TELEGRAM_BOT_TOKEN": "t",
        "TELEGRAM_CHAT_ID": "c",
        "ANTHROPIC_API_KEY": "k",
        **over,
    }))


def test_live_config_mode_passthrough():
    lc = _make(LIVE_MODE="shadow")
    assert lc.mode == "shadow"


def test_live_config_is_signal_enabled_is_case_insensitive():
    lc = _make(LIVE_SIGNAL_ALLOWLIST="first_signal,gainers_early")
    assert lc.is_signal_enabled("FIRST_SIGNAL") is True
    assert lc.is_signal_enabled("first_signal") is True
    assert lc.is_signal_enabled("volume_spike") is False


def test_resolve_size_usd_falls_back_to_default():
    lc = _make(LIVE_TRADE_AMOUNT_USD=Decimal("100"),
               LIVE_SIGNAL_SIZES="first_signal=50")
    assert lc.resolve_size_usd("first_signal") == Decimal("50")
    assert lc.resolve_size_usd("volume_spike") == Decimal("100")


def test_resolve_tp_sl_duration_fall_back_to_paper_values():
    lc = _make(
        PAPER_TP_PCT=Decimal("40"),
        PAPER_SL_PCT=Decimal("20"),
        PAPER_MAX_DURATION_HOURS=24,
        LIVE_TP_PCT=None,
        LIVE_SL_PCT=Decimal("15"),
        LIVE_MAX_DURATION_HOURS=None,
    )
    assert lc.resolve_tp_pct() == Decimal("40")
    assert lc.resolve_sl_pct() == Decimal("15")
    assert lc.resolve_max_duration_hours() == 24
```

- [ ] **Step 3: Implement `LiveConfig` verbatim from spec §4.2**

```python
# scout/live/config.py
"""LiveConfig — typed wrapper over Settings for live-trading knobs.

Single source of truth for fallback logic (LIVE_* → PAPER_* → default).
Consumers never read Settings directly.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from scout.config import Settings


class LiveConfig:
    def __init__(self, settings: Settings) -> None:
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
        return (
            self._s.LIVE_TP_PCT
            if self._s.LIVE_TP_PCT is not None
            else self._s.PAPER_TP_PCT
        )

    def resolve_sl_pct(self) -> Decimal:
        return (
            self._s.LIVE_SL_PCT
            if self._s.LIVE_SL_PCT is not None
            else self._s.PAPER_SL_PCT
        )

    def resolve_max_duration_hours(self) -> int:
        return (
            self._s.LIVE_MAX_DURATION_HOURS
            or self._s.PAPER_MAX_DURATION_HOURS
        )
```

- [ ] **Step 4: Run — expect pass**

```bash
uv run pytest tests/live/test_config.py -v
```

- [ ] **Step 5: Commit**

```bash
git add scout/live/__init__.py scout/live/config.py tests/live/test_config.py
git commit -m "feat(bl055): LiveConfig wrapper with fallback logic"
```

---

### Task 5: `.env.example` additions + `docs/live-mode-setup.md`

**Files:**
- Modify: `.env.example`
- Create: `docs/live-mode-setup.md`

- [ ] **Step 1: Append LIVE_* block to `.env.example`**

Paste spec §4.4 block verbatim (no credentials). Leave all lines commented.

- [ ] **Step 2: Write `docs/live-mode-setup.md`**

Sections required (spec I3):

1. **DO NOT COMMIT** banner at top.
2. API-key creation: Binance spot-only scope, no withdrawal, no margin, no futures, IP-restricted to VPS.
3. `--check-config` usage: `uv run python -m scout.main --check-config` — diff against expected before flipping mode.
4. "Restart not reload" semantics (spec §10.6): config changes require `systemctl restart gecko-pipeline`, NOT `systemctl reload`.
5. Flip-to-live checklist reference — link to spec §11.8.

Keep it under 100 lines. It's an ops doc, not a design doc.

- [ ] **Step 3: Commit**

```bash
git add .env.example docs/live-mode-setup.md
git commit -m "docs(bl055): LIVE_* env example + live-mode-setup.md"
```

---

## Group B — Adapter + resolver

### Task 6: `scout/live/types.py` + `scout/live/adapter_base.py`

**Files:**
- Create: `scout/live/types.py`
- Create: `scout/live/adapter_base.py`

- [ ] **Step 1: Write `scout/live/types.py`**

Minimal dataclasses for adapter/resolver/gate interop. No business logic.

```python
# scout/live/types.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class DepthLevel:
    price: Decimal
    qty: Decimal


@dataclass(frozen=True)
class Depth:
    pair: str
    bids: tuple[DepthLevel, ...]     # descending
    asks: tuple[DepthLevel, ...]     # ascending
    mid: Decimal
    fetched_at: datetime


@dataclass(frozen=True)
class WalkResult:
    vwap: Decimal | None             # None if insufficient_liquidity
    filled_qty: Decimal
    filled_notional: Decimal
    slippage_bps: int | None
    insufficient_liquidity: bool


@dataclass(frozen=True)
class ResolvedVenue:
    symbol: str
    venue: str
    pair: str
    source: Literal["cache", "override_table", "binance_exchangeinfo"]


@dataclass(frozen=True)
class KillState:
    kill_event_id: int
    killed_until: datetime
    reason: str
    triggered_by: Literal["daily_loss_cap", "manual", "ops_maintenance"]


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reject_reason: str | None = None  # matches §3.1 CHECK enum when non-None
    detail: str | None = None
```

- [ ] **Step 2: Write `scout/live/adapter_base.py`**

```python
# scout/live/adapter_base.py
"""Minimal ExchangeAdapter ABC (spec §2.1). Binance is v1's only impl."""
from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from scout.live.types import Depth


class ExchangeAdapter(ABC):
    venue_name: str

    @abstractmethod
    async def fetch_exchange_info_row(self, pair: str) -> dict | None:
        """Return parsed exchangeInfo row for `pair` or None on 404/delisted."""

    @abstractmethod
    async def resolve_pair_for_symbol(self, symbol: str) -> str | None:
        """Search exchangeInfo for symbol with quote=USDT, status=TRADING."""

    @abstractmethod
    async def fetch_depth(self, pair: str, limit: int = 100) -> Depth:
        """Return a Depth snapshot. Raises for transient failures."""

    @abstractmethod
    async def fetch_price(self, pair: str) -> Decimal:
        """Spot mid price via /ticker/price (weight=1)."""

    @abstractmethod
    async def send_order(self, *, pair: str, side: str, size_usd: Decimal) -> dict:
        """Live-mode real order. BL-055 implementations may raise
        NotImplementedError since live mode itself is gated at startup."""
```

- [ ] **Step 3: Commit**

```bash
git add scout/live/types.py scout/live/adapter_base.py
git commit -m "feat(bl055): ExchangeAdapter ABC + live package types"
```

---

### Task 7: `scout/live/binance_adapter.py` with weight-header governor

**Files:**
- Create: `scout/live/binance_adapter.py`
- Create: `tests/live/test_binance_adapter.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/live/test_binance_adapter.py
"""Tests for BinanceSpotAdapter (spec §7, §8, §9)."""

from decimal import Decimal

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.config import Settings
from scout.live.binance_adapter import BinanceSpotAdapter


def _settings():
    return Settings(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
    )


async def test_fetch_exchange_info_row_happy_path():
    with aioresponses() as m:
        m.get(
            "https://api.binance.com/api/v3/exchangeInfo?symbol=WBTCUSDT",
            payload={
                "symbols": [{
                    "symbol": "WBTCUSDT",
                    "status": "TRADING",
                    "baseAsset": "WBTC",
                    "quoteAsset": "USDT",
                }]
            },
            headers={"X-MBX-USED-WEIGHT-1M": "12"},
        )
        adapter = BinanceSpotAdapter(_settings())
        row = await adapter.fetch_exchange_info_row("WBTCUSDT")
        assert row is not None and row["status"] == "TRADING"
        await adapter.close()


async def test_fetch_exchange_info_row_returns_none_on_404():
    with aioresponses() as m:
        m.get(
            "https://api.binance.com/api/v3/exchangeInfo?symbol=ZZZZZUSDT",
            status=400,
            payload={"code": -1121, "msg": "Invalid symbol."},
        )
        adapter = BinanceSpotAdapter(_settings())
        assert await adapter.fetch_exchange_info_row("ZZZZZUSDT") is None
        await adapter.close()


async def test_fetch_depth_returns_parsed_depth():
    with aioresponses() as m:
        m.get(
            "https://api.binance.com/api/v3/depth?symbol=WBTCUSDT&limit=100",
            payload={
                "bids": [["100.0", "1.0"], ["99.5", "2.0"]],
                "asks": [["100.5", "1.0"], ["101.0", "2.0"]],
            },
            headers={"X-MBX-USED-WEIGHT-1M": "20"},
        )
        adapter = BinanceSpotAdapter(_settings())
        depth = await adapter.fetch_depth("WBTCUSDT")
        assert depth.pair == "WBTCUSDT"
        assert depth.asks[0].price == Decimal("100.5")
        assert depth.bids[0].price == Decimal("100.0")
        assert depth.mid == Decimal("100.25")
        await adapter.close()


async def test_semaphore_shrinks_at_80pct_weight():
    """Spec §9.1: when used weight >= 960 (80%), semaphore drops to 3."""
    with aioresponses() as m:
        m.get(
            "https://api.binance.com/api/v3/depth?symbol=X&limit=100",
            payload={"bids": [["1","1"]], "asks": [["1.01","1"]]},
            headers={"X-MBX-USED-WEIGHT-1M": "965"},
        )
        adapter = BinanceSpotAdapter(_settings())
        await adapter.fetch_depth("X")
        assert adapter._current_semaphore_cap == 3
        await adapter.close()


async def test_429_respects_retry_after():
    """Spec §9.1: 429 → respect Retry-After, no immediate retry."""
    with aioresponses() as m:
        m.get(
            "https://api.binance.com/api/v3/ticker/price?symbol=X",
            status=429,
            headers={"Retry-After": "5"},
        )
        m.get(
            "https://api.binance.com/api/v3/ticker/price?symbol=X",
            payload={"symbol": "X", "price": "1.0"},
            headers={"X-MBX-USED-WEIGHT-1M": "10"},
        )
        adapter = BinanceSpotAdapter(_settings())
        with pytest.raises(aiohttp.ClientResponseError):
            await adapter.fetch_price("X")
        await adapter.close()
```

- [ ] **Step 2: Run — expect import error**

```bash
uv run pytest tests/live/test_binance_adapter.py -v
```

- [ ] **Step 3: Implement `BinanceSpotAdapter`**

Scope is large — implement methods in this order, committing between major ones if useful:
1. `__init__(settings)` — create shared `aiohttp.ClientSession` with 10s timeout.
2. `_http_get(path, params)` — reads `X-MBX-USED-WEIGHT-1M`, calls `_update_weight_governor`.
3. `_update_weight_governor(weight)` — updates `self._current_semaphore_cap` per §9.1 thresholds.
4. `fetch_exchange_info_row(pair)` — GET /api/v3/exchangeInfo?symbol=…; parse single-item `symbols` array; None on 400/-1121.
5. `resolve_pair_for_symbol(symbol)` — probe `{symbol}USDT`; if exchangeInfo returns a row with `status=TRADING` and `quoteAsset=USDT`, return pair; else None.
6. `fetch_depth(pair, limit=100)` — parse bids/asks, compute mid = (best_bid + best_ask) / 2.
7. `fetch_price(pair)` — GET /api/v3/ticker/price.
8. `send_order(…)` — BL-055 raises NotImplementedError.
9. `close()` — close the ClientSession.

Use `Decimal` everywhere — never `float`. The 429 handling can initially just surface the `aiohttp.ClientResponseError` (let caller decide retry). Retry policy lives in the caller per spec §10.1.

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/live/test_binance_adapter.py -v
```
Expected: all 5 pass.

- [ ] **Step 5: Commit**

```bash
git add scout/live/binance_adapter.py tests/live/test_binance_adapter.py
git commit -m "feat(bl055): BinanceSpotAdapter with weight-header governor"
```

---

### Task 8: `scout/live/orderbook.py` — VWAP walker

**Files:**
- Create: `scout/live/orderbook.py`
- Create: `tests/live/test_orderbook.py`

- [ ] **Step 1: Write tests**

```python
# tests/live/test_orderbook.py
from decimal import Decimal
from datetime import datetime, timezone

from scout.live.types import Depth, DepthLevel
from scout.live.orderbook import walk_asks, walk_bids


def _asks(levels):
    return tuple(DepthLevel(price=Decimal(p), qty=Decimal(q)) for p, q in levels)


def _depth(bids, asks, mid):
    return Depth(
        pair="X",
        bids=_asks(bids),
        asks=_asks(asks),
        mid=Decimal(mid),
        fetched_at=datetime.now(timezone.utc),
    )


def test_walk_asks_vwap_single_level():
    d = _depth([], [("100", "10")], "100")
    r = walk_asks(d, Decimal("500"))
    assert not r.insufficient_liquidity
    assert r.filled_qty == Decimal("5")
    assert r.vwap == Decimal("100")
    assert r.slippage_bps == 0


def test_walk_asks_vwap_two_levels():
    d = _depth([], [("100", "1"), ("110", "10")], "100")
    # Need $200 → $100 from level 1 (fills 1 unit), $100 from level 2 (fills 10/11)
    r = walk_asks(d, Decimal("200"))
    assert r.vwap > Decimal("100")
    assert r.vwap < Decimal("110")
    assert r.slippage_bps > 0


def test_walk_asks_flags_insufficient_liquidity():
    d = _depth([], [("100", "0.5")], "100")
    r = walk_asks(d, Decimal("200"))
    assert r.insufficient_liquidity
    assert r.vwap is None


def test_walk_bids_symmetrical():
    d = _depth([("100", "10"), ("90", "5")], [], "100")
    r = walk_bids(d, Decimal("12"))
    # All 10 units from top bid, 2 units from second
    assert r.filled_qty == Decimal("12")
    assert r.vwap < Decimal("100")
```

- [ ] **Step 2: Implement walker**

```python
# scout/live/orderbook.py
"""Pure VWAP walker. No I/O (spec §8)."""
from __future__ import annotations

from decimal import Decimal

from scout.live.types import Depth, WalkResult


def walk_asks(depth: Depth, size_usd: Decimal) -> WalkResult:
    """Walk ask side accumulating notional until >= size_usd."""
    remaining = size_usd
    filled_notional = Decimal(0)
    filled_qty = Decimal(0)
    for level in depth.asks:
        level_notional = level.price * level.qty
        take_notional = min(level_notional, remaining)
        take_qty = take_notional / level.price
        filled_notional += take_notional
        filled_qty += take_qty
        remaining -= take_notional
        if remaining <= 0:
            break
    if remaining > 0:
        return WalkResult(
            vwap=None, filled_qty=filled_qty, filled_notional=filled_notional,
            slippage_bps=None, insufficient_liquidity=True,
        )
    vwap = filled_notional / filled_qty
    bps = int((vwap - depth.mid) / depth.mid * Decimal(10000))
    return WalkResult(
        vwap=vwap, filled_qty=filled_qty, filled_notional=filled_notional,
        slippage_bps=bps, insufficient_liquidity=False,
    )


def walk_bids(depth: Depth, qty: Decimal) -> WalkResult:
    """Walk bid side accumulating qty until >= qty."""
    remaining = qty
    filled_notional = Decimal(0)
    filled_qty = Decimal(0)
    for level in depth.bids:
        take_qty = min(level.qty, remaining)
        filled_qty += take_qty
        filled_notional += take_qty * level.price
        remaining -= take_qty
        if remaining <= 0:
            break
    if remaining > 0:
        return WalkResult(
            vwap=None, filled_qty=filled_qty, filled_notional=filled_notional,
            slippage_bps=None, insufficient_liquidity=True,
        )
    vwap = filled_notional / filled_qty
    bps = int((depth.mid - vwap) / depth.mid * Decimal(10000))
    return WalkResult(
        vwap=vwap, filled_qty=filled_qty, filled_notional=filled_notional,
        slippage_bps=bps, insufficient_liquidity=False,
    )
```

- [ ] **Step 3: Run tests + commit**

```bash
uv run pytest tests/live/test_orderbook.py -v
git add scout/live/orderbook.py tests/live/test_orderbook.py
git commit -m "feat(bl055): pure VWAP walker (walk_asks/walk_bids)"
```

---

### Task 9: `scout/live/resolver.py` — single-flight + TTL + override

**Files:**
- Create: `scout/live/resolver.py`
- Create: `tests/live/test_venue_resolver.py`

- [ ] **Step 1: Write tests (single-flight FIRST — spec §11.5)**

```python
# tests/live/test_venue_resolver.py
"""Tests for VenueResolver (spec §7). Single-flight is FIRST test per §11.5."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from freezegun import freeze_time

from scout.db import Database
from scout.live.resolver import VenueResolver, OverrideStore


async def test_single_flight_one_miss_one_binance_call(tmp_path):
    """Spec §7.1: N=10 concurrent resolve('WBTC') during cache miss must issue
    ONE Binance exchangeInfo call. Thundering-herd protection."""
    db = Database(tmp_path / "t.db"); await db.initialize()
    adapter = AsyncMock()
    adapter.resolve_pair_for_symbol = AsyncMock(return_value="WBTCUSDT")
    resolver = VenueResolver(
        binance_adapter=adapter,
        override_store=OverrideStore(db),
        positive_ttl=timedelta(hours=1),
        negative_ttl=timedelta(seconds=60),
        db=db,
    )
    results = await asyncio.gather(*[resolver.resolve("WBTC") for _ in range(10)])
    assert all(r is not None and r.pair == "WBTCUSDT" for r in results)
    assert adapter.resolve_pair_for_symbol.call_count == 1
    await db.close()


async def test_resolver_cache_hit_skips_binance(tmp_path):
    db = Database(tmp_path / "t.db"); await db.initialize()
    adapter = AsyncMock()
    adapter.resolve_pair_for_symbol = AsyncMock(return_value="WBTCUSDT")
    resolver = VenueResolver(
        binance_adapter=adapter, override_store=OverrideStore(db),
        positive_ttl=timedelta(hours=1), negative_ttl=timedelta(seconds=60),
        db=db,
    )
    await resolver.resolve("WBTC")
    await resolver.resolve("WBTC")
    assert adapter.resolve_pair_for_symbol.call_count == 1
    await db.close()


async def test_override_row_overrides_exchange_info(tmp_path):
    db = Database(tmp_path / "t.db"); await db.initialize()
    await db._conn.execute(
        "INSERT INTO venue_overrides (symbol, venue, pair, note, disabled, "
        "created_at, updated_at) "
        "VALUES ('WBTC','binance','WBTCUSDT','manual','0','2026-04-23T00Z','2026-04-23T00Z')"
    )
    await db._conn.commit()
    adapter = AsyncMock()
    adapter.resolve_pair_for_symbol = AsyncMock(return_value="ZZZUSDT")
    resolver = VenueResolver(
        binance_adapter=adapter, override_store=OverrideStore(db),
        positive_ttl=timedelta(hours=1), negative_ttl=timedelta(seconds=60),
        db=db,
    )
    r = await resolver.resolve("WBTC")
    assert r.pair == "WBTCUSDT" and r.source == "override_table"
    assert adapter.resolve_pair_for_symbol.call_count == 0
    await db.close()


async def test_override_disabled_returns_none_with_disabled_flag(tmp_path):
    db = Database(tmp_path / "t.db"); await db.initialize()
    await db._conn.execute(
        "INSERT INTO venue_overrides (symbol, venue, pair, disabled, "
        "created_at, updated_at) "
        "VALUES ('WBTC','binance','WBTCUSDT',1,'2026-04-23T00Z','2026-04-23T00Z')"
    )
    await db._conn.commit()
    resolver = VenueResolver(
        binance_adapter=AsyncMock(), override_store=OverrideStore(db),
        positive_ttl=timedelta(hours=1), negative_ttl=timedelta(seconds=60),
        db=db,
    )
    r = await resolver.resolve("WBTC")
    # Disabled override → resolver returns None AND does NOT fall through to exchangeInfo
    assert r is None
    await db.close()


@freeze_time("2026-04-23 00:00:00")
async def test_negative_cache_ttl_expires_at_60s(tmp_path):
    db = Database(tmp_path / "t.db"); await db.initialize()
    adapter = AsyncMock()
    adapter.resolve_pair_for_symbol = AsyncMock(return_value=None)
    resolver = VenueResolver(
        binance_adapter=adapter, override_store=OverrideStore(db),
        positive_ttl=timedelta(hours=1), negative_ttl=timedelta(seconds=60),
        db=db,
    )
    assert await resolver.resolve("UNKNOWN") is None
    assert adapter.resolve_pair_for_symbol.call_count == 1

    from freezegun import api
    api.freeze_time("2026-04-23 00:01:01").start()  # +61s
    assert await resolver.resolve("UNKNOWN") is None
    assert adapter.resolve_pair_for_symbol.call_count == 2
    await db.close()
```

- [ ] **Step 2: Run — expect import error**

- [ ] **Step 3: Implement `OverrideStore` + `VenueResolver`**

```python
# scout/live/resolver.py
"""VenueResolver + OverrideStore (spec §7). Two classes, one file per §2.1."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import structlog

from scout.db import Database
from scout.live.adapter_base import ExchangeAdapter
from scout.live.types import ResolvedVenue

log = structlog.get_logger(__name__)


class OverrideStore:
    """Read-only view of venue_overrides. Write path is direct SQL via ops CLI."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def lookup(self, symbol: str) -> tuple[str | None, bool] | None:
        """Return (pair, disabled_bool) for symbol, or None if no row."""
        assert self._db._conn is not None
        cur = await self._db._conn.execute(
            "SELECT pair, disabled FROM venue_overrides WHERE symbol = ?",
            (symbol.upper(),),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return (row[0], bool(row[1]))


class VenueResolver:
    def __init__(
        self,
        *,
        binance_adapter: ExchangeAdapter,
        override_store: OverrideStore,
        positive_ttl: timedelta,
        negative_ttl: timedelta,
        db: Database,
    ) -> None:
        self._adapter = binance_adapter
        self._overrides = override_store
        self._positive_ttl = positive_ttl
        self._negative_ttl = negative_ttl
        self._db = db
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def resolve(self, symbol: str) -> ResolvedVenue | None:
        sym = symbol.upper()
        # 1. Cache
        cached = await self._cache_get(sym)
        if cached is not None:
            return cached  # may be None from negative cache sentinel

        # 2. Single-flight per-symbol
        async with self._locks[sym]:
            cached = await self._cache_get(sym)
            if cached is not None:
                return cached

            # 3. Override
            ov = await self._overrides.lookup(sym)
            if ov is not None:
                pair, disabled = ov
                if disabled:
                    # Spec §5 gate 4: disabled override SHORT-CIRCUITS.
                    # Do NOT fall through to exchangeInfo.
                    return None
                resolved = ResolvedVenue(
                    symbol=sym, venue="binance", pair=pair, source="override_table"
                )
                await self._cache_put_positive(sym, resolved)
                return resolved

            # 4. Binance exchangeInfo
            pair = await self._adapter.resolve_pair_for_symbol(sym)
            if pair is None:
                await self._cache_put_negative(sym)
                return None
            resolved = ResolvedVenue(
                symbol=sym, venue="binance", pair=pair,
                source="binance_exchangeinfo",
            )
            await self._cache_put_positive(sym, resolved)
            return resolved

    # --- cache helpers -----------------------------------------------------

    async def _cache_get(self, sym: str) -> ResolvedVenue | None | False:
        """Return ResolvedVenue for positive hit, None for negative hit,
        False for cache miss. (Three-valued to distinguish 'not-cached' from
        'cached-as-negative'.)"""
        assert self._db._conn is not None
        now = datetime.now(timezone.utc)
        cur = await self._db._conn.execute(
            "SELECT outcome, venue, pair, expires_at FROM resolver_cache "
            "WHERE symbol = ?",
            (sym,),
        )
        row = await cur.fetchone()
        if row is None:
            return False
        expires_at = datetime.fromisoformat(row[3].replace("Z", "+00:00"))
        if expires_at <= now:
            return False
        if row[0] == "positive":
            return ResolvedVenue(
                symbol=sym, venue=row[1], pair=row[2], source="cache",
            )
        return None  # cached-negative

    async def _cache_put_positive(self, sym: str, rv: ResolvedVenue) -> None:
        now = datetime.now(timezone.utc)
        expires_at = now + self._positive_ttl
        await self._db._conn.execute(
            "INSERT INTO resolver_cache "
            "(symbol, outcome, venue, pair, resolved_at, expires_at) "
            "VALUES (?, 'positive', ?, ?, ?, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET "
            "  outcome=excluded.outcome, venue=excluded.venue, pair=excluded.pair, "
            "  resolved_at=excluded.resolved_at, expires_at=excluded.expires_at",
            (sym, rv.venue, rv.pair, now.isoformat(), expires_at.isoformat()),
        )
        await self._db._conn.commit()

    async def _cache_put_negative(self, sym: str) -> None:
        now = datetime.now(timezone.utc)
        expires_at = now + self._negative_ttl
        await self._db._conn.execute(
            "INSERT INTO resolver_cache "
            "(symbol, outcome, venue, pair, resolved_at, expires_at) "
            "VALUES (?, 'negative', NULL, NULL, ?, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET "
            "  outcome=excluded.outcome, venue=NULL, pair=NULL, "
            "  resolved_at=excluded.resolved_at, expires_at=excluded.expires_at",
            (sym, now.isoformat(), expires_at.isoformat()),
        )
        await self._db._conn.commit()
```

Note the `_cache_get` sentinel three-valuing: `False` = miss, `None` = cached-negative, `ResolvedVenue` = cached-positive. Tests must respect that.

- [ ] **Step 4: Run tests + commit**

```bash
uv run pytest tests/live/test_venue_resolver.py -v
git add scout/live/resolver.py tests/live/test_venue_resolver.py
git commit -m "feat(bl055): VenueResolver with single-flight + TTL + override fallback"
```

---

## Group C — Gates + kill switch + metrics

### Task 10: `scout/live/kill_switch.py`

**Files:**
- Create: `scout/live/kill_switch.py`
- Create: `tests/live/test_kill_switch.py`

- [ ] **Step 1: Write tests (`compute_kill_duration` G2 — spec §6.3 — first)**

```python
# tests/live/test_kill_switch.py
from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.live.kill_switch import KillSwitch, compute_kill_duration


@pytest.mark.parametrize(
    "trigger_hour,trigger_minute,expected_hours",
    [
        (0, 15, 23.75),    # trigger right after midnight → hold until NEXT midnight
        (12, 0, 12.0),     # noon → next midnight = 12h, min is 4h, max() = 12h
        (23, 55, 4.083),   # late-night → 4h minimum wins over 5-min-to-midnight
        (20, 0, 4.0),      # 20:00 → 4h min wins over 4h-to-midnight
    ],
)
def test_compute_kill_duration_maxes_midnight_vs_4h(
    trigger_hour, trigger_minute, expected_hours
):
    trig = datetime(2026, 4, 23, trigger_hour, trigger_minute, tzinfo=timezone.utc)
    dur = compute_kill_duration(trig)
    assert abs(dur.total_seconds() / 3600 - expected_hours) < 0.01


async def test_trigger_inserts_row_and_sets_control(tmp_path):
    db = Database(tmp_path / "t.db"); await db.initialize()
    ks = KillSwitch(db)
    kid = await ks.trigger(
        triggered_by="manual",
        reason="test",
        duration=timedelta(hours=1),
    )
    cur = await db._conn.execute(
        "SELECT id, triggered_by, cleared_at FROM kill_events"
    )
    rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == kid
    assert rows[0][1] == "manual"
    assert rows[0][2] is None

    cur = await db._conn.execute(
        "SELECT active_kill_event_id FROM live_control WHERE id=1"
    )
    assert (await cur.fetchone())[0] == kid
    await db.close()


async def test_is_active_returns_none_when_cleared(tmp_path):
    db = Database(tmp_path / "t.db"); await db.initialize()
    ks = KillSwitch(db)
    assert await ks.is_active() is None
    kid = await ks.trigger(triggered_by="manual", reason="x",
                           duration=timedelta(hours=1))
    assert (await ks.is_active()).kill_event_id == kid
    await ks.clear(cleared_by="manual")
    assert await ks.is_active() is None
    await db.close()


async def test_auto_clear_if_expired_fires_when_past_killed_until(tmp_path):
    db = Database(tmp_path / "t.db"); await db.initialize()
    ks = KillSwitch(db)
    await ks.trigger(triggered_by="manual", reason="x",
                     duration=timedelta(seconds=-1))  # already expired
    did_clear = await ks.auto_clear_if_expired()
    assert did_clear is True
    assert await ks.is_active() is None
    cur = await db._conn.execute(
        "SELECT cleared_by FROM kill_events ORDER BY id DESC LIMIT 1"
    )
    assert (await cur.fetchone())[0] == "auto_expired"
    await db.close()
```

- [ ] **Step 2: Implement**

Implement `KillSwitch` class and top-level `compute_kill_duration` verbatim from spec §6.3. Class methods per §6.1 signature. Use a transaction for trigger() to ensure kill_events INSERT + live_control UPDATE are atomic.

- [ ] **Step 3: Run tests + commit**

```bash
uv run pytest tests/live/test_kill_switch.py -v
git add scout/live/kill_switch.py tests/live/test_kill_switch.py
git commit -m "feat(bl055): KillSwitch with trigger/clear/auto-expired + G2 math"
```

---

### Task 11: `scout/live/metrics.py` — UPSERT counters

**Files:**
- Create: `scout/live/metrics.py`
- Create: `tests/live/test_metrics.py`

- [ ] **Step 1: Write tests**

```python
# tests/live/test_metrics.py
from scout.db import Database
from scout.live.metrics import inc


async def test_inc_creates_row_with_value_one(tmp_path):
    db = Database(tmp_path / "t.db"); await db.initialize()
    await inc(db, "shadow_orders_opened", date_utc="2026-04-23")
    cur = await db._conn.execute(
        "SELECT value FROM live_metrics_daily "
        "WHERE date='2026-04-23' AND metric='shadow_orders_opened'"
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()


async def test_inc_increments_existing_row(tmp_path):
    db = Database(tmp_path / "t.db"); await db.initialize()
    for _ in range(5):
        await inc(db, "shadow_rejects_no_venue", date_utc="2026-04-23")
    cur = await db._conn.execute(
        "SELECT value FROM live_metrics_daily "
        "WHERE date='2026-04-23' AND metric='shadow_rejects_no_venue'"
    )
    assert (await cur.fetchone())[0] == 5
    await db.close()


async def test_inc_separates_date_buckets(tmp_path):
    db = Database(tmp_path / "t.db"); await db.initialize()
    await inc(db, "shadow_orders_opened", date_utc="2026-04-23")
    await inc(db, "shadow_orders_opened", date_utc="2026-04-24")
    cur = await db._conn.execute(
        "SELECT date, value FROM live_metrics_daily "
        "WHERE metric='shadow_orders_opened' ORDER BY date"
    )
    rows = await cur.fetchall()
    assert [(r[0], r[1]) for r in rows] == [("2026-04-23", 1), ("2026-04-24", 1)]
    await db.close()
```

- [ ] **Step 2: Implement**

```python
# scout/live/metrics.py
from __future__ import annotations

from datetime import datetime, timezone

from scout.db import Database


async def inc(
    db: Database, metric: str, *, date_utc: str | None = None, by: int = 1
) -> None:
    """UPSERT-increment a daily counter. Uses today UTC if date_utc is None."""
    assert db._conn is not None
    d = date_utc or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    await db._conn.execute(
        "INSERT INTO live_metrics_daily (date, metric, value) VALUES (?, ?, ?) "
        "ON CONFLICT(date, metric) DO UPDATE SET value = value + excluded.value",
        (d, metric, by),
    )
    await db._conn.commit()
```

- [ ] **Step 3: Run + commit**

```bash
uv run pytest tests/live/test_metrics.py -v
git add scout/live/metrics.py tests/live/test_metrics.py
git commit -m "feat(bl055): live_metrics_daily UPSERT helper"
```

---

### Task 12: `scout/live/gates.py` — eight pre-trade gates

**Files:**
- Create: `scout/live/gates.py`
- Create: `tests/live/test_pretrade_gates.py`

- [ ] **Step 1: Write gate meta-test + parametrize (spec §11.4)**

```python
# tests/live/test_pretrade_gates.py
"""Tests for pre-trade gates (spec §5, §11.4)."""

from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from scout.db import Database
from scout.live.types import Depth, DepthLevel, ResolvedVenue


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
    shadow = {r for _, r in SHADOW_REJECT_REASONS}
    live = {r for _, r in LIVE_ONLY_REJECT_REASONS}
    assert shadow | live == CHECK_CONSTRAINT_VALUES


# One happy-path test + one per reject_reason. Implementer must bind a fixture
# that constructs the gate context (db, resolver, adapter, config, kill_switch)
# and invokes `Gates(...).evaluate(...)` matching the §5 order.

async def test_gates_pass_happy_path(tmp_path):
    from scout.live.gates import Gates
    db = Database(tmp_path / "t.db"); await db.initialize()
    # Build minimal context where every gate is satisfied.
    # ... detailed fixture setup — implementer writes based on Gates signature ...
```

Implementer note: the remaining 8 parametrized tests follow the same pattern. Each test:
1. Sets up a context where one specific gate fails.
2. Calls `Gates.evaluate(...)`.
3. Asserts `result.passed is False` and `result.reject_reason == expected`.

- [ ] **Step 2: Implement `Gates` class with 8 gates (spec §5)**

Gate execution order (first failure wins):
1. `_check_kill_switch` → `kill_switch`
2. `_check_signal_allowlist` → **NOT a rejection** — returns a special `skipped_not_allowlisted` sentinel; engine treats differently (no DB row)
3. `_check_venue_resolved` → `no_venue`
4. `_check_override_enabled` → `override_disabled`
5. `_check_depth_health` → `insufficient_depth` (needs adapter.fetch_depth)
6. `_check_slippage_cap` → `slippage_exceeds_cap` (uses walk_asks + SLIPPAGE_BPS_CAP)
7. `_check_exposure_cap` → `exposure_cap` (SELECT SUM(size_usd) + COUNT check)
8. `_check_balance` → `insufficient_balance` (live mode only — raises NotImplementedError in BL-055)

Return a single `GateResult(passed, reject_reason, detail)` at the first failure.

- [ ] **Step 3: Run + commit**

```bash
uv run pytest tests/live/test_pretrade_gates.py -v
git add scout/live/gates.py tests/live/test_pretrade_gates.py
git commit -m "feat(bl055): pre-trade gates (8 gates, first-fail short-circuit)"
```

---

### Task 13: Transactional daily-cap enforcement in close path

**Files:**
- Modify: `scout/live/kill_switch.py` (add `maybe_trigger_from_daily_loss` helper)
- Create: `tests/live/test_daily_cap.py`

- [ ] **Step 1: Write failing test for concurrent-close race (spec §11.5)**

```python
# tests/live/test_daily_cap.py
"""Spec §6.2 — transactional daily loss cap + §11.5 concurrent-close race."""

import asyncio
from decimal import Decimal

from scout.config import Settings
from scout.db import Database
from scout.live.kill_switch import KillSwitch, maybe_trigger_from_daily_loss


def _s(cap_usd=50):
    return Settings(
        TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k",
        LIVE_DAILY_LOSS_CAP_USD=Decimal(cap_usd),
    )


async def _seed_closed(db: Database, pnl: float, close_date="2026-04-23"):
    await db._conn.execute(
        "INSERT INTO shadow_trades "
        "(paper_trade_id, coin_id, symbol, venue, pair, signal_type, size_usd, "
        " status, realized_pnl_usd, created_at, closed_at) "
        "VALUES (0,'c','S','binance','SUSDT','fs','100','closed_sl',?,?,?)",
        (str(pnl), f"{close_date}T00:00:00Z", f"{close_date}T00:30:00Z"),
    )
    await db._conn.commit()


async def test_single_close_under_cap_does_not_trigger(tmp_path):
    db = Database(tmp_path / "t.db"); await db.initialize()
    await _seed_closed(db, -25.0)
    ks = KillSwitch(db)
    triggered = await maybe_trigger_from_daily_loss(db, ks, _s(50))
    assert triggered is False
    assert await ks.is_active() is None
    await db.close()


async def test_single_close_over_cap_triggers(tmp_path):
    db = Database(tmp_path / "t.db"); await db.initialize()
    await _seed_closed(db, -60.0)
    ks = KillSwitch(db)
    triggered = await maybe_trigger_from_daily_loss(db, ks, _s(50))
    assert triggered is True
    assert await ks.is_active() is not None
    await db.close()


async def test_two_concurrent_closes_trigger_exactly_once(tmp_path):
    """Spec §11.5: A=-$30, B=-$25 each racing close → one kill, idempotent."""
    db = Database(tmp_path / "t.db"); await db.initialize()
    await _seed_closed(db, -30.0)
    await _seed_closed(db, -25.0)
    ks = KillSwitch(db)
    results = await asyncio.gather(
        maybe_trigger_from_daily_loss(db, ks, _s(50)),
        maybe_trigger_from_daily_loss(db, ks, _s(50)),
    )
    assert sum(results) == 1
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM kill_events WHERE cleared_at IS NULL"
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()
```

- [ ] **Step 2: Implement the helper**

```python
# scout/live/kill_switch.py — append
from decimal import Decimal
from scout.config import Settings
from scout.db import Database


async def maybe_trigger_from_daily_loss(
    db: Database, ks: KillSwitch, settings: Settings
) -> bool:
    """Compute today-UTC closed-trade SUM(realized_pnl_usd); trigger kill if
    breached and no kill currently active. Idempotent.
    Returns True if this call triggered a kill."""
    assert db._conn is not None
    cur = await db._conn.execute(
        "SELECT COALESCE(SUM(CAST(realized_pnl_usd AS REAL)), 0) "
        "FROM shadow_trades "
        "WHERE status LIKE 'closed_%' "
        "  AND date(closed_at) = date('now')"
    )
    daily_sum = (await cur.fetchone())[0]
    if daily_sum > -float(settings.LIVE_DAILY_LOSS_CAP_USD):
        return False
    if await ks.is_active() is not None:
        return False
    await ks.trigger(
        triggered_by="daily_loss_cap",
        reason=f"daily_sum={daily_sum:.2f} cap=-{settings.LIVE_DAILY_LOSS_CAP_USD}",
        duration=compute_kill_duration(datetime.now(timezone.utc)),
    )
    return True
```

- [ ] **Step 3: Run + commit**

```bash
uv run pytest tests/live/test_daily_cap.py -v
git add scout/live/kill_switch.py tests/live/test_daily_cap.py
git commit -m "feat(bl055): transactional daily-loss-cap + idempotent kill trigger"
```

---

## Group D — Engine + evaluator + reconciliation + CLI

### Task 14: `scout/live/engine.py` — `LiveEngine.on_paper_trade_opened`

**Files:**
- Create: `scout/live/engine.py`
- Create: `tests/live/test_live_engine.py`

- [ ] **Step 1: Write tests for handoff matrix**

The handoff matrix (from spec §5 + §2.2):
1. `is_eligible(signal_type)=False` → log `live_handoff_skipped`, NO DB row.
2. Kill active → log `live_handoff_skipped_killed`, NO DB row.
3. Resolver returns None → DB row with `status='rejected'`, `reject_reason='no_venue'`, metric inc.
4. Override disabled → DB row with `reject_reason='override_disabled'`.
5. Depth insufficient → `insufficient_depth`.
6. Slippage excess → `slippage_exceeds_cap`.
7. Exposure cap hit → `exposure_cap`.
8. Happy path → DB row with `status='open'` + walked_vwap + metric inc.

Write one test per case. Use AsyncMock for adapter/resolver; real DB via tmp_path.

- [ ] **Step 2: Implement `LiveEngine`**

```python
# scout/live/engine.py
"""LiveEngine — chokepoint dispatcher. One method: on_paper_trade_opened."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import structlog

from scout.db import Database
from scout.live.config import LiveConfig
from scout.live.gates import Gates
from scout.live.kill_switch import KillSwitch
from scout.live.metrics import inc
from scout.live.resolver import VenueResolver

log = structlog.get_logger(__name__)


class LiveEngine:
    def __init__(
        self, *, config: LiveConfig, resolver: VenueResolver,
        db: Database, kill_switch: KillSwitch,
    ) -> None:
        self._config = config
        self._resolver = resolver
        self._db = db
        self._ks = kill_switch

    def is_eligible(self, signal_type: str) -> bool:
        """Cheap pre-check for chokepoint (spec §2.3). No I/O."""
        return self._config.is_signal_enabled(signal_type)

    async def on_paper_trade_opened(self, paper_trade) -> None:
        """Single entry point from PaperTrader chokepoint. Fire-and-forget.
        paper_trade must have .id, .signal_type, .symbol, .coin_id."""
        trade_id = paper_trade.id
        log.info("live_handoff_started",
                 paper_trade_id=trade_id, signal_type=paper_trade.signal_type,
                 mode=self._config.mode)

        # Gate 1: allowlist (cheap repeat — defense in depth)
        if not self._config.is_signal_enabled(paper_trade.signal_type):
            log.info("live_handoff_skipped",
                     paper_trade_id=trade_id,
                     signal_type=paper_trade.signal_type,
                     reason="not_allowlisted")
            return

        # Gate 2: kill
        kill = await self._ks.is_active()
        if kill is not None:
            log.info("live_handoff_skipped_killed",
                     paper_trade_id=trade_id,
                     kill_event_id=kill.kill_event_id)
            return

        # Delegate remaining gates + walk + DB write to a private method.
        # Gates class owns the cascade; engine owns DB ledger semantics.
        # ... (see Gates.evaluate + _write_shadow_row)
```

The full method body threads: `resolver.resolve` → `Gates.evaluate` → if rejected, write `rejected` row + `inc("shadow_rejects_<reason>")`; else `walk_asks` + write `open` row + `inc("shadow_orders_opened")`.

- [ ] **Step 3: Run + commit**

```bash
uv run pytest tests/live/test_live_engine.py -v
git add scout/live/engine.py tests/live/test_live_engine.py
git commit -m "feat(bl055): LiveEngine chokepoint dispatcher"
```

---

### Task 15: `scout/live/shadow_evaluator.py`

**Files:**
- Create: `scout/live/shadow_evaluator.py`
- Create: `tests/live/test_shadow_evaluator.py`

- [ ] **Step 1: Write tests**

- TP exit: open shadow row with entry_walked_vwap=100, tp_pct=20; simulate price=120 → close with status=`closed_tp`, realized_pnl from walked exit.
- SL exit: price=75 → `closed_sl`.
- Duration exit: created_at older than MAX_DURATION_HOURS → `closed_duration`.
- Mid-life halt: adapter raises venue 5xx 3× → `status='needs_manual_review'`, `review_retries=0`, `next_review_at=now+24h`.
- Third failed retry → `rejected`-ish terminal state + alert.
- Transactional daily-cap trigger on close (covered by Task 13 already).

- [ ] **Step 2: Implement evaluator**

Single `evaluate_open_shadow_trades(db, adapter, config, ks, settings)` coroutine. Inside:
1. `SELECT * FROM shadow_trades WHERE status='open' OR (status='needs_manual_review' AND next_review_at<=now)`.
2. For each row: fetch mid via adapter; compute hypothetical pnl_pct = (mid - entry_walked_vwap) / entry_walked_vwap * 100.
3. TP crossed? SL crossed? Duration expired? → walk exit depth, compute `exit_walked_vwap`, compute `realized_pnl_usd` and `realized_pnl_pct`, UPDATE row, `maybe_trigger_from_daily_loss(…)` OUTSIDE the UPDATE transaction per §6.2.
4. Exception path → `review_retries+=1`, `next_review_at=now+24h`. After 3rd failure → terminal state + WARN.

Exported `shadow_evaluator_loop(engine, db, settings)` — infinite while-True loop with sleep `settings.TRADE_EVAL_INTERVAL_SEC`.

- [ ] **Step 3: Run + commit**

```bash
uv run pytest tests/live/test_shadow_evaluator.py -v
git add scout/live/shadow_evaluator.py tests/live/test_shadow_evaluator.py
git commit -m "feat(bl055): shadow evaluator with TP/SL/duration + review-retry"
```

---

### Task 16: `scout/live/reconciliation.py`

**Files:**
- Create: `scout/live/reconciliation.py`
- Create: `tests/live/test_reconciliation.py`

- [ ] **Step 1: Write tests**

- Zero-row boot: `live_boot_reconciliation_done` still fires (`rows_inspected=0`). Verified via structlog `capture_logs`.
- Open shadow row that crossed TP mid-restart: close as `closed_via_reconciliation` with WARN.
- Drift window log: `live_boot_reconciliation_drift_window` fires with earliest open `created_at` and restart timestamp.

- [ ] **Step 2: Implement**

```python
# scout/live/reconciliation.py
async def reconcile_open_shadow_trades(
    *, db, adapter, config, ks, settings
) -> None:
    """Boot-time recovery. ALWAYS logs live_boot_reconciliation_done."""
    assert db._conn is not None
    now = datetime.now(timezone.utc)
    cur = await db._conn.execute(
        "SELECT id, MIN(created_at) FROM shadow_trades WHERE status='open'"
    )
    # ... implementation: drift-window log, per-row TP/SL/duration check,
    # terminal log always fires.
```

- [ ] **Step 3: Run + commit**

```bash
uv run pytest tests/live/test_reconciliation.py -v
git add scout/live/reconciliation.py tests/live/test_reconciliation.py
git commit -m "feat(bl055): boot reconciliation with always-on terminal log"
```

---

### Task 17: `scout/live/cli_kill.py` — manual kill CLI

**Files:**
- Create: `scout/live/cli_kill.py`
- Create: `tests/live/test_cli_kill.py`

- [ ] **Step 1: Write tests**

```python
# tests/live/test_cli_kill.py
import sys

from scout.db import Database
from scout.live.cli_kill import main as cli_main


async def test_on_triggers_kill(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    db = Database(db_path); await db.initialize(); await db.close()
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setattr(sys, "argv", ["cli_kill", "--on", "ops test"])
    await cli_main()
    db2 = Database(db_path); await db2.initialize()
    cur = await db2._conn.execute(
        "SELECT triggered_by, reason FROM kill_events ORDER BY id DESC LIMIT 1"
    )
    row = await cur.fetchone()
    assert row[0] == "manual"
    assert row[1] == "ops test"
    await db2.close()
```

- [ ] **Step 2: Implement**

argparse with `--on REASON`, `--off`, `--status`. Loads Settings, opens Database, calls KillSwitch.trigger / clear / is_active. Entry point: `if __name__ == "__main__": asyncio.run(main())`.

- [ ] **Step 3: Run + commit**

```bash
uv run pytest tests/live/test_cli_kill.py -v
git add scout/live/cli_kill.py tests/live/test_cli_kill.py
git commit -m "feat(bl055): manual kill-switch CLI (python -m scout.live.cli_kill)"
```

---

### Task 18: `scout/live/loops.py` — async loops

**Files:**
- Create: `scout/live/loops.py`
- Create: `tests/live/test_loops.py`

Scheduled loops per spec §10:
- `shadow_evaluator_loop` — every `TRADE_EVAL_INTERVAL_SEC`; calls `evaluate_open_shadow_trades`; logs unhandled exceptions, continues.
- `override_staleness_loop` — daily at UTC 12:00; walks venue_overrides, probes Binance, batches stale WARN alert.
- `live_metrics_rollup_loop` — daily at UTC 00:30; reads today's metrics, posts INFO summary.

- [ ] **Step 1: Write tests**

One happy-path test per loop — fake clock via freezegun or inject `sleep_fn`; verify that one iteration runs the expected work and sleeps the expected interval. Test cancellation via `asyncio.CancelledError`.

- [ ] **Step 2: Implement loops matching `briefing_loop` idiom from `scout/main.py`**

Each loop accepts the components it needs (db, adapter, alerter, settings) and runs forever. Use `compute_next_run_utc(now, target_hour, target_minute)` helpers for the daily loops.

- [ ] **Step 3: Run + commit**

```bash
uv run pytest tests/live/test_loops.py -v
git add scout/live/loops.py tests/live/test_loops.py
git commit -m "feat(bl055): shadow evaluator / override staleness / metrics rollup loops"
```

---

## Group E — Main integration + paper chokepoint + check-config + CI

### Task 19: PaperTrader chokepoint — constructor injection

**Files:**
- Modify: `scout/trading/paper.py`
- Create: `tests/live/test_paper_chokepoint.py`

- [ ] **Step 1: Write failing test**

```python
# tests/live/test_paper_chokepoint.py
import asyncio
from unittest.mock import AsyncMock

import pytest

from scout.db import Database
from scout.trading.paper import PaperTrader


async def test_paper_trader_no_live_engine_unchanged(tmp_path):
    """LIVE_MODE=paper: PaperTrader.execute_buy works identically with
    live_engine=None."""
    db = Database(tmp_path / "t.db"); await db.initialize()
    pt = PaperTrader()  # existing constructor — no live_engine kwarg required
    trade_id = await pt.execute_buy(
        db=db, token_id="c", symbol="S", name="N", chain="eth",
        signal_type="first_signal", signal_data={}, current_price=1.0,
        amount_usd=100, tp_pct=40, sl_pct=20,
        signal_combo="",
        lead_time_vs_trending_min=None,
        lead_time_vs_trending_status=None,
        live_eligible_cap=20, min_quant_score=0,
    )
    assert trade_id is not None
    await db.close()


async def test_paper_trader_dispatches_to_live_engine_when_allowlisted(tmp_path):
    db = Database(tmp_path / "t.db"); await db.initialize()
    le = AsyncMock()
    le.is_eligible = lambda st: True
    pt = PaperTrader(live_engine=le)
    await pt.execute_buy(
        db=db, token_id="c", symbol="S", name="N", chain="eth",
        signal_type="first_signal", signal_data={}, current_price=1.0,
        amount_usd=100, tp_pct=40, sl_pct=20,
        signal_combo="",
        lead_time_vs_trending_min=None,
        lead_time_vs_trending_status=None,
        live_eligible_cap=20, min_quant_score=0,
    )
    # Task is scheduled but not necessarily awaited inline — wait for it.
    await asyncio.sleep(0)
    le.on_paper_trade_opened.assert_called_once()
    await db.close()


async def test_paper_trader_skips_dispatch_when_not_eligible(tmp_path):
    db = Database(tmp_path / "t.db"); await db.initialize()
    le = AsyncMock()
    le.is_eligible = lambda st: False
    pt = PaperTrader(live_engine=le)
    await pt.execute_buy(
        db=db, token_id="c", symbol="S", name="N", chain="eth",
        signal_type="volume_spike", signal_data={}, current_price=1.0,
        amount_usd=100, tp_pct=40, sl_pct=20,
        signal_combo="",
        lead_time_vs_trending_min=None,
        lead_time_vs_trending_status=None,
        live_eligible_cap=20, min_quant_score=0,
    )
    le.on_paper_trade_opened.assert_not_called()
    await db.close()
```

- [ ] **Step 2: Modify `PaperTrader`**

Key changes:
1. Add `__init__(self, *, live_engine=None)` — accept optional live_engine. Store `self._live_engine`, `self._pending_live_tasks=set()`.
2. At the TAIL of `execute_buy`, AFTER the INSERT commits and AFTER the `would_be_live` log, add:

```python
if self._live_engine is not None and self._live_engine.is_eligible(signal_type):
    if len(self._pending_live_tasks) > 50:
        log.warning(
            "live_handoff_backpressure",
            pending=len(self._pending_live_tasks),
            trade_id=trade_id,
        )
    task = asyncio.create_task(
        self._live_engine.on_paper_trade_opened(
            _PaperTradeHandoff(
                id=trade_id, signal_type=signal_type,
                symbol=symbol, coin_id=token_id,
            )
        )
    )
    self._pending_live_tasks.add(task)
    task.add_done_callback(self._pending_live_tasks.discard)
```

3. Define a tiny `_PaperTradeHandoff` dataclass locally (id, signal_type, symbol, coin_id) — keeps LiveEngine's contract typed without importing PaperTrade internals.

- [ ] **Step 3: Run all paper tests AND new chokepoint tests**

```bash
uv run pytest tests/test_paper_trader.py tests/live/test_paper_chokepoint.py -v
```

Existing `tests/test_paper_trader.py` must still pass — chokepoint is opt-in via constructor.

- [ ] **Step 4: Commit**

```bash
git add scout/trading/paper.py tests/live/test_paper_chokepoint.py
git commit -m "feat(bl055): PaperTrader chokepoint — optional LiveEngine injection"
```

---

### Task 20: Wire `LiveEngine` into `scout/main.py` + startup guardrails

**Files:**
- Modify: `scout/main.py`

- [ ] **Step 1: Read scout/main.py to find the startup block**

```bash
grep -n "async def main\|await db.initialize\|asyncio.gather\|briefing_loop" scout/main.py | head -20
```

- [ ] **Step 2: Add live-mode wiring from spec §4.3**

Block placement: AFTER `await db.initialize()` and BEFORE `PaperTrader()` instantiation. Guard with `if live_config.mode in ("shadow", "live"):`. Live-mode sub-branch raises `NotImplementedError("balance gate not wired")` immediately after checking BINANCE_API_KEY/SECRET exist.

```python
from scout.live.config import LiveConfig
from scout.live.binance_adapter import BinanceSpotAdapter
from scout.live.engine import LiveEngine
from scout.live.kill_switch import KillSwitch
from scout.live.resolver import VenueResolver, OverrideStore
from scout.live.loops import (
    shadow_evaluator_loop, override_staleness_loop, live_metrics_rollup_loop,
)

live_config = LiveConfig(settings)
live_engine: LiveEngine | None = None
_live_owned = []  # adapters to close on shutdown

if live_config.mode in ("shadow", "live"):
    if live_config.mode == "live":
        if not settings.BINANCE_API_KEY or not settings.BINANCE_API_SECRET:
            raise RuntimeError(
                "LIVE_MODE=live requires BINANCE_API_KEY/SECRET"
            )
        raise NotImplementedError(
            "balance gate not wired for live mode — cannot start live trading "
            "until scout/live/balance_gate.py is implemented"
        )
    adapter = BinanceSpotAdapter(settings)
    _live_owned.append(adapter)
    resolver = VenueResolver(
        binance_adapter=adapter,
        override_store=OverrideStore(db),
        positive_ttl=timedelta(hours=1),
        negative_ttl=timedelta(seconds=60),
        db=db,
    )
    ks = KillSwitch(db)
    live_engine = LiveEngine(
        config=live_config, resolver=resolver, db=db, kill_switch=ks,
    )

paper_trader = PaperTrader(live_engine=live_engine)  # opt-in injection

if live_config.mode in ("shadow", "live"):
    tasks.append(asyncio.create_task(
        shadow_evaluator_loop(live_engine, adapter, db, ks, settings)))
    tasks.append(asyncio.create_task(
        override_staleness_loop(adapter, db, settings)))
    tasks.append(asyncio.create_task(
        live_metrics_rollup_loop(db, alerter, settings)))
```

- [ ] **Step 3: Ensure shutdown closes `_live_owned`**

On graceful shutdown (ctrl-C / SIGTERM), iterate `_live_owned` and `await adapter.close()`.

- [ ] **Step 4: Smoke-run**

```bash
uv run python -m scout.main --dry-run --cycles 1
```

Expected: process starts and exits cleanly with `LIVE_MODE=paper` (default). No Binance traffic.

- [ ] **Step 5: Commit**

```bash
git add scout/main.py
git commit -m "feat(bl055): wire LiveEngine + loops into main (paper default unchanged)"
```

---

### Task 21: `--check-config` CLI flag + boot reconciliation call

**Files:**
- Modify: `scout/main.py`

- [ ] **Step 1: Write a smoke test**

```python
# tests/live/test_check_config.py
import subprocess
import sys


def test_check_config_prints_resolved_values():
    result = subprocess.run(
        [sys.executable, "-m", "scout.main", "--check-config"],
        capture_output=True, text=True, timeout=30,
        env={"PATH": __import__("os").environ["PATH"],
             "TELEGRAM_BOT_TOKEN": "t",
             "TELEGRAM_CHAT_ID": "c",
             "ANTHROPIC_API_KEY": "k"},
    )
    assert result.returncode == 0
    assert "LIVE_MODE" in result.stdout
    assert "paper" in result.stdout
```

- [ ] **Step 2: Add argparse flag + handler**

```python
# scout/main.py — top of main()
parser = argparse.ArgumentParser()
parser.add_argument("--check-config", action="store_true")
parser.add_argument("--dry-run", action="store_true")
parser.add_argument("--cycles", type=int, default=None)
args = parser.parse_args()

if args.check_config:
    s = Settings()
    lc = LiveConfig(s)
    print(f"LIVE_MODE={lc.mode}")
    print(f"live_signal_allowlist_set={sorted(s.live_signal_allowlist_set)}")
    print(f"live_signal_sizes_map={dict(s.live_signal_sizes_map)}")
    print(f"resolve_tp_pct={lc.resolve_tp_pct()}")
    print(f"resolve_sl_pct={lc.resolve_sl_pct()}")
    print(f"resolve_max_duration_hours={lc.resolve_max_duration_hours()}")
    print(f"LIVE_DAILY_LOSS_CAP_USD={s.LIVE_DAILY_LOSS_CAP_USD}")
    print(f"LIVE_MAX_EXPOSURE_USD={s.LIVE_MAX_EXPOSURE_USD}")
    print(f"LIVE_MAX_OPEN_POSITIONS={s.LIVE_MAX_OPEN_POSITIONS}")
    return 0
```

- [ ] **Step 3: Call boot reconciliation when mode in (shadow, live)**

After LiveEngine is constructed but before loops start:

```python
from scout.live.reconciliation import reconcile_open_shadow_trades
await reconcile_open_shadow_trades(
    db=db, adapter=adapter, config=live_config, ks=ks, settings=settings,
)
```

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/live/test_check_config.py -v
git add scout/main.py tests/live/test_check_config.py
git commit -m "feat(bl055): --check-config flag + boot reconciliation call"
```

---

### Task 22: Integration tests — six canonical flows (spec §11.6)

**Files:**
- Create: `tests/integration/__init__.py` (if missing)
- Create: `tests/integration/test_live_shadow_loop.py`

- [ ] **Step 1: Write six flow tests**

Each test: start with a fully-wired LiveEngine (real DB + aioresponses-mocked Binance + real resolver + real gates), trigger a paper-trade handoff, assert the resulting `shadow_trades` row.

Flows per §11.6:
1. Happy path (TP hit).
2. Not listed → `rejected` `no_venue`.
3. Depth starved → `rejected` `insufficient_depth`.
4. Venue transient 3× → `rejected` `venue_unavailable` + WARN.
5. Restart mid-shadow → `live_boot_reconciliation_done` fires; T3 zero-row variant also fires.
6. Mid-life halt → `needs_manual_review` → +24h retry → back to open → 3rd fail terminal + alert.

- [ ] **Step 2: Run + commit**

```bash
uv run pytest tests/integration/test_live_shadow_loop.py -v
git add tests/integration/test_live_shadow_loop.py tests/integration/__init__.py
git commit -m "test(bl055): six canonical shadow-loop integration flows"
```

---

### Task 23: CI test-count baseline gate (spec §11.9)

**Files:**
- Modify: (CI workflow file — probably `.github/workflows/tests.yml` or similar; if none exists, create)

- [ ] **Step 1: Check current baseline**

```bash
uv run pytest --collect-only -q | tail -1
```

Current master baseline (post BL-060 + hotfix): run locally, record the integer `N`.

- [ ] **Step 2: Add a baseline gate to CI**

If `.github/workflows/tests.yml` exists, append a step:

```yaml
- name: Regression protection — test count baseline
  run: |
    BASELINE_COUNT=N    # set to current master count
    ACTUAL_COUNT=$(uv run pytest --collect-only -q | tail -1 | awk '{print $1}')
    test "$ACTUAL_COUNT" -ge "$BASELINE_COUNT" || {
      echo "ERROR: test count dropped from $BASELINE_COUNT to $ACTUAL_COUNT"
      exit 1
    }
```

If there is no CI workflow yet, create `.github/workflows/tests.yml` with the basic pytest step plus the baseline gate.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/tests.yml
git commit -m "ci(bl055): test-count baseline gate per spec §11.9"
```

---

## Post-implementation tasks

### Task 24: Full suite + coverage check

- [ ] **Step 1: Run the entire suite**

```bash
uv run pytest tests/ --tb=short -q
```

Expected: all tests pass. Count ≥ current baseline + ~150.

- [ ] **Step 2: Collect coverage summary**

```bash
uv run pytest tests/live/ tests/integration/test_live_shadow_loop.py \
  --cov=scout/live --cov-report=term-missing
```

Target: ≥ 90% coverage on `scout/live/*`. Miss rationale noted in PR description for any file below that.

### Task 25: Pre-merge F1 pre-flight (spec §4.5)

- [ ] **Step 1: Run on VPS BEFORE merging**

```bash
ssh srilu-vps 'cd /root/gecko-alpha && git fetch origin && git checkout feat/bl055-live-trading-core && uv run python -c "from scout.config import Settings; Settings(); print(\"ok\")"' > .ssh_f1.txt 2>&1
```

Read `.ssh_f1.txt`. Must print `ok`. Any ValidationError → fix `.env` typo on VPS first. Record result in PR.

### Task 26: Create PR

```bash
gh pr create --title "feat(bl055): live-trading execution core — shadow mode" \
    --body "$(cat <<'EOF'
## Summary

Execution-core plumbing for live trading on Binance spot. Default unchanged
(`LIVE_MODE=paper`); shadow-mode exercises the full pipeline without real
orders; live-mode blocked by `NotImplementedError` at startup (balance gate
pending in BL-058).

## Test plan

- [x] tests/live/* green locally
- [x] tests/integration/test_live_shadow_loop.py green locally
- [x] Full suite count ≥ baseline
- [x] F1 pre-flight (`extra="forbid"`) on VPS — see `.ssh_f1.txt`
- [ ] 7-day shadow soak on VPS (spec §11.7) — starts post-merge

## Rollout

1. Merge with `LIVE_MODE=paper` (default). Zero behavior change.
2. On VPS, set `LIVE_MODE=shadow` + `LIVE_SIGNAL_ALLOWLIST=first_signal` +
   credentials NOT set (shadow doesn't need them).
3. `systemctl restart gecko-pipeline`.
4. Monitor for 7 calendar days per soak criteria.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

### Task 27: Five parallel reviewers on PR diff

Per the user's directive. Reviewers focus on:
- Schema correctness + FK RESTRICT + CHECK constraints
- Concurrency correctness (single-flight, transactional daily cap)
- Error taxonomy (does every gate map to the right reject_reason?)
- Observability (are the §10.2 log events present with stable names?)
- Shadow-to-live leakage (could anything reach a real order in BL-055?)

### Task 28: Address findings + merge + deploy + start soak

- [ ] `gh pr merge --squash --delete-branch`
- [ ] SSH `git fetch && git reset --hard origin/master`
- [ ] Edit VPS `.env` — append `LIVE_MODE=shadow` and `LIVE_SIGNAL_ALLOWLIST=first_signal`
- [ ] `systemctl restart gecko-pipeline gecko-dashboard`
- [ ] Verify via `journalctl -u gecko-pipeline -n 50`: `live_boot_reconciliation_done` fires with `rows_inspected=0`.
- [ ] Record soak start time; schedule 7-day checkpoint.

---

## Self-review notes

**Spec coverage map:**

- §1 (goals, non-goals, three modes): Task 20 (startup guardrails), Task 3 (LIVE_MODE enum).
- §2 (architecture, chokepoint): Task 19 (chokepoint), Task 14 (engine).
- §3 (data model): Task 1 (migration) + Task 2 (pragmas).
- §4 (config surface, LiveConfig, .env.example, F1 pre-flight): Tasks 3, 4, 5, 21, 25.
- §5 (pre-trade gates): Task 12.
- §6 (kill switch + daily loss cap): Tasks 10, 13.
- §7 (venue resolver): Task 9.
- §8 (orderbook walker): Task 8.
- §9 (rate limiting): Task 7.
- §10 (error handling + observability): spread across Tasks 14, 15, 16 (logging events + metrics).
- §11 (testing strategy): Tasks 22 (integration), 23 (CI baseline), 24 (coverage).
- §12 (rollout): Task 28.
- §13 I1-I5 tickets: I1 Task 2, I2 Task 25, I3 Task 5, I4 (manual systemd audit — included in soak checklist), I5 (post-soak memory entry — out of scope for this plan).

**Known gaps relative to spec:**

- Spec §11.7 mentions `scripts/soak_report.sh` — deferred. Plan includes the query logic as a tracked item but actual script writing happens post-merge alongside soak.
- Spec §11.8 flip-to-live checklist — this plan implements **shadow mode only**. Flipping to live is explicitly out of BL-055 v1 (per §1.3 and §12.3).

**Deliberate simplifications vs spec:**

- The spec allows "two classes, one file" for resolver.py. Plan respects that.
- Loops use the existing `briefing_loop` pattern from `scout/main.py` rather than introducing APScheduler (noted in spec §4.3 "Scheduler idiom follows briefing_loop pattern").
- Alerter wiring for WARN/CRITICAL from spec §10.4 is threaded through existing `scout/alerter.py` patterns; new alerters are not introduced.
