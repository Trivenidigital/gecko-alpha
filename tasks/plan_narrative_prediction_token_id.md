# narrative_prediction token_id divergence — combined plan + design

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans.

**New primitives introduced:** new structured log event `signal_skipped_synthetic_token_id` (parallels existing `signal_skipped_junk` shape at `scout/trading/signals.py`); new pre-open validation gate at `trade_predictions` dispatcher (`scout/trading/signals.py:~748`) that REJECTS the prediction if `pred.coin_id` is missing from BOTH `price_cache` AND the BL-076 `Database.lookup_symbol_name_by_coin_id` resolution chain (`gainers_snapshots` / `volume_history_cg` / `volume_spikes`); no DB schema changes; no Settings changes.

**Combined plan + design rationale:** scope is ~10 LOC behavioral change + 2-3 tests. Full plan + 2 reviewers + design + 2 reviewers cycle is overkill for scope; combined doc + 2 reviewers is sufficient rigor.

---

## Hermes-first analysis

**Domains checked against the 671-skill hub at `hermes-agent.nousresearch.com/docs/skills` (verified 2026-05-04):**

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Token-id resolution / synthetic-ID detection in trading systems | None | Build inline (extend existing dispatcher gate pattern) |
| CoinGecko API response data quality / disambiguation | None | Build inline |
| Pre-open validation gates at trade dispatch | None | Build inline (matches PR #44 / BL-076 dispatcher patterns) |

**Verdict:** Pure project-internal data-quality gate. Building inline by extending `scout/trading/signals.py:trade_predictions` dispatcher with the same `signal_skipped_*` telemetry pattern that BL-076 + PR #44 use.

---

## Drift grounding (per alignment doc Part 3)

**Read before drafting (verified):**

- `scout/trading/signals.py:638-773` — `trade_predictions(db, predictions, engine, settings)` dispatcher.
- `scout/trading/signals.py:747-754` — existing price_cache fetch (returns None gracefully if not found, then passes None to `engine.open_trade(entry_price=None)`). Currently REACTIVE — engine logs `trade_skipped_no_price` AFTER trying to open.
- `scout/trading/signals.py:565-622` — `_is_junk_coinid` + `_is_tradeable_candidate` filters (existing dispatcher gates). The new gate parallels these.
- `scout/trading/signals.py:34/132/...` — `signal_skipped_*` event family (existing telemetry shape).
- `scout/trading/engine.py:103-115` + `:216-231` — `open_trade(token_id, ...)` and the reactive `trade_skipped_no_price` log.
- `scout/db.py:90-189` — `Database.lookup_symbol_name_by_coin_id(coin_id) -> tuple[str, str]` (BL-076; sequential lookup across `gainers_snapshots` / `volume_history_cg` / `volume_spikes` with per-table `OperationalError` narrowing). Currently used by `trade_chain_completions` only.
- `scout/narrative/predictor.py:108-112` — `coin_id = t.get("id", "")` filter on CoinGecko `/coins/markets` response. Doesn't catch synthetic-but-not-empty cases.
- `scout/trading/params.py:47-49` — `CALIBRATION_EXCLUDE_SIGNALS` already excludes `narrative_prediction` from auto-calibration with the comment "narrative_prediction has known token_id divergence (32/56 stale-young rows in late-April audit) — outcomes are partly noise from upstream".

**Bug evidence:**
- Memory file `project_paper_evaluator_zombie_fix_2026_04_27.md`: "narrative_prediction has divergent token_ids (32 of 56 stale-young); separate upstream issue."
- Verified by inspection of `tasks/todo.md` line 81: "32 of 56 stale-young open trades have empty/synthetic token_ids that don't appear in `price_cache`. Separate upstream fix."
- Reactive engine log `trade_skipped_no_price` already fires for these but engine path is past the point of dispatcher visibility.

**Pattern conformance:**
- `signal_skipped_*` telemetry: matches `scout/trading/signals.py:34/132/565-622` shape. Operator-aggregator dashboards already pivot on this prefix.
- `Database.lookup_symbol_name_by_coin_id`: already used by `trade_chain_completions` at `signals.py:843`. Reusing it for narrative_prediction is the consistent choice.
- Pre-open gate: matches PR #44 (junk filter) + BL-076 (engine WARNING) sequencing — gates fire BEFORE engine.open_trade.

---

**Goal:** Stop opening narrative_prediction paper trades for tokens whose `coin_id` does not resolve in `price_cache` (primary) OR the BL-076 lookup chain (fallback for race scenarios where price_cache is delayed).

**Architecture:** Single behavioral change in `scout/trading/signals.py:trade_predictions` dispatcher. Add a check after the existing junk/marketcap/narrative-fit gates and BEFORE the existing price fetch. Order:

1. Existing gates: marketcap (line 717), narrative fit (line 723), junk categories (line 726), junk coin_id (line 732)
2. **NEW gate (this PR):** price_cache existence OR BL-076 fallback resolution
3. Existing: price fetch (line 748) → engine.open_trade

If the new gate fails: emit `signal_skipped_synthetic_token_id` log + `continue`.

If `price_cache` has the row: pass-through (existing behavior).

If `price_cache` is missing BUT `lookup_symbol_name_by_coin_id` finds the coin in any of the 3 snapshot tables: pass-through. The lookup-fallback handles the race where `price_cache` is being populated but hasn't caught up to a fresh prediction.

If both fail: REJECT with telemetry.

**Tech Stack:** Python 3.12, async via aiosqlite, structlog, pytest + pytest-asyncio. No new dependencies.

---

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `scout/trading/signals.py` | Add pre-open validation gate in `trade_predictions` dispatcher (~10 LOC + structured log event) | Modify |
| `tests/test_narrative_prediction_token_id.py` | New test file — 3 tests pinning the gate behavior | Create |

---

## Tasks

### Task 1: Write the failing tests

**Files:**
- Create: `tests/test_narrative_prediction_token_id.py`

- [ ] **Step 1: Write test file**

```python
"""narrative_prediction token_id divergence — pre-open validation gate.

Pins the BL-076-style dispatcher pattern: refuse to open a paper trade
for a prediction whose coin_id doesn't resolve in price_cache OR the
BL-076 lookup chain (gainers_snapshots / volume_history_cg /
volume_spikes). Telemetry: signal_skipped_synthetic_token_id event.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest
from structlog.testing import capture_logs

_SKIP_AIOHTTP = pytest.mark.skipif(
    sys.platform == "win32" and os.environ.get("SKIP_AIOHTTP_TESTS") == "1",
    reason="Windows + SKIP_AIOHTTP_TESTS=1: skip aiohttp tests",
)


@pytest.fixture
async def db(tmp_path):
    from scout.db import Database
    d = Database(tmp_path / "t.db")
    await d.initialize()
    yield d
    await d.close()


class _StubEngine:
    """Captures engine.open_trade calls without touching DB further."""
    def __init__(self):
        self.opened: list[dict] = []

    async def open_trade(self, **kwargs):
        self.opened.append(kwargs)


def _make_pred(coin_id="real-coin", symbol="REAL", name="Real Coin"):
    """Build a NarrativePrediction-shaped object minimal for dispatcher."""
    from types import SimpleNamespace
    return SimpleNamespace(
        coin_id=coin_id,
        symbol=symbol,
        name=name,
        market_cap=10_000_000.0,
        narrative_fit_score=0.85,
        narrative_category="ai",
        narrative_summary="test",
        chain="ethereum",
    )


@pytest.mark.asyncio
async def test_synthetic_token_id_rejected_with_telemetry(db, settings_factory):
    """T1 — coin_id not in price_cache and not in lookup chain → reject
    with signal_skipped_synthetic_token_id event; engine.open_trade NOT called."""
    from scout.trading.signals import trade_predictions
    settings = settings_factory()
    engine = _StubEngine()
    pred = _make_pred(coin_id="synthetic-coin-xyz")
    with capture_logs() as logs:
        await trade_predictions(db, [pred], engine, settings)
    events = [e.get("event") for e in logs]
    assert "signal_skipped_synthetic_token_id" in events
    assert engine.opened == []  # no trade opened


@pytest.mark.asyncio
async def test_legit_in_price_cache_opens_trade(db, settings_factory):
    """T2 — coin_id present in price_cache → trade opens (existing behavior
    preserved; gate is a refusal-only addition)."""
    from scout.trading.signals import trade_predictions
    settings = settings_factory()
    engine = _StubEngine()
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        ("legit-coin", 1.0, now),
    )
    await db._conn.commit()
    pred = _make_pred(coin_id="legit-coin")
    with capture_logs() as logs:
        await trade_predictions(db, [pred], engine, settings)
    events = [e.get("event") for e in logs]
    assert "signal_skipped_synthetic_token_id" not in events
    assert len(engine.opened) == 1
    assert engine.opened[0]["token_id"] == "legit-coin"


@pytest.mark.asyncio
async def test_missing_from_price_cache_but_in_lookup_chain_opens_trade(
    db, settings_factory
):
    """T3 — race scenario: coin_id missing from price_cache but PRESENT in
    gainers_snapshots (BL-076 lookup chain). Fallback path accepts;
    engine.open_trade called even though entry_price is None (matches
    existing behavior of the price-fetch fall-through)."""
    from scout.trading.signals import trade_predictions
    settings = settings_factory()
    engine = _StubEngine()
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO gainers_snapshots "
        "(coin_id, symbol, name, price_change_24h, market_cap, "
        " volume_24h, price_at_snapshot, snapshot_at) "
        "VALUES ('race-coin', 'RACE', 'Race', 12.0, 5000000, 1000, 1.0, ?)",
        (now,),
    )
    await db._conn.commit()
    pred = _make_pred(coin_id="race-coin")
    with capture_logs() as logs:
        await trade_predictions(db, [pred], engine, settings)
    events = [e.get("event") for e in logs]
    assert "signal_skipped_synthetic_token_id" not in events
    assert len(engine.opened) == 1
    assert engine.opened[0]["token_id"] == "race-coin"
```

- [ ] **Step 2: Run tests to verify they FAIL**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_narrative_prediction_token_id.py -v --tb=short
```

Expected: T1 FAILS (rejection event not emitted), T2 + T3 likely fail at the dispatcher schema (depending on existing fixtures).

---

### Task 2: Implement the gate

**Files:**
- Modify: `scout/trading/signals.py:trade_predictions`

- [ ] **Step 1: Insert validation gate**

In `scout/trading/signals.py:trade_predictions` dispatcher, after the existing junk-coin_id check at line 732 and BEFORE the existing price_cache fetch at line 748:

```python
            # narrative_prediction token_id validation (this PR):
            # Reject predictions whose coin_id does not resolve in
            # price_cache OR the BL-076 lookup chain (gainers_snapshots /
            # volume_history_cg / volume_spikes). 32 of 56 stale-young
            # open trades had synthetic/empty token_ids; engine's reactive
            # trade_skipped_no_price log fires too late to gate the trade.
            #
            # Fallback to lookup_symbol_name_by_coin_id handles the race
            # where price_cache hasn't caught up to a fresh prediction.
            cur = await db._conn.execute(
                "SELECT 1 FROM price_cache WHERE coin_id = ? LIMIT 1",
                (pred.coin_id,),
            )
            in_price_cache = (await cur.fetchone()) is not None
            if not in_price_cache:
                # Race-tolerant fallback: BL-076 lookup chain.
                fallback_symbol, fallback_name = (
                    await db.lookup_symbol_name_by_coin_id(pred.coin_id)
                )
                in_lookup_chain = bool(fallback_symbol and fallback_name)
                if not in_lookup_chain:
                    log.info(
                        "signal_skipped_synthetic_token_id",
                        coin_id=pred.coin_id,
                        symbol=pred.symbol,
                        signal_type="narrative_prediction",
                        signal_combo="narrative_prediction",
                        reason="token_id not in price_cache or BL-076 lookup chain",
                    )
                    continue
```

- [ ] **Step 2: Run tests to verify GREEN**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_narrative_prediction_token_id.py -v --tb=short
```

Expected: 3 PASS.

- [ ] **Step 3: Regression sweep**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_narrative_prediction_token_id.py tests/test_paper_evaluator.py tests/test_bl076_junk_filter_and_symbol_name.py tests/test_signal_params.py tests/test_paper_trader.py -q
```

Expected: all PASS (no regression).

- [ ] **Step 4: Commit**

```bash
git add scout/trading/signals.py tests/test_narrative_prediction_token_id.py
git commit -m "feat: narrative_prediction token_id validation gate

Reject predictions whose coin_id doesn't resolve in price_cache OR the
BL-076 lookup chain (gainers_snapshots/volume_history_cg/volume_spikes).
Closes the upstream-data-quality gap that produced 32/56 stale-young
narrative_prediction trades with synthetic token_ids in late-April audit.

Telemetry: signal_skipped_synthetic_token_id event matches existing
signal_skipped_* family pattern (signals.py:34/132/...). Operator
aggregator dashboards already pivot on this prefix.

3 tests pin the behavior:
- T1: synthetic coin_id → reject + telemetry
- T2: legit coin_id in price_cache → open (preserved behavior)
- T3: race-window coin_id missing from price_cache but present in
  lookup chain → open via fallback (no false-positive rejection)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Test matrix

| ID | Test | Layer | What it pins |
|---|---|---|---|
| T1 | `test_synthetic_token_id_rejected_with_telemetry` | Integration (dispatcher) | Reject + `signal_skipped_synthetic_token_id` event fires; engine.open_trade NOT called |
| T2 | `test_legit_in_price_cache_opens_trade` | Integration (dispatcher) | Existing behavior preserved (gate is refusal-only) |
| T3 | `test_missing_from_price_cache_but_in_lookup_chain_opens_trade` | Integration (dispatcher) | Race-window fallback works; no false-positive rejection of fresh predictions |

3 active tests. Zero deferred.

---

## Failure modes (silent-failure-first ordering)

| # | Failure | Silent or loud? | Mitigation in this PR | Residual risk |
|---|---|---|---|---|
| F1 | Synthetic coin_id slips through gate (e.g., new placeholder family from CoinGecko like `demo-N`) | **Silent** (paper trade opens against junk; closes via `expired_stale_no_price` per zombie safeguard PR #54) | Gate uses `Database.lookup_symbol_name_by_coin_id` which checks 3 distinct sources — synthetic IDs miss all 3 because no real ingestion path emits them. New placeholder families would also miss all 3 unless they appear in real CoinGecko `/coins/markets` AND the snapshot tables, which makes them "real" by definition | Acceptable — relies on the snapshot-table pipeline being the source-of-truth for "real" coin_ids |
| F2 | False-positive rejection of legit prediction whose coin_id is mid-flight (price_cache writer hasn't committed yet) | **Loud** (`signal_skipped_synthetic_token_id` fires for legit token; operator dashboards aggregate it) | T3 — the `lookup_symbol_name_by_coin_id` fallback chain catches predictions that are visible in any of 3 snapshot tables, which the price_cache writer reads from. The race window is narrow (snapshot tables populate before price_cache; if both miss, the prediction's underlying coin doesn't exist in our ingestion at all) | Operator can grep `signal_skipped_synthetic_token_id` to spot any legit token loss and investigate |
| F3 | `Database.lookup_symbol_name_by_coin_id` raises `OperationalError` (column rename in one of the 3 source tables) | **Loud** (per BL-076's narrowed `OperationalError` handling — re-raises) | BL-076 already handles per-table narrowing; this PR inherits | None |
| F4 | Operator removes the gate via PR revert; old behavior returns | **Silent** (synthetic-id trades resume opening; closed via zombie safeguard PR #54 within max_duration_hours) | Acceptable — reverting this PR is an explicit operator decision; the underlying zombie-safeguard mitigates trade longevity | Acceptable |
| F5 | `pred.coin_id` is `None` (Pydantic model permits Optional) | **Loud** (`SELECT ... WHERE coin_id = NULL` always returns no rows; gate rejects with `signal_skipped_synthetic_token_id`) | T1 covers via the `synthetic-coin-xyz` (string) path; if model permits None, it surfaces here loudly via the same gate | None |

**Silent-failure count: 2 / Loud: 3.**

---

## Performance notes

- Gate adds at most 2 indexed `SELECT 1 ... LIMIT 1` queries per prediction:
  - `price_cache (coin_id PK)` — single hash-style lookup, O(log n) ~< 1ms
  - `Database.lookup_symbol_name_by_coin_id` — 3 indexed lookups in worst case (BL-076 verified all 3 indexes)
- At observed N=10-30 predictions per cycle, ≤120 lookups/cycle = <2ms additional DB cost. Negligible.

---

## Rollback

Pure code revert — no DB schema changes, no migration, no Settings changes:

```bash
ssh root@89.167.116.187 "cd /root/gecko-alpha && systemctl stop gecko-pipeline && git checkout <prev-master-sha> && find . -name __pycache__ -exec rm -rf {} + && systemctl start gecko-pipeline"
```

Verification post-rollback: `journalctl -u gecko-pipeline | grep signal_skipped_synthetic_token_id` returns empty (gate removed); next cycle resumes opening synthetic-id trades (regression to pre-fix behavior — expected; only ~32/cycle of 56 narrative predictions affected, low-risk to wait for re-deploy).

---

## Operational verification (§5)

**Pre-deploy:**
- Capture journalctl error baseline: `BASELINE=$(journalctl -u gecko-pipeline --since "10 minutes ago" --no-pager | grep -ciE "error|exception|traceback")`
- Capture current synthetic-id count: `sqlite3 /root/gecko-alpha/scout.db "SELECT COUNT(*) FROM paper_trades WHERE signal_type = 'narrative_prediction' AND status = 'open' AND token_id NOT IN (SELECT coin_id FROM price_cache)"`

**Stop-FIRST sequence** (BL-076 lesson):
1. `systemctl stop gecko-pipeline`
2. `git pull origin master`
3. `find . -name __pycache__ -type d -exec rm -rf {} +`
4. `systemctl start gecko-pipeline`
5. `systemctl is-active gecko-pipeline` → expect `active`

**Post-deploy verification (~30 min after restart):**
- `journalctl -u gecko-pipeline --since "30 minutes ago" | grep signal_skipped_synthetic_token_id | head -10` → expect entries with synthetic coin_ids
- Compare error count vs baseline → ≤ baseline
- New narrative_prediction paper_trades count growing only with resolvable token_ids: `sqlite3 ... "SELECT signal_type, token_id, opened_at FROM paper_trades WHERE signal_type='narrative_prediction' ORDER BY opened_at DESC LIMIT 5"` → check token_ids look real

**Soak-then-escalate criterion:** 7 days of zero false-positive rejections (i.e., zero `signal_skipped_synthetic_token_id` events for tokens that DO have real CoinGecko coin_ids — operator-spot-check by grepping a sample). If clean, the gate stays.

---

## Self-Review

1. **Hermes-first:** ✓ table + verdict per convention. 3/3 negative.
2. **Drift grounding:** ✓ explicit file:line refs to `signals.py:638-773` + `Database.lookup_symbol_name_by_coin_id` + `predictor.py:108-112`. Memory file referenced.
3. **Test matrix:** 3 active tests covering reject + accept + race-fallback. Zero deferred.
4. **Failure modes:** 5/5 enumerated, silent-failure-first ordered. F1+F4 silent (acceptable, mitigated by snapshot-pipeline truth + zombie safeguard); F2+F3+F5 loud.
5. **Performance:** ≤2 indexed lookups per prediction. Negligible.
6. **Rollback:** code-only; pure revert.
7. **Combined plan+design rationale:** scope is ~10 LOC + 3 tests; full pipeline overkill; combined doc + 2 reviewers preserves rigor without ceremony.
8. **No DDL changes:** verified.
9. **Honest scope:**
   - **NOT in scope:** fix at the upstream `scout/narrative/predictor.py:108-112` source (the CoinGecko `/coins/markets` filter). Reason: that fix would require knowing what makes a coin "synthetic" upstream, which is harder; the dispatcher gate is the cheaper + safer place to enforce.
   - **NOT in scope:** retrofit existing 32 stale-young open trades. They will close via the zombie safeguard's `expired_stale_no_price` path within their `max_duration_hours`. No backfill needed.
   - **NOT in scope:** apply same gate to `trade_volume_spikes` or `trade_chain_completions`. The audit only flagged narrative_prediction; if other dispatchers exhibit the same pattern, separate PR.
10. **No production code that auto-arms:** this PR is pure refusal logic; only effect is rejecting predictions that would have failed at the engine layer anyway. Safer than current state.
