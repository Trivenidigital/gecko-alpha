**New primitives introduced:** New module `scout/live/correction_counter.py` exposing `increment_consecutive(db, signal_type, venue)` (writes/upserts `signal_venue_correction_count` row, resets `last_corrected_at` on each successful fill) and `reset_on_correction(db, signal_type, venue, correction_at)` (zeroes `consecutive_no_correction` when operator unwinds within 24h). New `LiveEngine._dispatch_live(paper_trade, venue, size_usd)` private method — runs only when `LIVE_MODE='live'` AND `LIVE_TRADING_ENABLED=True` AND `LIVE_USE_REAL_SIGNED_REQUESTS=True`; calls `RoutingLayer.get_candidates` → picks top → adapter.place_order_request → adapter.await_fill_confirmation → on terminal=filled/partial calls `correction_counter.increment_consecutive`. Existing shadow-mode flow (write `shadow_trades` row) preserved verbatim under `LIVE_MODE='shadow'`. New optional dependency on `LiveEngine.__init__`: `routing: RoutingLayer | None = None` (when None, live-mode flow falls back to BL-055 resolver path — operator gradient: M1.5b ships routing-layer enabled but flag-gated). New Settings field `LIVE_USE_ROUTING_LAYER: bool = False` — default False = M1.5a behavior preserved (routing scaffold present but not wired); operator opts in to M1.5b's multi-venue dispatch by flipping this. M1.5b does NOT call `should_require_approval` (Telegram gateway runtime integration deferred to M1.5c). M1.5b does NOT add recurring health probe, reconciler, or V2 minor cleanups (deferred to M1.5c).

# Live Trading Milestone 1.5b Implementation Plan — Engine Routing Dispatch + Correction Counter Writer

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** Close V1 review's CRITICAL findings C1 (engine doesn't call routing layer) + C2 (signal_venue_correction_count has zero writers). Wire `RoutingLayer.get_candidates` into engine dispatch under `LIVE_MODE='live'`. Wire correction counter increment-on-fill helper. Both wirings flag-gated behind `LIVE_USE_ROUTING_LAYER` (default False) to preserve M1.5a behavior under default config.

**Architecture:** `LiveEngine.on_paper_trade_opened` keeps existing shadow-mode flow intact. Adds a parallel `_dispatch_live()` private method that fires under `LIVE_MODE='live'` AND `LIVE_USE_ROUTING_LAYER=True`. The live path:
1. Calls `RoutingLayer.get_candidates(canonical, chain_hint, signal_type, size_usd)` — returns ranked list
2. If empty → INSERT live_trades row with `status='rejected', reject_reason='no_venue'`; return
3. Picks top candidate
4. Calls `adapter.place_order_request(OrderRequest)` — idempotency-aware (M1.5a)
5. Calls `adapter.await_fill_confirmation(...)` — polls until terminal (M1.5a)
6. On `status='filled'` / `'partial'` → calls `correction_counter.increment_consecutive(db, signal_type, venue)`
7. On `status='rejected'` / `'timeout'` → no counter increment; UPDATE live_trades.status accordingly

Telegram approval gateway runtime hook (V1-C1 partial) deferred to M1.5c — ALL approval gates are decorative in M1.5b. Acceptable because dormant state remains fail-closed: `LIVE_USE_ROUTING_LAYER=False` default → routing not called; `LIVE_USE_REAL_SIGNED_REQUESTS=False` default → place_order_request raises NotImplementedError; `LIVE_TRADING_ENABLED=False` default → main.py refuses live boot.

**Tech Stack:** Python 3.12, aiosqlite, pydantic v2 BaseSettings, pytest-asyncio (auto mode), aioresponses (HTTP mock), structlog (PrintLoggerFactory — tests use `structlog.testing.capture_logs`).

**Test reference snippets omit `_REQUIRED` for brevity** — same convention as M1.5a plan.

**Total scope:** ~30-40 steps across 6 tasks. Smaller than M1.5a (~70 steps) because no new schema migrations, no new exception classes, no signing-primitive surface.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `scout/live/correction_counter.py` | **Create** | `increment_consecutive(db, signal_type, venue)` + `reset_on_correction(db, signal_type, venue, correction_at)`. Pure DB wrapper; no I/O beyond aiosqlite. |
| `scout/live/engine.py` | Modify | Add `_dispatch_live()` method; modify `on_paper_trade_opened` to branch on `mode == 'live' and LIVE_USE_ROUTING_LAYER` |
| `scout/config.py` | Modify | Add `LIVE_USE_ROUTING_LAYER: bool = False` Settings field |
| `scout/main.py` | Modify | Construct `RoutingLayer` instance + pass to `LiveEngine.__init__` when `LIVE_USE_ROUTING_LAYER=True` |
| `tests/test_live_correction_counter.py` | **Create** | increment behavior + reset behavior + idempotency on first-row creation |
| `tests/test_live_engine_dispatch.py` | **Create** | live-mode dispatch flow tests (mocked routing + adapter); shadow-mode unchanged regression |
| `tests/test_live_master_kill.py` | Modify | Add `LIVE_USE_ROUTING_LAYER` default test |

**Schema versions reserved:** none. M1.5b is migration-free.

---

## Task 0: Setup — branch + Settings field

- [ ] **Step 1: Verify branch**

```bash
git branch --show-current
# Expected: feat/live-trading-m1-5b
```

- [ ] **Step 2: Verify M1.5a prereqs**

```bash
ls scout/live/correction_counter.py 2>&1  # No such file (created Task 1)
grep -c "_dispatch_live\|LIVE_USE_ROUTING_LAYER" scout/live/engine.py
# Expected: 0 (not yet wired)
grep -c "from scout.live.routing" scout/main.py
# Expected: 0 (RoutingLayer not yet constructed)
```

- [ ] **Step 3: Add `LIVE_USE_ROUTING_LAYER` Settings field**

In `scout/config.py` near the M1.5a `LIVE_USE_REAL_SIGNED_REQUESTS` block:

```python
    # M1.5b — gates the multi-venue routing layer dispatch in
    # LiveEngine. When False (default), engine falls back to M1.5a's
    # BL-055 single-venue resolver path. Operator opts in by flipping
    # True after observing first 1-3 successful place_order_request +
    # await_fill_confirmation cycles in live mode.
    LIVE_USE_ROUTING_LAYER: bool = False
```

- [ ] **Step 4: Failing test**

In `tests/test_live_master_kill.py`:

```python
def test_live_use_routing_layer_defaults_off(self):
    """M1.5b — default False preserves M1.5a single-venue behavior."""
    assert (
        Settings(_env_file=None, **_REQUIRED).LIVE_USE_ROUTING_LAYER is False
    )
```

- [ ] **Step 5: Run + commit**

```bash
uv run --native-tls pytest tests/test_live_master_kill.py -v
git add scout/config.py tests/test_live_master_kill.py
git commit -m "feat(live-m1.5b): LIVE_USE_ROUTING_LAYER Settings field — Task 0"
```

---

## Task 1: `correction_counter.py` — increment/reset helpers

**Files:**
- Create: `scout/live/correction_counter.py`
- Test: `tests/test_live_correction_counter.py` (NEW)

- [ ] **Step 1: Failing tests**

```python
"""BL-NEW-LIVE-HYBRID M1.5b: signal_venue_correction_count writer tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scout.db import Database
from scout.live.correction_counter import (
    increment_consecutive,
    reset_on_correction,
)


@pytest.mark.asyncio
async def test_increment_creates_row_on_first_call(tmp_path):
    """First call for (signal_type, venue) creates a row with
    consecutive_no_correction=1."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await increment_consecutive(db, "first_signal", "binance")
    cur = await db._conn.execute(
        "SELECT consecutive_no_correction FROM signal_venue_correction_count "
        "WHERE signal_type = ? AND venue = ?",
        ("first_signal", "binance"),
    )
    row = await cur.fetchone()
    assert row[0] == 1
    await db.close()


@pytest.mark.asyncio
async def test_increment_bumps_counter_on_subsequent_calls(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    for _ in range(5):
        await increment_consecutive(db, "first_signal", "binance")
    cur = await db._conn.execute(
        "SELECT consecutive_no_correction FROM signal_venue_correction_count "
        "WHERE signal_type = ? AND venue = ?",
        ("first_signal", "binance"),
    )
    row = await cur.fetchone()
    assert row[0] == 5
    await db.close()


@pytest.mark.asyncio
async def test_reset_zeros_counter_and_records_correction_at(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    for _ in range(10):
        await increment_consecutive(db, "first_signal", "binance")
    correction_at = datetime.now(timezone.utc).isoformat()
    await reset_on_correction(db, "first_signal", "binance", correction_at)
    cur = await db._conn.execute(
        "SELECT consecutive_no_correction, last_corrected_at "
        "FROM signal_venue_correction_count "
        "WHERE signal_type = ? AND venue = ?",
        ("first_signal", "binance"),
    )
    row = await cur.fetchone()
    assert row[0] == 0
    assert row[1] == correction_at
    await db.close()


@pytest.mark.asyncio
async def test_increment_independent_per_signal_venue_pair(tmp_path):
    """(first_signal × binance) and (first_signal × kraken) have
    independent counters."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await increment_consecutive(db, "first_signal", "binance")
    await increment_consecutive(db, "first_signal", "kraken")
    await increment_consecutive(db, "first_signal", "binance")
    cur = await db._conn.execute(
        "SELECT venue, consecutive_no_correction "
        "FROM signal_venue_correction_count "
        "WHERE signal_type = ? ORDER BY venue",
        ("first_signal",),
    )
    rows = await cur.fetchall()
    by_venue = {r[0]: r[1] for r in rows}
    assert by_venue == {"binance": 2, "kraken": 1}
    await db.close()
```

- [ ] **Step 2: Run — expect 4 FAILs**

- [ ] **Step 3: Implement `scout/live/correction_counter.py`**

```python
"""BL-NEW-LIVE-HYBRID M1.5b: signal_venue_correction_count writer.

Closes V1 review's C2 finding (counter stuck at 0 forever — no writers).
The counter is read by `approval_thresholds.should_require_approval`
Gate 1 (new-venue gate: < 30 consecutive_no_correction → require approval).
M1.5b's increment-on-fill semantic is a SIMPLIFICATION of design intent
(design says increment after 24h with no operator unwind); M1.5c's
reconciler can refine to true 24h-window logic later.
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

from scout.db import Database

log = structlog.get_logger(__name__)


async def increment_consecutive(
    db: Database, signal_type: str, venue: str
) -> None:
    """Increment consecutive_no_correction by 1 for (signal_type, venue).

    Creates the row on first call (ON CONFLICT...DO UPDATE).
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    now_iso = datetime.now(timezone.utc).isoformat()
    async with db._txn_lock:
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
    log.info(
        "correction_counter_incremented",
        signal_type=signal_type,
        venue=venue,
    )


async def reset_on_correction(
    db: Database, signal_type: str, venue: str, correction_at: str
) -> None:
    """Reset consecutive_no_correction to 0 + record last_corrected_at.

    Called from the operator-correction path (M1.5c when reconciler
    detects a 24h-window unwind; M1.5b operator can call directly via
    SQL for manual corrections).
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    now_iso = datetime.now(timezone.utc).isoformat()
    async with db._txn_lock:
        await db._conn.execute(
            """INSERT INTO signal_venue_correction_count
               (signal_type, venue, consecutive_no_correction,
                last_corrected_at, last_updated_at)
               VALUES (?, ?, 0, ?, ?)
               ON CONFLICT (signal_type, venue) DO UPDATE SET
                  consecutive_no_correction = 0,
                  last_corrected_at = excluded.last_corrected_at,
                  last_updated_at = excluded.last_updated_at""",
            (signal_type, venue, correction_at, now_iso),
        )
        await db._conn.commit()
    log.info(
        "correction_counter_reset",
        signal_type=signal_type,
        venue=venue,
        correction_at=correction_at,
    )
```

- [ ] **Step 4: Run — expect 4 PASS**

- [ ] **Step 5: Commit**

```bash
git add scout/live/correction_counter.py tests/test_live_correction_counter.py
git commit -m "feat(live-m1.5b): correction_counter increment/reset helpers (Task 1, V1-C2 closure)"
```

---

## Task 2: `LiveEngine._dispatch_live` — routing dispatch + place_order + await_fill + counter increment

**Files:**
- Modify: `scout/live/engine.py`
- Test: `tests/test_live_engine_dispatch.py` (NEW)

- [ ] **Step 1: Refactor `on_paper_trade_opened`**

Add `LIVE_USE_ROUTING_LAYER` branch. Existing shadow flow preserved verbatim under `mode='shadow'` OR `mode='live' and not flag_set`.

```python
    async def on_paper_trade_opened(self, paper_trade: _PaperTradeLike) -> None:
        # ... existing master-kill check, gates, allowlist, etc unchanged ...

        # After gates pass + entry_vwap computed + shadow_trades row written:
        # ... existing happy-path code unchanged for shadow mode ...

        # M1.5b live-mode dispatch (V1-C1 closure)
        if (
            self._config.mode == "live"
            and getattr(self._config._s, "LIVE_USE_ROUTING_LAYER", False)
            and self._routing is not None
        ):
            await self._dispatch_live(
                paper_trade=paper_trade,
                size_usd=size_usd,
            )

    async def _dispatch_live(
        self, *, paper_trade: _PaperTradeLike, size_usd: Decimal
    ) -> None:
        """M1.5b live-mode dispatch (V1-C1 + V1-C2 closure).

        - Routes via RoutingLayer
        - Calls adapter.place_order_request (M1.5a idempotency-aware)
        - Calls adapter.await_fill_confirmation (M1.5a polling)
        - On terminal=filled/partial → increment correction counter
        """
        from uuid import uuid4
        from scout.live.adapter_base import OrderRequest
        from scout.live.correction_counter import increment_consecutive

        canonical = paper_trade.symbol
        chain_hint = getattr(paper_trade, "chain", None)

        candidates = await self._routing.get_candidates(
            canonical=canonical,
            chain_hint=chain_hint,
            signal_type=paper_trade.signal_type,
            size_usd=float(size_usd),
        )
        if not candidates:
            log.info(
                "live_dispatch_no_venue",
                paper_trade_id=paper_trade.id,
                canonical=canonical,
            )
            return  # routing layer logs reason; engine doesn't double-write

        top = candidates[0]
        intent_uuid = str(uuid4())
        request = OrderRequest(
            paper_trade_id=paper_trade.id,
            canonical=canonical,
            venue_pair=top.venue_pair,
            side="buy",
            size_usd=float(size_usd),
            intent_uuid=intent_uuid,
        )

        try:
            venue_order_id = await self._adapter.place_order_request(request)
        except NotImplementedError as exc:
            log.info(
                "live_dispatch_signed_disabled",
                paper_trade_id=paper_trade.id,
                err=str(exc),
            )
            return
        except Exception:
            log.exception(
                "live_dispatch_place_order_failed",
                paper_trade_id=paper_trade.id,
            )
            return

        try:
            confirmation = await self._adapter.await_fill_confirmation(
                venue_order_id=venue_order_id,
                client_order_id=request.intent_uuid,  # cid format covered by idempotency.py
                timeout_sec=30.0,
            )
        except Exception:
            log.exception(
                "live_dispatch_await_fill_failed",
                paper_trade_id=paper_trade.id,
                venue_order_id=venue_order_id,
            )
            return

        log.info(
            "live_dispatch_terminal",
            paper_trade_id=paper_trade.id,
            venue_order_id=venue_order_id,
            status=confirmation.status,
            fill_price=confirmation.fill_price,
        )

        # V1-C2 closure: increment consecutive_no_correction counter on
        # successful fill. Resets fire from operator-correction path
        # (manual SQL or M1.5c reconciler).
        if confirmation.status in ("filled", "partial"):
            await increment_consecutive(
                self._db, paper_trade.signal_type, top.venue
            )
```

- [ ] **Step 2: Update `LiveEngine.__init__` to accept routing param**

```python
    def __init__(
        self,
        *,
        config: LiveConfig,
        resolver: VenueResolver,
        adapter: ExchangeAdapter,
        db: Database,
        kill_switch: KillSwitch,
        routing: "RoutingLayer | None" = None,
    ) -> None:
        # ... existing ...
        self._routing = routing
```

- [ ] **Step 3: Tests in `tests/test_live_engine_dispatch.py`**

Stubbed RoutingLayer + stubbed adapter to test dispatch flow without real Binance:

```python
@pytest.mark.asyncio
async def test_dispatch_live_skips_when_no_candidates(tmp_path):
    """Empty routing result → log + return (no DB write here; routing
    layer wrote 'no_venue' detail)."""
    # ... stub routing.get_candidates returns [] ...
    # ... call _dispatch_live ...
    # ... assert correction_counter NOT incremented ...


@pytest.mark.asyncio
async def test_dispatch_live_increments_counter_on_filled(tmp_path):
    """Top candidate + adapter returns FILLED → counter incremented."""
    # ... stub routing returns [RouteCandidate(binance, BTCUSDT, ...)] ...
    # ... stub adapter.place_order_request returns 'BNX-12345' ...
    # ... stub adapter.await_fill_confirmation returns confirmation status='filled' ...
    # ... call _dispatch_live ...
    # ... assert signal_venue_correction_count[(first_signal, binance)].consecutive_no_correction == 1


@pytest.mark.asyncio
async def test_dispatch_live_no_counter_on_timeout(tmp_path):
    """Adapter returns status='timeout' → counter NOT incremented."""
    # ... assert correction_counter.consecutive_no_correction == 0


@pytest.mark.asyncio
async def test_dispatch_live_no_op_when_signed_disabled(tmp_path):
    """LIVE_USE_REAL_SIGNED_REQUESTS=False → place_order_request raises
    NotImplementedError → engine logs + returns silently."""


@pytest.mark.asyncio
async def test_shadow_mode_unchanged_when_routing_layer_off(tmp_path):
    """LIVE_USE_ROUTING_LAYER=False (default) + mode='live' → existing
    M1.5a flow runs (no _dispatch_live call). M1.5a tests stay green."""
```

- [ ] **Step 4: Commit**

```bash
git add scout/live/engine.py tests/test_live_engine_dispatch.py
git commit -m "feat(live-m1.5b): _dispatch_live engine wiring (Task 2, V1-C1+C2 closures)"
```

---

## Task 3: `scout/main.py` — construct RoutingLayer + inject into LiveEngine

**Files:**
- Modify: `scout/main.py`

- [ ] **Step 1: Construct RoutingLayer when LIVE_USE_ROUTING_LAYER=True**

In the `if live_config.mode in ("shadow", "live"):` block, after `live_adapter` construction:

```python
        live_routing: "RoutingLayer | None" = None
        if getattr(settings, "LIVE_USE_ROUTING_LAYER", False):
            from scout.live.routing import RoutingLayer

            live_routing = RoutingLayer(
                db=db,
                settings=settings,
                adapters={"binance": live_adapter},
            )
            logger.info("routing_layer_constructed", venues=["binance"])

        live_engine = LiveEngine(
            config=live_config,
            resolver=resolver,
            adapter=live_adapter,
            db=db,
            kill_switch=live_kill_switch,
            routing=live_routing,
        )
```

- [ ] **Step 2: Commit**

```bash
git commit -am "feat(live-m1.5b): construct RoutingLayer + inject into LiveEngine (Task 3)"
```

---

## Task 4: Full regression + black

```bash
uv run --native-tls pytest tests/test_live_*.py tests/live/ -q
uv run --native-tls black scout/ tests/
git commit -am "chore(live-m1.5b): black reformat"
```

---

## Task 5: PR + 3-vector reviewers + merge + deploy

Per CLAUDE.md §8 (money flows axis):
- V1 — statistical/policy: counter semantic correctness; correction-counter use case alignment with V1's approval-removal gate.
- V2 — structural/code: engine refactor preserves shadow-mode contract; routing dispatch composition; transaction boundaries.
- V3 — strategy/blast-radius: dormant-state safety (default flags off); operator activation sequencing; reversibility.

---

## Done criteria

- All new tests pass; full regression clean
- 0 schema migrations introduced
- V1-C1 closure: routing layer is called from engine under flag-gated live mode
- V1-C2 closure: signal_venue_correction_count has writers
- LIVE_USE_ROUTING_LAYER defaults False — M1.5a behavior preserved unless operator opts in
- M1.5c plan can be drafted (recurring health probe + reconciler + Telegram approval gateway runtime + minor cleanups)

## What this milestone does NOT do (M1.5c scope)

- Does NOT call `should_require_approval` from engine (Telegram gateway runtime hook)
- Does NOT add recurring health probe (boot-time smoke is point-in-time)
- Does NOT add reconciliation worker for orphaned live_trades rows
- Does NOT bundle V2 deferred minors (ServiceRunner cancel-log, view CAST symmetry, override-NULL filter, venue_health staleness gate)

## Reversibility

Fast revert: `LIVE_USE_ROUTING_LAYER=False` in `.env` → restart. Engine falls back to M1.5a's BL-055 single-venue resolver path; no live trades are dispatched via routing.
