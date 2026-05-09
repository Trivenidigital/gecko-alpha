**New primitives introduced:** New module `scout/live/correction_counter.py` exposing `increment_consecutive(db, signal_type, venue)` (writes/upserts `signal_venue_correction_count` row on terminal `status='filled'` ONLY — partial fills excluded per plan-stage R1+R2 finding C3; coerces None/empty `signal_type` to "unknown" per R1-I7) and `reset_on_correction(db, signal_type, venue, correction_at)` (zeroes `consecutive_no_correction` for the (signal_type, venue) pair when operator issues a correction; M1.5b operator manual SQL only — no automatic caller). New `LiveEngine._dispatch_live(paper_trade, size_usd)` private method — runs only when `LIVE_MODE='live'` AND `LIVE_USE_ROUTING_LAYER=True` AND `routing is not None`; calls `RoutingLayer.get_candidates` → picks top → adapter.place_order_request → adapter.await_fill_confirmation (using cid derived via `make_client_order_id(paper_trade.id, intent_uuid)` per R1-C2) → on terminal=`filled` only calls `correction_counter.increment_consecutive`. **Existing assert at `engine.py:88-91` (`assert self._config.mode != "live"`) is REMOVED** — M1.5b legitimately needs live mode to reach the engine; main.py boot guard at `scout/main.py:1062-1086` is the authoritative safety contract per R1+R2 finding C1. Existing shadow-mode flow (write `shadow_trades` row) preserved verbatim under `LIVE_MODE='shadow'`. New optional dependency on `LiveEngine.__init__`: `routing: RoutingLayer | None = None` — emits structlog WARN at construction if mode='live' AND flag=True AND routing=None (R1-I5 visibility). New Settings field `LIVE_USE_ROUTING_LAYER: bool = False` — default False = M1.5a behavior preserved (routing scaffold present but not wired); operator opts in to M1.5b's multi-venue dispatch by flipping this. M1.5b does NOT call `should_require_approval` (Telegram gateway runtime integration deferred to M1.5c — plan honestly closes V1-C1 as ROUTING-HALF only). M1.5b does NOT add recurring health probe, reconciler, `total_fills_lifetime` lifetime telemetry column, or V2 minor cleanups (all deferred to M1.5c).

# Live Trading Milestone 1.5b Implementation Plan — Engine Routing Dispatch + Correction Counter Writer

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** Close V1 review's CRITICAL findings C1 (engine doesn't call routing layer) + C2 (signal_venue_correction_count has zero writers). Wire `RoutingLayer.get_candidates` into engine dispatch under `LIVE_MODE='live'`. Wire correction counter increment-on-fill helper. Both wirings flag-gated behind `LIVE_USE_ROUTING_LAYER` (default False) to preserve M1.5a behavior under default config.

**Architecture:** `LiveEngine.on_paper_trade_opened` keeps existing shadow-mode flow intact. Adds a parallel `_dispatch_live()` private method that fires under `LIVE_MODE='live'` AND `LIVE_USE_ROUTING_LAYER=True`. The live path:
1. Calls `RoutingLayer.get_candidates(canonical, chain_hint, signal_type, size_usd)` — returns ranked list
2. If empty → log `live_dispatch_no_venue`; return (routing layer logged structural reason; engine does NOT write a separate live_trades reject row — `place_order_request` is the only writer of `live_trades` rows)
3. Picks top candidate
4. Calls `adapter.place_order_request(OrderRequest)` — idempotency-aware (M1.5a). Adapter computes `cid = make_client_order_id(paper_trade.id, intent_uuid)` internally and writes the live_trades row.
5. Calls `adapter.await_fill_confirmation(...)` with **the same `cid` from step 4** (NOT the raw `intent_uuid`) — polls until terminal (M1.5a). The `cid` format is `gecko-{paper_trade_id}-{uuid8}` per `scout/live/idempotency.py:make_client_order_id`; passing raw `intent_uuid` would cause `await_fill_confirmation`'s SELECT lookup at `binance_adapter.py:628` to return zero rows → RuntimeError.
6. On `status='filled'` ONLY → calls `correction_counter.increment_consecutive(db, signal_type, venue)`. Partial-fills are NOT counted (per V1-C2 reviewer fold: PARTIALLY_FILLED can transition to CANCELED for IOC/remaining-cancel; reconciler-domain).
7. On `status='partial'` / `'rejected'` / `'timeout'` → no counter increment.

**Per-venue counter intent (intentional):** `signal_venue_correction_count` PK is `(signal_type, venue)`. When M1.5c adds Kraken/Coinbase, each venue's counter starts at 0 — the operator gradually builds confidence per-venue (binance counter at 30 does NOT auto-clear approval for first kraken trade). This is the V1 design intent, not a bug.

**Counter-reset semantic (acknowledged simplification):** `reset_on_correction` zeros the entire `consecutive_no_correction` field for a `(signal_type, venue)` pair on a single operator unwind. Worked example: 30 successful fills → counter=30 → operator unwinds trade #31 → counter=0 → all 30 prior good fills lose their auto-clear progress. This semantic matches the field name (`consecutive_no_correction` = "consecutive trades without correction") but has UX consequence — runbook entry surfaces this so operators know one unwind costs the full streak. M1.5c reconciler may add a separate `total_fills_lifetime` column for dashboard telemetry that survives resets.

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
    db: Database, signal_type: str | None, venue: str
) -> None:
    """Increment consecutive_no_correction by 1 for (signal_type, venue).

    Creates the row on first call (ON CONFLICT...DO UPDATE). Counter is
    incremented only on terminal status='filled' (NOT 'partial' — see
    engine.py:_dispatch_live + plan-stage R1+R2 finding C3).

    Empty/None signal_type is coerced to "unknown" (R1-I7 fold) — this
    avoids a crash if a future dispatcher path emits empty signal_type
    (cashtag-dispatch path under BL-065 has historically produced empty
    values).
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    signal_type = signal_type or "unknown"
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
    db: Database, signal_type: str | None, venue: str, correction_at: str
) -> None:
    """Reset consecutive_no_correction to 0 + record last_corrected_at.

    Called from the operator-correction path (M1.5c when reconciler
    detects a 24h-window unwind; M1.5b operator can call directly via
    SQL for manual corrections).

    SEMANTIC ACKNOWLEDGMENT (plan-stage R2 finding C2): a single reset
    zeros the ENTIRE consecutive_no_correction field for the
    (signal_type, venue) pair. Worked example: 30 fills → counter=30 →
    operator unwinds trade #31 → counter=0 → all 30 prior good fills
    lose their auto-clear-approval progress. This semantic matches the
    field name (`consecutive_no_correction` = "consecutive trades
    without correction") and matches V1's gate intent ("trust requires
    UNBROKEN streak"), but has UX consequence — runbook entry surfaces
    this. M1.5c reconciler may add `total_fills_lifetime` for dashboard
    telemetry that survives resets.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    signal_type = signal_type or "unknown"
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

- [ ] **Step 1: Remove M1.5a's `assert mode != "live"` guard at engine.py:88-91**

R1+R2 plan-stage CRITICAL finding C1: the existing assert at engine.py:88-91 (`assert self._config.mode != "live"`) blocks ALL paths reaching the engine in live mode — including the new `_dispatch_live` branch added below. Without removing it, M1.5b's dispatch is structurally unreachable and operator activation crashes with `AssertionError` on the first live signal.

Replace the assert (lines 88-91) with a comment documenting that main.py boot guard at `scout/main.py:1062-1086` enforces `LIVE_TRADING_ENABLED=True` for live mode — that boot guard is the safety contract; the runtime assert is no longer needed because M1.5b legitimately wants live-mode flows to reach the engine. The docstring above the method (lines 73-87) should also be updated to remove the "engine entry NO LONGER short-circuits on master kill" line and replace with "M1.5b: live mode dispatch is enabled when `LIVE_USE_ROUTING_LAYER=True`."

```python
        # M1.5b: live mode dispatch is permitted. main.py boot guards
        # (scout/main.py:1062-1086) enforce LIVE_TRADING_ENABLED=True +
        # LIVE_USE_REAL_SIGNED_REQUESTS=True for mode='live'. The
        # assertion that previously blocked live mode here is removed
        # because M1.5b's _dispatch_live path legitimately fires under
        # mode='live' AND LIVE_USE_ROUTING_LAYER=True.
```

- [ ] **Step 2: Refactor `on_paper_trade_opened`**

Add `LIVE_USE_ROUTING_LAYER` branch. Existing shadow flow preserved verbatim under `mode='shadow'` OR `mode='live' and not flag_set`.

```python
    async def on_paper_trade_opened(self, paper_trade: _PaperTradeLike) -> None:
        # M1.5b: assert removed (see Step 1).
        # ... existing master-kill check, gates, allowlist, etc unchanged ...

        # After gates pass + entry_vwap computed + shadow_trades row written:
        # ... existing happy-path code unchanged for shadow mode ...

        # M1.5b live-mode dispatch (V1-C1 routing-half closure)
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
        """M1.5b live-mode dispatch (V1-C1 routing-half + V1-C2 closures).

        - Routes via RoutingLayer
        - Calls adapter.place_order_request (M1.5a idempotency-aware)
        - Calls adapter.await_fill_confirmation (M1.5a polling) with the
          same cid the adapter just wrote to live_trades
        - On terminal=filled → increment correction counter
        - On BinanceAuthError mid-session → engages KillSwitch (R1-M1)
        - On no candidates → writes live_trades reject row (R2-M1 / Q2)
        """
        from uuid import uuid4
        from scout.live.adapter_base import OrderRequest
        from scout.live.binance_adapter import (
            BinanceAuthError,
            BinanceIPBanError,
        )
        from scout.live.correction_counter import increment_consecutive
        from scout.live.exceptions import VenueTransientError
        from scout.live.idempotency import make_client_order_id

        canonical = paper_trade.symbol
        chain_hint = getattr(paper_trade, "chain", None)

        # R2-I2 fold: entry telemetry — operator can grep "did
        # _dispatch_live even fire for this signal?"
        log.info(
            "live_dispatch_entered",
            paper_trade_id=paper_trade.id,
            canonical=canonical,
            size_usd=float(size_usd),
            signal_type=paper_trade.signal_type,
        )

        candidates = await self._routing.get_candidates(
            canonical=canonical,
            chain_hint=chain_hint,
            signal_type=paper_trade.signal_type,
            size_usd=float(size_usd),
        )

        # R2-I2 fold: candidate-count visibility
        log.info(
            "live_dispatch_candidates_returned",
            paper_trade_id=paper_trade.id,
            count=len(candidates),
            top_venue=candidates[0].venue if candidates else None,
        )

        if not candidates:
            # R2-M1 / §7 Q2 fold: write a live_trades reject row so the
            # dashboard /api/live_trades surface shows the silent-failure
            # mode. reject_reason 'no_venue' is in M1.5a's CHECK list.
            await self._db._conn.execute(
                "INSERT INTO live_trades "
                "(paper_trade_id, status, reject_reason, created_at) "
                "VALUES (?, 'rejected', 'no_venue', ?)",
                (paper_trade.id, datetime.now(timezone.utc).isoformat()),
            )
            await self._db._conn.commit()
            log.info(
                "live_dispatch_no_venue",
                paper_trade_id=paper_trade.id,
                canonical=canonical,
            )
            return

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
        # R1-C2 fix: derive the same cid the adapter writes to
        # live_trades.client_order_id. await_fill_confirmation's SELECT
        # lookup at binance_adapter.py:628 uses this cid; passing raw
        # intent_uuid here would return zero rows → RuntimeError.
        cid = make_client_order_id(paper_trade.id, intent_uuid)

        try:
            venue_order_id = await self._adapter.place_order_request(request)
        except NotImplementedError as exc:
            # Defense-in-depth: §2.2 misconfig CRASH should prevent
            # reaching here (engine __init__ refuses to construct under
            # routing+signed misconfig). If we still hit it (e.g.,
            # operator runtime-edited .env without restart), log + return.
            log.info(
                "live_dispatch_signed_disabled",
                paper_trade_id=paper_trade.id,
                err=str(exc),
            )
            return
        except BinanceAuthError as exc:
            # R1-M1 fold: API key revoked mid-session. Gates already
            # approved, so this is severe — engage KillSwitch and stop
            # all subsequent live dispatches until operator investigates.
            log.error(
                "live_dispatch_auth_revoked_mid_session",
                paper_trade_id=paper_trade.id,
                err=str(exc),
            )
            await self._kill_switch.engage(
                reason="binance_auth_revoked_mid_session"
            )
            return
        except BinanceIPBanError as exc:
            # R1-M1 fold: IP banned. KillSwitch + Telegram alert.
            log.error(
                "live_dispatch_ip_banned",
                paper_trade_id=paper_trade.id,
                err=str(exc),
            )
            await self._kill_switch.engage(reason="binance_ip_banned")
            return
        except VenueTransientError as exc:
            # Known transient class — log INFO, next signal will retry.
            log.info(
                "live_dispatch_venue_transient",
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
                client_order_id=cid,  # R1-C2 fix: full cid, not raw intent_uuid
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
        # successful fill ONLY. R1+R2 plan-stage finding C3: PARTIALLY_FILLED
        # can transition to CANCELED for IOC orders, so partial fills are
        # NOT counted to avoid double-count when the eventual terminal is
        # observed by the M1.5c reconciler. Resets fire from operator-
        # correction path (manual SQL or M1.5c reconciler).
        if confirmation.status == "filled":
            await increment_consecutive(
                self._db, paper_trade.signal_type, top.venue
            )

- [ ] **Step 3: Update `LiveEngine.__init__` to accept routing param + CRASH on misconfig**

R2-C1 + R2-I3 + R1-M2 fold: replace the planned WARN log with a fail-closed CRASH covering BOTH (a) `LIVE_USE_ROUTING_LAYER=True AND LIVE_USE_REAL_SIGNED_REQUESTS=False` (operator forgot signed flag → silent no-op) and (b) `LIVE_USE_ROUTING_LAYER=True AND routing=None` (main.py wiring forgot kwarg).

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

        # R2-C1 + R2-I3 + R1-M2 fold: fail-closed CRASH on misconfig.
        # Cost-of-crash is bounded by systemd RestartSec=30s +
        # StartLimitBurst=3 + OnFailure Telegram (M1.5a runbook §1+§2).
        # Cost-of-WARN-and-skip is unbounded (operator walkaway = arbitrary
        # missed signals at observed ~1.8 signals/hr prod rate).
        if config.mode == "live":
            flag_routing = getattr(
                config._s, "LIVE_USE_ROUTING_LAYER", False
            )
            flag_signed = getattr(
                config._s, "LIVE_USE_REAL_SIGNED_REQUESTS", False
            )
            if flag_routing and not flag_signed:
                raise RuntimeError(
                    "Misconfig: LIVE_USE_ROUTING_LAYER=True but "
                    "LIVE_USE_REAL_SIGNED_REQUESTS=False. Engine would "
                    "silently no-op every signal. Set "
                    "LIVE_USE_REAL_SIGNED_REQUESTS=True or "
                    "LIVE_USE_ROUTING_LAYER=False before boot."
                )
            if flag_routing and routing is None:
                raise RuntimeError(
                    "Misconfig: LIVE_USE_ROUTING_LAYER=True but "
                    "routing=None. Check scout/main.py construction "
                    "passes routing=live_routing kwarg to LiveEngine."
                )
```

- [ ] **Step 4: Tests in `tests/test_live_engine_dispatch.py`**

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
async def test_dispatch_live_uses_full_cid_for_await_fill(tmp_path):
    """R1-C2 regression: await_fill_confirmation receives
    'gecko-{paper_trade_id}-{uuid8}' cid format, NOT raw intent_uuid.
    Verifies make_client_order_id is called with (paper_trade.id, intent_uuid)
    and the resulting cid is passed to await_fill_confirmation."""
    # ... stub routing returns 1 candidate ...
    # ... stub adapter.place_order_request returns 'BNX-1' ...
    # ... capture await_fill_confirmation kwargs ...
    # ... assert kwargs['client_order_id'] starts with 'gecko-' and != raw intent_uuid


@pytest.mark.asyncio
async def test_dispatch_live_increments_counter_on_filled(tmp_path):
    """Top candidate + adapter returns FILLED → counter incremented."""
    # ... stub routing returns [RouteCandidate(binance, BTCUSDT, ...)] ...
    # ... stub adapter.place_order_request returns 'BNX-12345' ...
    # ... stub adapter.await_fill_confirmation returns confirmation status='filled' ...
    # ... call _dispatch_live ...
    # ... assert signal_venue_correction_count[(first_signal, binance)].consecutive_no_correction == 1


@pytest.mark.asyncio
async def test_dispatch_live_no_counter_on_partial(tmp_path):
    """R1+R2 C3 regression: partial fills do NOT increment counter.
    PARTIALLY_FILLED can transition to CANCELED — reconciler-domain."""
    # ... stub adapter.await_fill_confirmation returns status='partial' ...
    # ... assert correction_counter.consecutive_no_correction == 0


@pytest.mark.asyncio
async def test_dispatch_live_no_counter_on_timeout(tmp_path):
    """Adapter returns status='timeout' → counter NOT incremented."""
    # ... assert correction_counter.consecutive_no_correction == 0


@pytest.mark.asyncio
async def test_dispatch_live_no_op_when_signed_disabled(tmp_path):
    """LIVE_USE_REAL_SIGNED_REQUESTS=False → place_order_request raises
    NotImplementedError → engine logs + returns silently."""


@pytest.mark.asyncio
async def test_live_mode_routing_layer_off_does_not_call_routing(tmp_path):
    """R1-I6 regression: mode='live' AND LIVE_USE_ROUTING_LAYER=False →
    existing M1.5a flow runs WITHOUT calling routing.get_candidates.
    Protects against future refactor moving routing call before flag check."""
    # ... construct engine with mode='live', LIVE_USE_ROUTING_LAYER=False ...
    # ... routing stub records all calls ...
    # ... call on_paper_trade_opened ...
    # ... assert routing.get_candidates was NEVER called


@pytest.mark.asyncio
async def test_engine_init_warns_when_routing_flag_set_but_layer_none(tmp_path, caplog):
    """R1-I5 regression: structlog WARN emitted when mode='live' AND
    LIVE_USE_ROUTING_LAYER=True AND routing=None at construction."""
    # ... construct engine with mode='live', flag=True, routing=None ...
    # ... assert 'live_routing_flag_set_but_layer_missing' event emitted


@pytest.mark.asyncio
async def test_shadow_mode_unchanged_when_routing_layer_off(tmp_path):
    """LIVE_USE_ROUTING_LAYER=False (default) + mode='shadow' → existing
    M1.5a flow runs (no _dispatch_live call). M1.5a tests stay green."""
```

- [ ] **Step 5: Counter test — empty/None signal_type semantic**

R1-I7 fold: define behavior for `signal_type=None` or empty string. Decision: **coerce to "unknown"** so the counter still tracks the (signal_type='unknown', venue) pair rather than crashing — matches the resilient posture of the existing dispatch path. Add to `tests/test_live_correction_counter.py`:

```python
@pytest.mark.asyncio
async def test_increment_handles_none_signal_type(tmp_path):
    """signal_type=None coerced to 'unknown' (not crash, not silent skip)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await increment_consecutive(db, None, "binance")  # type: ignore
    cur = await db._conn.execute(
        "SELECT signal_type, consecutive_no_correction "
        "FROM signal_venue_correction_count WHERE venue = ?",
        ("binance",),
    )
    row = await cur.fetchone()
    assert row[0] == "unknown"
    assert row[1] == 1
    await db.close()
```

Update `increment_consecutive` (Task 1) to coerce: `signal_type = signal_type or "unknown"`. Same coercion for `reset_on_correction`.

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
- **V1-C1 routing-half closure**: routing layer is called from engine under flag-gated live mode (R2-I3 fold: tightened from "V1-C1 closure" — Telegram approval gateway runtime hook is M1.5c scope, so V1-C1 is only HALF closed by M1.5b)
- V1-C2 closure: signal_venue_correction_count has writers (filled-only; reset semantic acknowledged)
- LIVE_USE_ROUTING_LAYER defaults False — M1.5a behavior preserved unless operator opts in
- M1.5a's `assert mode != "live"` removed at engine.py:88-91 (R1+R2 plan-stage finding C1)
- `_dispatch_live` uses `make_client_order_id(paper_trade.id, intent_uuid)` for `await_fill_confirmation` cid (R1 plan-stage finding C2)
- Engine __init__ logs WARN if routing flag set but layer is None (R1-I5)
- M1.5c plan can be drafted (recurring health probe + reconciler + Telegram approval gateway runtime + minor cleanups)

## What this milestone does NOT do (M1.5c scope)

- Does NOT call `should_require_approval` from engine (Telegram gateway runtime hook) — V1-C1 approval-half deferred
- Does NOT add recurring health probe (boot-time smoke is point-in-time; first M1.5b live dispatch fires WITHOUT venue_health row, falling back to routing's default 0.5 score — R2-I1 risk surfaced in runbook)
- Does NOT add reconciliation worker for orphaned live_trades rows (in-flight orders mid-restart are operator-manual cleanup per `docs/runbooks/live-trading-deploy.md` §6 — R1-M10 / R2-M2 fold)
- Does NOT auto-call `reset_on_correction` from any path (M1.5b operator manual SQL only — R1-I4 fold)
- Does NOT bundle V2 deferred minors (ServiceRunner cancel-log, view CAST symmetry, override-NULL filter, venue_health staleness gate)
- Does NOT add `total_fills_lifetime` column for dashboard telemetry that survives resets (M1.5c may add — counter-reset UX cost acknowledged but not mitigated here)

## Operator activation prereqs (post-M1.5b deploy)

In addition to M1.5a prereqs already documented in `project_live_m1_5a_shipped_2026_05_09.md`:

1. Flip `LIVE_USE_ROUTING_LAYER=True` in `.env`
2. **Acknowledge first-signal venue_health gap (R2-I1):** the first M1.5b live dispatch fires before any `venue_health` row exists; routing's health filter defaults to score 0.5. The boot-time smoke check validates auth + read-only paths but does not write a venue_health row. Operator should treat the FIRST live dispatch as a verification trade and watch for an immediate fill confirmation — if any anomaly, kill-switch via flag-flip + restart.
3. **Acknowledge counter-reset UX cost:** any operator unwind (manual SQL or M1.5c reconciler) zeros the entire `consecutive_no_correction` for the (signal_type, venue) pair. After 30 successful fills, one unwind costs the full streak.
4. Per-venue counters intended: when M1.5c adds a second venue, that venue's counter starts at 0 (not auto-cleared from binance progress) — this is the V1 design intent.

## Reversibility

**Fast revert (in-flight-tolerant):** `LIVE_USE_ROUTING_LAYER=False` in `.env` → restart. Engine falls back to M1.5a's BL-055 single-venue resolver path; no NEW live trades are dispatched via routing. **In-flight caveat:** if the engine restart happens between `place_order_request` and `await_fill_confirmation`, the order is live on Binance with no engine watcher; the live_trades row stays `status='open'`. M1.5c reconciler will close these; for M1.5b, operator manual cleanup per `docs/runbooks/live-trading-deploy.md` §6 (R1-M10 / R2-M2).

**Slower revert (git):** `git revert <PR squash>` + LIVE_MODE='paper' before the revert (otherwise the restored M1.5a `assert mode != "live"` immediately crashes engine entry under live mode).
