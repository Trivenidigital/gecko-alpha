**New primitives introduced:** Same as `plan_bl075_phase_b_slow_burn.md` — `detect_slow_burn_7d` function in `scout/spikes/detector.py`, `slow_burn_candidates` table with `also_in_momentum_7d` flag, migration `bl_slow_burn_v1` (schema_version 20260515), 6 Settings (`SLOW_BURN_*`), main.py wiring, runbook, tests.

# Design — BL-075 Phase B: Slow-burn watcher

Plan: `plan_bl075_phase_b_slow_burn.md` (R1 statistical + R2 code-structural plan-stage reviewer fixes folded; RIV pre-merge back-check completed 2026-05-10).

## Hermes-first analysis

Inherited from plan. No skill match in 18 Hermes domains for slow-burn detection. Build from scratch.

## File-level diff

### 1. `scout/spikes/detector.py` (extend)

Add new function below existing `detect_7d_momentum`:

```python
async def detect_slow_burn_7d(
    db: "Database",
    raw_coins: list[dict],
    min_7d_change: float = 50.0,
    max_1h_change: float = 5.0,
    max_mcap: float = 500_000_000,
    min_volume_24h: float = 100_000,
    dedup_days: int = 7,
) -> list[dict]:
    """Find tokens with slow-burn shape: strong 7d gain + low 1h volatility.

    BL-075 Phase B — captures the RIV-shape blind spot (multi-day distributed
    accumulation, not concentrated spike).

    Filter:
      change_7d >= min_7d_change
      AND abs(change_1h) <= max_1h_change   # symmetric (R1 MUST-FIX)
      AND volume_24h >= min_volume_24h
      AND (market_cap == 0 OR market_cap <= max_mcap)  # mcap=0 ALLOWED

    Mcap=0 tolerance (Phase A blind-spot fix): CoinGecko returns null mcap
    for ~53% of scanned tokens. Existing detect_7d_momentum:219 silently
    rejects these. Slow-burn explicitly preserves them and emits structured
    `slow_burn_mcap_unknown` for observability. Validated by RIV back-check
    2026-05-10: RIV's mcap was NULL for 900+ of its first 947 CG data points;
    a mcap-tolerant detector would have caught RIV during that window.

    Cross-detector overlap flag: stores `also_in_momentum_7d` boolean per
    row by querying momentum_7d for the same coin within ±3 days. Enables
    clean D+14 evaluation of whether slow-burn surfaces tokens momentum_7d
    misses.

    No paper trade dispatch (research-only, like velocity_alerter).
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    today_utc = now_dt.strftime("%Y-%m-%d")
    results: list[dict] = []
    mcap_unknown_count = 0

    for coin in raw_coins:
        cid = coin.get("id")
        if not cid:
            continue
        change_7d = coin.get("price_change_percentage_7d_in_currency") or 0
        change_1h = coin.get("price_change_percentage_1h_in_currency") or 0
        mcap_raw = coin.get("market_cap")
        mcap = mcap_raw if mcap_raw and mcap_raw > 0 else None
        volume = coin.get("total_volume") or 0

        # 7d threshold
        if change_7d < min_7d_change:
            continue
        # 1h symmetric volatility gate
        if abs(change_1h) > max_1h_change:
            continue
        # Volume floor
        if volume < min_volume_24h:
            continue
        # Mcap upper bound — but mcap=None passes through (mcap-unknown cohort)
        if mcap is not None and mcap > max_mcap:
            continue

        if mcap is None:
            mcap_unknown_count += 1
            logger.info(
                "slow_burn_mcap_unknown",
                coin_id=cid,
                symbol=(coin.get("symbol") or "").upper(),
                change_7d=change_7d,
                change_1h=change_1h,
                volume_24h=volume,
            )

        # Pre-INSERT dedup: skip if seen for this coin within dedup_days.
        # No UNIQUE constraint on table — this query is the guard (R2 MUST-FIX).
        cursor = await db._conn.execute(
            "SELECT id FROM slow_burn_candidates "
            "WHERE coin_id = ? AND date(detected_at) >= date('now', ?)",
            (cid, f"-{dedup_days} days"),
        )
        if await cursor.fetchone():
            continue

        # Cross-detector overlap flag: query momentum_7d ±3 days.
        cursor = await db._conn.execute(
            "SELECT id FROM momentum_7d "
            "WHERE coin_id = ? AND date(detected_at) >= date('now', '-3 days')",
            (cid,),
        )
        also_in_momentum = 1 if await cursor.fetchone() else 0

        row_data = {
            "coin_id": cid,
            "symbol": (coin.get("symbol") or "").upper(),
            "name": coin.get("name") or "",
            "price_change_7d": change_7d,
            "price_change_1h": change_1h,
            "price_change_24h": coin.get("price_change_percentage_24h") or 0,
            "market_cap": mcap,  # may be None
            "current_price": coin.get("current_price"),
            "volume_24h": volume,
            "also_in_momentum_7d": also_in_momentum,
        }

        await db._conn.execute(
            """INSERT INTO slow_burn_candidates
               (coin_id, symbol, name, price_change_7d, price_change_1h,
                price_change_24h, market_cap, current_price, volume_24h,
                also_in_momentum_7d, detected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row_data["coin_id"],
                row_data["symbol"],
                row_data["name"],
                row_data["price_change_7d"],
                row_data["price_change_1h"],
                row_data["price_change_24h"],
                row_data["market_cap"],
                row_data["current_price"],
                row_data["volume_24h"],
                row_data["also_in_momentum_7d"],
                now,
            ),
        )

        results.append(row_data)

    if results:
        await db._conn.commit()
        logger.info(
            "slow_burn_detected",
            count=len(results),
            mcap_unknown=mcap_unknown_count,
            also_in_momentum_count=sum(r["also_in_momentum_7d"] for r in results),
        )

    return results
```

### 2. `scout/db.py` migration `_migrate_bl_slow_burn_v1`

```python
async def _migrate_bl_slow_burn_v1(self) -> None:
    """BL-075 Phase B: create slow_burn_candidates table.

    Schema mirrors momentum_7d + price_change_1h (the new gate's value)
    + also_in_momentum_7d (overlap flag for D+14 evaluation).

    market_cap is REAL nullable — slow-burn intentionally preserves the
    mcap-unknown cohort that detect_7d_momentum silently rejects (Phase A
    found 53.5% of CG-scanned tokens have null mcap). See Phase B plan.

    Migration `bl_slow_burn_v1`, schema_version 20260515.
    """
    import structlog
    _log = structlog.get_logger()
    if self._conn is None:
        raise RuntimeError("Database not initialized.")
    conn = self._conn
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        await conn.execute("BEGIN EXCLUSIVE")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version    INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL,
                description TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS slow_burn_candidates (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                coin_id         TEXT    NOT NULL,
                symbol          TEXT    NOT NULL,
                name            TEXT,
                price_change_7d REAL    NOT NULL,
                price_change_1h REAL    NOT NULL,
                price_change_24h REAL,
                market_cap      REAL,
                current_price   REAL,
                volume_24h      REAL,
                also_in_momentum_7d INTEGER NOT NULL DEFAULT 0,
                detected_at     TEXT    NOT NULL
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_slow_burn_detected "
            "ON slow_burn_candidates(detected_at)"
        )
        # R4 MUST-FIX: composite index for the dedup hot-path query
        # (coin_id = ? AND date(detected_at) >= ...). Mirrors
        # momentum_7d's (coin_id, detected_at) index at scout/db.py:668.
        # Without this, dedup scans all rows for each coin_id during
        # cycle dispatch — fine at session start but degrades over the
        # 14-day soak as the table grows.
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_slow_burn_coin_date "
            "ON slow_burn_candidates(coin_id, detected_at)"
        )
        await conn.execute(
            "INSERT OR IGNORE INTO schema_version "
            "(version, applied_at, description) VALUES (?, ?, ?)",
            (20260515, now_iso, "bl_slow_burn_v1_slow_burn_candidates"),
        )
        await conn.commit()
    except Exception as e:
        _log.exception(
            "schema_migration_failed",
            migration="bl_slow_burn_v1",
            err=str(e),
            err_type=type(e).__name__,
        )
        try:
            await conn.execute("ROLLBACK")
        except Exception as rb_err:
            _log.exception(
                "schema_migration_rollback_failed",
                migration="bl_slow_burn_v1",
                err=str(rb_err),
                err_type=type(rb_err).__name__,
            )
        _log.error("SCHEMA_DRIFT_DETECTED", migration="bl_slow_burn_v1")
        raise

    # Post-assertion — schema_version row + description match.
    cur = await conn.execute(
        "SELECT description FROM schema_version WHERE version = ?",
        (20260515,),
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("bl_slow_burn_v1 schema_version row missing after migration")
    if row[0] != "bl_slow_burn_v1_slow_burn_candidates":
        raise RuntimeError(
            f"bl_slow_burn_v1 schema_version description mismatch — "
            f"expected 'bl_slow_burn_v1_slow_burn_candidates', got {row[0]!r}"
        )
```

Wired into `_apply_migrations` chain after `_migrate_bl_quote_pair_v1`.

### 3. `scout/config.py` — add 6 Settings

Place near existing `MOMENTUM_7D_*` block (line 140):

```python
# -------- Slow-Burn Watcher (BL-075 Phase B) --------
# RIV-shape detector: strong 7d gain with low 1h volatility (slow accumulation,
# not concentrated pump). Captures the cohort detect_7d_momentum + velocity_alerter
# both miss. Research-only — no paper trade dispatch. Mcap=0 tolerant (Phase A
# blind-spot fix; see scout/heartbeat.py mcap_null_with_price_count).
SLOW_BURN_ENABLED: bool = True
SLOW_BURN_MIN_7D_CHANGE: float = 50.0
SLOW_BURN_MAX_1H_CHANGE: float = 5.0  # symmetric: abs(change_1h) <= this
SLOW_BURN_MAX_MCAP: float = 500_000_000
SLOW_BURN_MIN_VOLUME: float = 100_000
SLOW_BURN_DEDUP_DAYS: int = 7
```

### 4. `scout/main.py` — wire after `MOMENTUM_7D` block

Per plan §6.

### 5. `scout/heartbeat.py` — R4 MUST-FIX live observability counter

Mirror the Phase A `mcap_null_with_price_count` pattern. Without this, an
env-mismatch silent-disable (e.g., `SLOW_BURN_ENABLED` missing from `.env`)
goes undetected until D+3 SQL query.

```python
# Add to _heartbeat_stats:
"slow_burn_detected_today": 0,

# Add to _reset_heartbeat_stats:
slow_burn_detected_today=0,

# New increment fn:
def increment_slow_burn_detected(count: int = 1) -> None:
    """BL-075 Phase B: bump per-cycle slow-burn detection counter.

    Called from detect_slow_burn_7d on each results commit so the
    operator sees live detection volume in heartbeat output without
    waiting for the D+3 SQL check.
    """
    _heartbeat_stats["slow_burn_detected_today"] += count

# Add to _maybe_emit_heartbeat output:
slow_burn_detected_today=_heartbeat_stats["slow_burn_detected_today"],
```

Detector calls `increment_slow_burn_detected(len(results))` after the commit
in `detect_slow_burn_7d`. Reset per-cycle is operator-facing — the counter
is "today's detections, accumulating since last process restart" (matches
`mcap_null_with_price_count` semantics).

## Test plan (`tests/test_slow_burn_detector.py`)

10 cases minimum:

```python
"""Tests for scout/spikes/detector.py::detect_slow_burn_7d (BL-075 Phase B)."""

from __future__ import annotations
from datetime import datetime, timezone
import pytest

from scout.db import Database
from scout.spikes.detector import detect_slow_burn_7d


def _coin(**overrides) -> dict:
    """Minimum CG /coins/markets-shape dict for tests."""
    base = {
        "id": "test-coin",
        "symbol": "TEST",
        "name": "Test Coin",
        "price_change_percentage_7d_in_currency": 80.0,
        "price_change_percentage_1h_in_currency": 2.0,
        "price_change_percentage_24h": 12.0,
        "market_cap": 10_000_000,
        "current_price": 0.5,
        "total_volume": 200_000,
    }
    base.update(overrides)
    return base


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "t.db"))
    await database.initialize()
    yield database
    await database.close()


# ----- Happy path -----

async def test_slow_burn_fires_on_canonical_shape(db):
    """7d=80% + 1h=2% + mcap=$10M + vol=$200K → fires."""
    results = await detect_slow_burn_7d(db, [_coin()])
    assert len(results) == 1
    assert results[0]["coin_id"] == "test-coin"
    assert results[0]["price_change_7d"] == 80.0
    assert results[0]["also_in_momentum_7d"] == 0


# ----- 7d boundary -----

@pytest.mark.parametrize(
    "change_7d, should_fire",
    [(49.99, False), (50.0, True), (50.01, True)],
)
async def test_slow_burn_7d_threshold_boundary(db, change_7d, should_fire):
    coin = _coin(price_change_percentage_7d_in_currency=change_7d, id=f"test-{change_7d}")
    results = await detect_slow_burn_7d(db, [coin])
    assert (len(results) == 1) is should_fire


# ----- 1h SYMMETRIC boundary (R1 MUST-FIX) -----

@pytest.mark.parametrize(
    "change_1h, should_fire",
    [
        (4.99, True),    # +4.99% — within band, fires
        (5.0, True),     # exactly threshold — fires (abs <=)
        (5.01, False),   # just over up — does not fire
        (-4.99, True),   # -4.99% — symmetric, fires
        (-5.0, True),    # -5.0% — exactly threshold — fires
        (-5.01, False),  # -5.01% — symmetric over, does not fire
        (-8.0, False),   # R1 example: -8% retrace, NOT a slow burn
    ],
)
async def test_slow_burn_1h_symmetric_boundary(db, change_1h, should_fire):
    coin = _coin(
        price_change_percentage_1h_in_currency=change_1h,
        id=f"test-1h-{change_1h}",
    )
    results = await detect_slow_burn_7d(db, [coin])
    assert (len(results) == 1) is should_fire, (
        f"change_1h={change_1h} expected fire={should_fire}; got {len(results)} results"
    )


# ----- Velocity-shape rejection (NOT slow burn) -----

async def test_velocity_shape_does_not_fire(db):
    """7d=80% + 1h=15% → concentrated pump, not slow burn → no fire."""
    coin = _coin(price_change_percentage_1h_in_currency=15.0, id="velocity")
    results = await detect_slow_burn_7d(db, [coin])
    assert len(results) == 0


# ----- Volume floor (R3 MUST-FIX boundary parametrize) -----

@pytest.mark.parametrize(
    "volume_24h, should_fire",
    [
        (99_999, False),   # one dollar below floor — does not fire
        (100_000, True),   # exactly at floor — fires (>= guard)
        (100_001, True),   # one dollar above — fires
    ],
)
async def test_slow_burn_volume_boundary(db, volume_24h, should_fire):
    coin = _coin(total_volume=volume_24h, id=f"vol-{volume_24h}")
    results = await detect_slow_burn_7d(db, [coin])
    assert (len(results) == 1) is should_fire


# ----- Mega-cap floor -----

async def test_mega_cap_does_not_fire(db):
    coin = _coin(market_cap=1_000_000_000, id="megacap")
    results = await detect_slow_burn_7d(db, [coin])
    assert len(results) == 0


# ----- Mcap-unknown cohort fires (Phase A blind-spot fix) -----

async def test_mcap_unknown_fires_with_null_market_cap(db):
    """RIV-style: CG returns null mcap. Detector must fire + log structured.

    R3 CRITICAL: do NOT use caplog — caplog is vacuous for structlog events
    (only catches stdlib logging.* records). Use structlog.testing.capture_logs
    to assert the 'slow_burn_mcap_unknown' event was emitted.
    """
    import structlog
    coin = _coin(market_cap=None, id="riv-style")
    with structlog.testing.capture_logs() as captured:
        results = await detect_slow_burn_7d(db, [coin])
    assert len(results) == 1
    assert results[0]["market_cap"] is None
    # Lock the structured log emission (without it, observability is dead).
    events = [e["event"] for e in captured]
    assert "slow_burn_mcap_unknown" in events, (
        f"slow_burn_mcap_unknown event missing; captured events: {events}"
    )


async def test_mcap_zero_fires_with_null_market_cap(db):
    """CG sometimes returns 0 instead of null — same blind-spot cohort."""
    coin = _coin(market_cap=0, id="zero-mcap")
    results = await detect_slow_burn_7d(db, [coin])
    assert len(results) == 1
    assert results[0]["market_cap"] is None  # normalized to None in row


# ----- Dedup -----

async def test_dedup_within_window_skips_second_detection(db):
    """Same coin twice within dedup_days → only first fires."""
    coin = _coin(id="dedup-test")
    results1 = await detect_slow_burn_7d(db, [coin])
    results2 = await detect_slow_burn_7d(db, [coin])
    assert len(results1) == 1
    assert len(results2) == 0


# ----- Cross-detector overlap flag (R1 MUST-FIX) -----

async def test_also_in_momentum_7d_flag_set_when_overlap(db):
    """Coin already in momentum_7d → also_in_momentum_7d=1 in slow_burn row."""
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO momentum_7d
           (coin_id, symbol, name, price_change_7d, price_change_24h,
            market_cap, current_price, volume_24h, detected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("overlap", "TEST", "Test", 120.0, 30.0, 5_000_000, 0.5, 300_000, now),
    )
    await db._conn.commit()
    coin = _coin(id="overlap")
    results = await detect_slow_burn_7d(db, [coin])
    assert len(results) == 1
    assert results[0]["also_in_momentum_7d"] == 1


async def test_also_in_momentum_7d_flag_zero_when_no_overlap(db):
    """R3 MUST-FIX: explicit negative case — coin NOT in momentum_7d → flag=0.

    Without this test, a bug where the overlap query always returns a row
    would fall through (default value also happens to be 0, so naive testing
    misses both the over-fire AND the never-set bug).
    """
    # Pre-seed momentum_7d with a DIFFERENT coin to confirm query specificity.
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO momentum_7d
           (coin_id, symbol, name, price_change_7d, price_change_24h,
            market_cap, current_price, volume_24h, detected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("other-coin", "OTHER", "Other", 200.0, 50.0, 10_000_000, 1.0, 500_000, now),
    )
    await db._conn.commit()
    coin = _coin(id="no-overlap")  # different coin_id from seeded row
    results = await detect_slow_burn_7d(db, [coin])
    assert len(results) == 1
    assert results[0]["also_in_momentum_7d"] == 0


# ----- Migration tests in tests/test_db_migration_bl_slow_burn.py -----
# R3 MUST-FIX: parity with BL-NEW-QUOTE-PAIR's 5 migration tests
# (orphan-detection, schema_version row, idempotent rerun, description-mismatch,
# composite index existence).

async def test_bl_slow_burn_v1_columns_added(tmp_path):
    """Table + columns exist post-initialize."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(slow_burn_candidates)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "coin_id" in cols
    assert "price_change_1h" in cols
    assert "also_in_momentum_7d" in cols
    assert "market_cap" in cols  # nullable
    await db.close()


async def test_bl_slow_burn_v1_wired_into_apply_migrations(tmp_path):
    """R3: schema_version row written = migration is in _apply_migrations chain.

    Without this test, an orphaned migration (defined but not wired) would
    silently succeed test_bl_slow_burn_v1_columns_added — `_create_tables`
    or another migration could create slow_burn_candidates incidentally.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT description FROM schema_version WHERE version=20260515"
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "bl_slow_burn_v1_slow_burn_candidates"
    await db.close()


async def test_bl_slow_burn_v1_idempotent_rerun(tmp_path):
    """R3 MUST-FIX: every restart re-runs all migrations; must not raise."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._migrate_bl_slow_burn_v1()  # second call — must not raise
    cur = await db._conn.execute("PRAGMA table_info(slow_burn_candidates)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "also_in_momentum_7d" in cols
    await db.close()


async def test_bl_slow_burn_v1_description_mismatch_raises(tmp_path):
    """R3 MUST-FIX: post-assertion catches version-collision case where
    INSERT OR IGNORE silently skipped a pre-seeded row with wrong description.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Tamper: replace description.
    await db._conn.execute(
        "UPDATE schema_version SET description = ? WHERE version = ?",
        ("some_other_migration_v999", 20260515),
    )
    await db._conn.commit()
    with pytest.raises(RuntimeError, match="description mismatch"):
        await db._migrate_bl_slow_burn_v1()
    await db.close()


async def test_bl_slow_burn_v1_composite_index_exists(tmp_path):
    """R4 MUST-FIX regression-lock: composite index for dedup hot-path."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name='idx_slow_burn_coin_date'"
    )
    assert (await cur.fetchone()) is not None, (
        "composite (coin_id, detected_at) index missing — "
        "dedup query will scan full coin partition"
    )
    await db.close()


async def test_slow_burn_heartbeat_counter_present(tmp_path):
    """R4 MUST-FIX: live observability counter must be in heartbeat stats."""
    from scout.heartbeat import _heartbeat_stats
    assert "slow_burn_detected_today" in _heartbeat_stats, (
        "heartbeat counter slow_burn_detected_today missing — "
        "operator has no live signal until D+3 SQL query"
    )
```

## Runbook (`docs/runbook_slow_burn_phase_b.md`)

Brief — install is automatic via deploy. Operator runs verification queries at D+3, D+7, D+14:

```sql
-- D+3 sanity check: any detections at all?
SELECT COUNT(*), COUNT(DISTINCT coin_id) FROM slow_burn_candidates;

-- D+14 volume gate (must be ≥35 unique coins)
SELECT COUNT(DISTINCT coin_id) FROM slow_burn_candidates;

-- D+14 mcap-unknown cohort split
SELECT
  SUM(CASE WHEN market_cap IS NOT NULL AND market_cap > 0 THEN 1 ELSE 0 END) AS mcap_known,
  SUM(CASE WHEN market_cap IS NULL OR market_cap = 0 THEN 1 ELSE 0 END) AS mcap_unknown,
  COUNT(*) AS total
FROM slow_burn_candidates;

-- D+14 separability gate (must be <70% overlap with momentum_7d)
SELECT
  ROUND(100.0 * SUM(also_in_momentum_7d) / COUNT(*), 1) AS overlap_pct
FROM slow_burn_candidates;

-- D+14 quality gate (manual): for first 35 detections, spot-check CG
-- price_change_30d_in_currency for each. Count ≥2x runners.
SELECT coin_id, symbol, detected_at, market_cap, current_price
FROM slow_burn_candidates
ORDER BY detected_at ASC
LIMIT 35;
```

Revert: `SLOW_BURN_ENABLED=False` env override on prod (.env). Migration is forward-only; table stays untouched.

## Reviewer dispatch — design stage (2 parallel)

- **R3 (test rigor):** Are 10+ tests sufficient? Specifically: is the mcap-unknown caplog-vs-structlog interaction tested correctly (caplog vacuous on structlog warning per `feedback_caplog_vacuous_on_structlog.md`-style pattern)? Are migration tests (`test_db_migration_bl_slow_burn.py`) following the orphan-detection / schema_version-content / preserve-rows pattern from BL-NEW-QUOTE-PAIR's test_db_migration_bl_quote_pair.py? Cross-detector overlap test exercises only the positive case — does it need a negative (`also_in_momentum_7d=0` when no overlap)?
- **R4 (operational):** Is the migration safe on the running prod pipeline (BEGIN EXCLUSIVE during a polling cycle that reads from candidates table)? Does the dedup query (`date(detected_at) >= date('now', '-7 days')`) work correctly across UTC midnight boundaries? Should the runbook include an explicit "verify Phase A counter is still incrementing" step (smoke check that mcap-tolerance is still relevant)?
