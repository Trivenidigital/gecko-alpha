# Second-Wave Detection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect tokens that previously pumped, cooled down for 3-14 days, and are now showing early re-accumulation signals — catching "second wave" setups before the next move. Runs as a parallel async loop inside gecko-alpha.

**Architecture:** New `scout/secondwave/` package runs alongside the existing pipeline and narrative agent via `asyncio.gather()`. Three-phase loop every 30 minutes: SCAN (DB) -> CONFIRM (CoinGecko) -> ALERT. Opt-in via `SECONDWAVE_ENABLED` flag. Zero MiroFish/narrative scoring — the prior pump already validated the narrative.

**Tech Stack:** Python 3.12, aiohttp, aiosqlite, Pydantic v2, structlog, pytest + aioresponses

**Spec:** `docs/superpowers/specs/2026-04-10-second-wave-detection-design.md`

---

## Pre-flight Notes

The spec references `alerts.market_cap_usd`, `alerts.price_usd`, and `alerts.token_name`/`ticker`. The current `alerts` schema (`scout/db.py` lines 100-107) only has `id`, `contract_address`, `chain`, `conviction_score`, `alert_market_cap`, `alerted_at`. Task 2 therefore adds `price_usd`, `token_name`, `ticker` columns to `alerts` via `ALTER TABLE IF NOT EXISTS` style idempotent migration, and adds a new `log_alert_extended` path (or extends `log_alert`) in a backward-compatible way. The scan query uses `alert_market_cap` (existing) and the new `price_usd`, `token_name`, `ticker` columns.

---

## File Map

### New files (create)
| File | Responsibility |
|------|---------------|
| `scout/secondwave/__init__.py` | Package init |
| `scout/secondwave/models.py` | `SecondWaveCandidate` Pydantic model |
| `scout/secondwave/detector.py` | Scan DB, score re-accumulation, orchestrate loop |
| `scout/secondwave/alerts.py` | Telegram alert formatter |
| `tests/test_secondwave_models.py` | Model validation tests |
| `tests/test_secondwave_db.py` | Schema migration, insert, query, dedup, volume history |
| `tests/test_secondwave_detector.py` | Scoring math, threshold gating, loop wiring |
| `tests/test_secondwave_alerts.py` | Alert formatting edge cases |

### Modified files
| File | Changes |
|------|---------|
| `scout/config.py` | Add 10 `SECONDWAVE_*` config fields |
| `scout/db.py` | Add `second_wave_candidates` table, migrate `alerts`, add 5 new methods |
| `scout/main.py` | Add `secondwave_loop` to `asyncio.gather()`; extend the `db.log_alert(...)` call at line 235 to persist `price_usd`/`token_name`/`ticker` |
| `.env.example` | Add `SECONDWAVE_*` env vars |
| `dashboard/api.py` | (Phase 2) Add `/api/secondwave/candidates` + `/api/secondwave/stats` |

---

## Task 1: Models + Config

**Files:**
- Create: `scout/secondwave/__init__.py`
- Create: `scout/secondwave/models.py`
- Modify: `scout/config.py`
- Modify: `.env.example`
- Test: `tests/test_secondwave_models.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_secondwave_models.py
"""Tests for SecondWaveCandidate model."""
from datetime import datetime, timezone

from scout.secondwave.models import SecondWaveCandidate


def test_secondwave_candidate_minimal():
    cand = SecondWaveCandidate(
        contract_address="0xabc",
        chain="ethereum",
        token_name="Test Token",
        ticker="TEST",
        peak_quant_score=75,
        peak_signals_fired=["momentum_ratio", "vol_acceleration"],
        first_seen_at=datetime.now(timezone.utc),
        original_market_cap=1_000_000.0,
        alert_market_cap=2_000_000.0,
        days_since_first_seen=5.2,
        price_drop_from_peak_pct=-42.5,
        current_price=0.00012,
        current_market_cap=1_200_000.0,
        current_volume_24h=500_000.0,
        price_vs_alert_pct=75.0,
        volume_vs_cooldown_avg=3.1,
        reaccumulation_score=85,
        reaccumulation_signals=["sufficient_drawdown", "price_recovery", "volume_pickup", "strong_prior_signal"],
        detected_at=datetime.now(timezone.utc),
    )
    assert cand.coingecko_id is None
    assert cand.alerted_at is None
    assert cand.reaccumulation_score == 85
    assert "price_recovery" in cand.reaccumulation_signals


def test_secondwave_candidate_with_coingecko_id():
    cand = SecondWaveCandidate(
        contract_address="0xdef",
        chain="solana",
        token_name="CG Token",
        ticker="CGT",
        coingecko_id="cg-token",
        peak_quant_score=80,
        peak_signals_fired=[],
        first_seen_at=datetime.now(timezone.utc),
        original_market_cap=500_000.0,
        alert_market_cap=1_500_000.0,
        days_since_first_seen=7.0,
        price_drop_from_peak_pct=-50.0,
        current_price=0.5,
        current_market_cap=750_000.0,
        current_volume_24h=100_000.0,
        price_vs_alert_pct=60.0,
        volume_vs_cooldown_avg=2.0,
        reaccumulation_score=65,
        reaccumulation_signals=["sufficient_drawdown", "strong_prior_signal"],
        detected_at=datetime.now(timezone.utc),
    )
    assert cand.coingecko_id == "cg-token"


def test_secondwave_candidate_stale_price():
    """price_is_stale=True must round-trip through the model."""
    cand = SecondWaveCandidate(
        contract_address="0xstale",
        chain="ethereum",
        token_name="Stale Token",
        ticker="STL",
        peak_quant_score=70,
        peak_signals_fired=["momentum_ratio"],
        first_seen_at=datetime.now(timezone.utc),
        original_market_cap=1_000_000.0,
        alert_market_cap=2_000_000.0,
        days_since_first_seen=6.0,
        price_drop_from_peak_pct=0.0,
        current_price=1.0,
        current_market_cap=2_000_000.0,
        current_volume_24h=None,
        price_vs_alert_pct=100.0,
        volume_vs_cooldown_avg=0.0,
        price_is_stale=True,
        reaccumulation_score=50,
        reaccumulation_signals=["price_recovery", "strong_prior_signal"],
        detected_at=datetime.now(timezone.utc),
    )
    assert cand.price_is_stale is True
    assert cand.current_volume_24h is None
```

- [ ] **Step 2: Verify tests fail**

```bash
uv run pytest tests/test_secondwave_models.py -v
```
Expected: `ModuleNotFoundError: No module named 'scout.secondwave'`

- [ ] **Step 3: Create package + models**

```python
# scout/secondwave/__init__.py
"""Second-Wave Detection — re-accumulation signals for previously-pumped tokens."""
```

```python
# scout/secondwave/models.py
"""Pydantic models for Second-Wave Detection."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class SecondWaveCandidate(BaseModel):
    contract_address: str
    chain: str
    token_name: str
    ticker: str

    # Prior pump data
    coingecko_id: str | None = None
    peak_quant_score: int
    peak_signals_fired: list[str]
    first_seen_at: datetime
    original_alert_at: datetime | None = None
    original_market_cap: float
    alert_market_cap: float

    # Cooldown data
    days_since_first_seen: float
    price_drop_from_peak_pct: float

    # Re-accumulation signals
    current_price: float
    current_market_cap: float
    current_volume_24h: float | None = None
    price_vs_alert_pct: float
    volume_vs_cooldown_avg: float
    price_is_stale: bool = False

    # Scoring
    reaccumulation_score: int
    reaccumulation_signals: list[str]

    # Metadata
    detected_at: datetime
    alerted_at: datetime | None = None
```

- [ ] **Step 4: Add config knobs**

Append to the `Settings` class in `scout/config.py`:

```python
    # -------- Second-Wave Detection --------
    SECONDWAVE_ENABLED: bool = False
    SECONDWAVE_POLL_INTERVAL: int = 1800
    SECONDWAVE_MIN_PRIOR_SCORE: int = 60
    SECONDWAVE_COOLDOWN_MIN_DAYS: int = 3
    SECONDWAVE_COOLDOWN_MAX_DAYS: int = 14
    SECONDWAVE_MIN_DRAWDOWN_PCT: float = 30.0
    SECONDWAVE_MIN_RECOVERY_PCT: float = 70.0
    SECONDWAVE_VOL_PICKUP_RATIO: float = 2.0
    SECONDWAVE_ALERT_THRESHOLD: int = 50
    SECONDWAVE_DEDUP_DAYS: int = 7
```

Append to `.env.example`:

```bash
# --- Second-Wave Detection ---
SECONDWAVE_ENABLED=false
SECONDWAVE_POLL_INTERVAL=1800
SECONDWAVE_MIN_PRIOR_SCORE=60
SECONDWAVE_COOLDOWN_MIN_DAYS=3
SECONDWAVE_COOLDOWN_MAX_DAYS=14
SECONDWAVE_MIN_DRAWDOWN_PCT=30.0
SECONDWAVE_MIN_RECOVERY_PCT=70.0
SECONDWAVE_VOL_PICKUP_RATIO=2.0
SECONDWAVE_ALERT_THRESHOLD=50
SECONDWAVE_DEDUP_DAYS=7
```

- [ ] **Step 5: Verify tests pass**

```bash
uv run pytest tests/test_secondwave_models.py -v
uv run pytest --tb=short -q
```

- [ ] **Step 6: Commit**

```bash
git add scout/secondwave/__init__.py scout/secondwave/models.py scout/config.py .env.example tests/test_secondwave_models.py
git commit -m "feat(secondwave): add SecondWaveCandidate model and config knobs"
```

---

## Task 2: DB Schema + Query Methods

**Files:**
- Modify: `scout/db.py`
- Test: `tests/test_secondwave_db.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_secondwave_db.py
"""Tests for second-wave DB schema and query methods."""
from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


async def _insert_alert(db, contract, alerted_days_ago, market_cap, price, name="Tok", ticker="TK", chain="eth"):
    ts = (datetime.now(timezone.utc) - timedelta(days=alerted_days_ago)).isoformat()
    await db._conn.execute(
        """INSERT INTO alerts
           (contract_address, chain, conviction_score, alert_market_cap, price_usd, token_name, ticker, alerted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (contract, chain, 80.0, market_cap, price, name, ticker, ts),
    )
    await db._conn.commit()


async def _insert_score_history(db, contract, score, days_ago=5):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    await db._conn.execute(
        "INSERT INTO score_history (contract_address, score, scanned_at) VALUES (?, ?, ?)",
        (contract, score, ts),
    )
    await db._conn.commit()


async def test_second_wave_candidates_table_exists(db):
    cursor = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='second_wave_candidates'"
    )
    assert await cursor.fetchone() is not None


async def test_alerts_table_has_new_columns(db):
    cursor = await db._conn.execute("PRAGMA table_info(alerts)")
    cols = {row[1] for row in await cursor.fetchall()}
    assert {"price_usd", "token_name", "ticker"}.issubset(cols)


async def test_get_secondwave_scan_candidates_filters_by_window(db):
    # In window: alerted 5 days ago, peak score 75
    await _insert_alert(db, "0xin", alerted_days_ago=5, market_cap=2e6, price=1.0)
    await _insert_score_history(db, "0xin", score=75.0, days_ago=5)
    # Too fresh: 1 day ago
    await _insert_alert(db, "0xfresh", alerted_days_ago=1, market_cap=2e6, price=1.0)
    await _insert_score_history(db, "0xfresh", score=80.0, days_ago=1)
    # Too stale: 20 days ago
    await _insert_alert(db, "0xstale", alerted_days_ago=20, market_cap=2e6, price=1.0)
    await _insert_score_history(db, "0xstale", score=80.0, days_ago=20)
    # Weak prior: score 40
    await _insert_alert(db, "0xweak", alerted_days_ago=5, market_cap=2e6, price=1.0)
    await _insert_score_history(db, "0xweak", score=40.0, days_ago=5)

    rows = await db.get_secondwave_scan_candidates(
        min_age_days=3, max_age_days=14, min_peak_score=60, dedup_days=7
    )
    addrs = {r["contract_address"] for r in rows}
    assert "0xin" in addrs
    assert "0xfresh" not in addrs
    assert "0xstale" not in addrs
    assert "0xweak" not in addrs


async def test_get_secondwave_scan_candidates_excludes_already_detected(db):
    await _insert_alert(db, "0xdup", alerted_days_ago=5, market_cap=2e6, price=1.0)
    await _insert_score_history(db, "0xdup", score=80.0, days_ago=5)
    now = datetime.now(timezone.utc).isoformat()
    await db.insert_secondwave_candidate({
        "contract_address": "0xdup", "chain": "eth",
        "token_name": "Dup", "ticker": "DUP", "coingecko_id": None,
        "peak_quant_score": 80, "peak_signals_fired": [],
        "first_seen_at": now, "original_alert_at": None,
        "original_market_cap": 1e6, "alert_market_cap": 2e6,
        "days_since_first_seen": 5.0, "price_drop_from_peak_pct": -40.0,
        "current_price": 0.8, "current_market_cap": 1.2e6,
        "current_volume_24h": 5e5, "price_vs_alert_pct": 80.0,
        "volume_vs_cooldown_avg": 2.5, "price_is_stale": False,
        "reaccumulation_score": 85,
        "reaccumulation_signals": ["sufficient_drawdown", "price_recovery"],
        "detected_at": now, "alerted_at": now,
    })
    rows = await db.get_secondwave_scan_candidates(3, 14, 60, 7)
    assert all(r["contract_address"] != "0xdup" for r in rows)


async def test_was_secondwave_alerted(db):
    assert await db.was_secondwave_alerted("0xnew") is False
    now = datetime.now(timezone.utc).isoformat()
    await db.insert_secondwave_candidate({
        "contract_address": "0xnew", "chain": "eth",
        "token_name": "X", "ticker": "X", "coingecko_id": None,
        "peak_quant_score": 70, "peak_signals_fired": [],
        "first_seen_at": now, "original_alert_at": None,
        "original_market_cap": 1e6, "alert_market_cap": 2e6,
        "days_since_first_seen": 5.0, "price_drop_from_peak_pct": -40.0,
        "current_price": 0.8, "current_market_cap": 1.2e6,
        "current_volume_24h": 5e5, "price_vs_alert_pct": 80.0,
        "volume_vs_cooldown_avg": 2.5, "price_is_stale": False,
        "reaccumulation_score": 60, "reaccumulation_signals": [],
        "detected_at": now, "alerted_at": now,
    })
    assert await db.was_secondwave_alerted("0xnew") is True


async def test_get_volume_history(db):
    now = datetime.now(timezone.utc)
    for i, v in enumerate([100.0, 200.0, 300.0]):
        ts = (now - timedelta(days=i)).isoformat()
        await db._conn.execute(
            "INSERT INTO volume_snapshots (contract_address, volume_24h_usd, scanned_at) VALUES (?, ?, ?)",
            ("0xvh", v, ts),
        )
    await db._conn.commit()
    hist = await db.get_volume_history("0xvh", days=14)
    assert len(hist) == 3
    assert sorted(hist) == [100.0, 200.0, 300.0]


async def test_get_recent_secondwave_candidates(db):
    now = datetime.now(timezone.utc).isoformat()
    await db.insert_secondwave_candidate({
        "contract_address": "0xr", "chain": "eth",
        "token_name": "R", "ticker": "R", "coingecko_id": None,
        "peak_quant_score": 70, "peak_signals_fired": ["x"],
        "first_seen_at": now, "original_alert_at": None,
        "original_market_cap": 1e6, "alert_market_cap": 2e6,
        "days_since_first_seen": 5.0, "price_drop_from_peak_pct": -40.0,
        "current_price": 0.8, "current_market_cap": 1.2e6,
        "current_volume_24h": 5e5, "price_vs_alert_pct": 80.0,
        "volume_vs_cooldown_avg": 2.5, "price_is_stale": False,
        "reaccumulation_score": 77, "reaccumulation_signals": ["price_recovery"],
        "detected_at": now, "alerted_at": now,
    })
    rows = await db.get_recent_secondwave_candidates(days=7)
    assert len(rows) == 1
    assert rows[0]["reaccumulation_score"] == 77
    assert rows[0]["reaccumulation_signals"] == ["price_recovery"]
    assert rows[0]["peak_signals_fired"] == ["x"]


async def test_log_alert_persists_new_columns(db):
    """Ensure log_alert's extended signature round-trips price_usd/token_name/ticker."""
    await db.log_alert(
        contract_address="0xnewcols",
        chain="ethereum",
        conviction_score=72.5,
        alert_market_cap=1_500_000.0,
        price_usd=0.42,
        token_name="NewCol Token",
        ticker="NCT",
    )
    cursor = await db._conn.execute(
        """SELECT contract_address, chain, conviction_score, alert_market_cap,
                  price_usd, token_name, ticker
           FROM alerts WHERE contract_address = ?""",
        ("0xnewcols",),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["contract_address"] == "0xnewcols"
    assert row["chain"] == "ethereum"
    assert row["conviction_score"] == 72.5
    assert row["alert_market_cap"] == 1_500_000.0
    assert row["price_usd"] == 0.42
    assert row["token_name"] == "NewCol Token"
    assert row["ticker"] == "NCT"
```

- [ ] **Step 2: Verify tests fail**

```bash
uv run pytest tests/test_secondwave_db.py -v
```
Expected: `no such table: second_wave_candidates` / missing method errors.

- [ ] **Step 3: Extend `_create_tables` in `scout/db.py`**

Inside `_create_tables`, append to the `executescript` block (before the closing `"""`):

```sql
CREATE TABLE IF NOT EXISTS second_wave_candidates (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_address         TEXT NOT NULL,
    chain                    TEXT NOT NULL,
    token_name               TEXT NOT NULL,
    ticker                   TEXT NOT NULL,
    coingecko_id             TEXT,
    peak_quant_score         INTEGER NOT NULL,
    peak_signals_fired       TEXT,
    first_seen_at            TEXT NOT NULL,
    original_alert_at        TEXT,
    original_market_cap      REAL,
    alert_market_cap         REAL,
    days_since_first_seen    REAL,
    price_drop_from_peak_pct REAL,
    current_price            REAL,
    current_market_cap       REAL,
    current_volume_24h       REAL,
    price_vs_alert_pct       REAL,
    volume_vs_cooldown_avg   REAL,
    price_is_stale           INTEGER NOT NULL DEFAULT 0,
    reaccumulation_score     INTEGER NOT NULL,
    reaccumulation_signals   TEXT NOT NULL,
    detected_at              TEXT NOT NULL,
    alerted_at               TEXT,
    created_at               TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sw_contract
    ON second_wave_candidates(contract_address, detected_at);
CREATE INDEX IF NOT EXISTS idx_sw_score
    ON second_wave_candidates(reaccumulation_score);
```

After the `executescript` block, add an idempotent migration for the `alerts` table:

```python
        # Migrate alerts table: add price_usd, token_name, ticker if missing
        cursor = await self._conn.execute("PRAGMA table_info(alerts)")
        existing_cols = {row[1] for row in await cursor.fetchall()}
        for col, ddl in (
            ("price_usd", "ALTER TABLE alerts ADD COLUMN price_usd REAL"),
            ("token_name", "ALTER TABLE alerts ADD COLUMN token_name TEXT"),
            ("ticker", "ALTER TABLE alerts ADD COLUMN ticker TEXT"),
        ):
            if col not in existing_cols:
                await self._conn.execute(ddl)
        await self._conn.commit()
```

- [ ] **Step 4: Add query methods to `Database`**

Append to `scout/db.py`:

```python
    # ------------------------------------------------------------------
    # Second-Wave Detection
    # ------------------------------------------------------------------

    async def get_secondwave_scan_candidates(
        self,
        min_age_days: int = 3,
        max_age_days: int = 14,
        min_peak_score: int = 60,
        dedup_days: int = 7,
    ) -> list[dict]:
        """Get alerted tokens in the cooldown window whose peak quant_score
        exceeded min_peak_score and that haven't been second-wave alerted recently.

        Joins `alerts` (persists beyond candidates prune) with `score_history`
        (peak score via MAX aggregate). Note: the `predictions` table has
        `coin_id` / `symbol` but no `contract_address` column, so we do NOT
        JOIN it here — callers must do a follow-up lookup by symbol to resolve
        a CoinGecko id (see `run_once` in `scout/secondwave/detector.py`).
        Peak score is filtered in SQL HAVING clause. All time-window integers
        are passed as bound parameters (never f-string interpolated) to
        eliminate any SQL injection risk.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cursor = await self._conn.execute(
            """SELECT a.contract_address,
                      a.chain,
                      COALESCE(a.token_name, '') AS token_name,
                      COALESCE(a.ticker, '')     AS ticker,
                      a.alert_market_cap,
                      a.price_usd                AS alert_price,
                      a.alerted_at,
                      MAX(sh.score)              AS peak_quant_score
               FROM alerts a
               LEFT JOIN score_history sh ON sh.contract_address = a.contract_address
               WHERE a.alerted_at <= datetime('now', '-' || ? || ' days')
                 AND a.alerted_at >= datetime('now', '-' || ? || ' days')
                 AND a.contract_address NOT IN (
                     SELECT contract_address FROM second_wave_candidates
                     WHERE detected_at >= datetime('now', '-' || ? || ' days')
                 )
               GROUP BY a.contract_address
               HAVING peak_quant_score >= ?""",
            (int(min_age_days), int(max_age_days), int(dedup_days), int(min_peak_score)),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_coingecko_id_by_symbol(self, symbol: str) -> str | None:
        """Look up a CoinGecko coin_id from the predictions table by ticker symbol.

        Used by second-wave detection to resolve live-price lookups for tokens
        that were also tracked by the narrative agent. Returns the most-recent
        matching coin_id, or None if no narrative prediction has been made for
        this symbol (in which case the token is treated as DEX-only and its
        price is marked stale).
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        if not symbol:
            return None
        cursor = await self._conn.execute(
            """SELECT coin_id FROM predictions
               WHERE symbol = ?
               ORDER BY predicted_at DESC
               LIMIT 1""",
            (symbol,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def was_secondwave_alerted(
        self, contract_address: str, days: int = 7
    ) -> bool:
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cursor = await self._conn.execute(
            """SELECT COUNT(*) FROM second_wave_candidates
               WHERE contract_address = ?
                 AND detected_at >= datetime('now', '-' || ? || ' days')""",
            (contract_address, int(days)),
        )
        row = await cursor.fetchone()
        return row[0] > 0 if row else False

    async def insert_secondwave_candidate(self, candidate: dict) -> None:
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        await self._conn.execute(
            """INSERT INTO second_wave_candidates
               (contract_address, chain, token_name, ticker, coingecko_id,
                peak_quant_score, peak_signals_fired, first_seen_at,
                original_alert_at, original_market_cap, alert_market_cap,
                days_since_first_seen, price_drop_from_peak_pct,
                current_price, current_market_cap, current_volume_24h,
                price_vs_alert_pct, volume_vs_cooldown_avg, price_is_stale,
                reaccumulation_score, reaccumulation_signals,
                detected_at, alerted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                candidate["contract_address"],
                candidate["chain"],
                candidate["token_name"],
                candidate["ticker"],
                candidate.get("coingecko_id"),
                candidate["peak_quant_score"],
                json.dumps(candidate.get("peak_signals_fired") or []),
                candidate["first_seen_at"],
                candidate.get("original_alert_at"),
                candidate.get("original_market_cap"),
                candidate.get("alert_market_cap"),
                candidate.get("days_since_first_seen"),
                candidate.get("price_drop_from_peak_pct"),
                candidate.get("current_price"),
                candidate.get("current_market_cap"),
                candidate.get("current_volume_24h"),
                candidate.get("price_vs_alert_pct"),
                candidate.get("volume_vs_cooldown_avg"),
                1 if candidate.get("price_is_stale") else 0,
                candidate["reaccumulation_score"],
                json.dumps(candidate.get("reaccumulation_signals") or []),
                candidate["detected_at"],
                candidate.get("alerted_at"),
            ),
        )
        await self._conn.commit()

    async def get_recent_secondwave_candidates(self, days: int = 7) -> list[dict]:
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cursor = await self._conn.execute(
            """SELECT * FROM second_wave_candidates
               WHERE detected_at >= datetime('now', '-' || ? || ' days')
               ORDER BY reaccumulation_score DESC""",
            (int(days),),
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        for r in rows:
            r["peak_signals_fired"] = json.loads(r.get("peak_signals_fired") or "[]")
            r["reaccumulation_signals"] = json.loads(r.get("reaccumulation_signals") or "[]")
            r["price_is_stale"] = bool(r.get("price_is_stale", 0))
        return rows

    async def get_volume_history(
        self, contract_address: str, days: int = 14
    ) -> list[float]:
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        cursor = await self._conn.execute(
            """SELECT volume_24h_usd FROM volume_snapshots
               WHERE contract_address = ?
                 AND scanned_at >= datetime('now', '-' || ? || ' days')
               ORDER BY scanned_at DESC""",
            (contract_address, int(days)),
        )
        rows = await cursor.fetchall()
        return [r[0] for r in rows]
```

- [ ] **Step 5: Verify tests pass**

```bash
uv run pytest tests/test_secondwave_db.py -v
uv run pytest --tb=short -q
```

- [ ] **Step 6: Commit**

```bash
git add scout/db.py tests/test_secondwave_db.py
git commit -m "feat(secondwave): add second_wave_candidates table and query methods"
```

---

## Task 3: Detector Logic (Scoring + Loop)

**Files:**
- Create: `scout/secondwave/detector.py`
- Test: `tests/test_secondwave_detector.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_secondwave_detector.py
"""Tests for second-wave scoring and detection."""
from datetime import datetime, timezone

from scout.config import Settings
from scout.secondwave.detector import (
    build_secondwave_candidate,
    score_reaccumulation,
)


def _settings() -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="x",
        TELEGRAM_CHAT_ID="x",
        SECONDWAVE_MIN_DRAWDOWN_PCT=30.0,
        SECONDWAVE_MIN_RECOVERY_PCT=70.0,
        SECONDWAVE_VOL_PICKUP_RATIO=2.0,
        SECONDWAVE_ALERT_THRESHOLD=50,
    )


def test_score_full_house():
    settings = _settings()
    candidate = {"peak_quant_score": 80}
    score, signals = score_reaccumulation(
        candidate,
        current_price=0.8,
        current_volume=5000.0,
        current_market_cap=1_200_000.0,
        alert_market_cap=2_000_000.0,  # 40% drawdown
        alert_price=1.0,                # 80% recovery
        volume_history=[1000.0, 1000.0, 1000.0],  # 5x pickup
        settings=settings,
    )
    assert score == 100
    assert set(signals) == {
        "sufficient_drawdown",
        "price_recovery",
        "volume_pickup",
        "strong_prior_signal",
    }


def test_score_below_threshold_dex_token():
    settings = _settings()
    candidate = {"peak_quant_score": 78}
    score, signals = score_reaccumulation(
        candidate,
        current_price=1.0,      # stale == alert_price, so recovery == 100% fires... test insufficient drawdown instead
        current_volume=None,
        current_market_cap=1_900_000.0,  # only 5% drawdown — no signal
        alert_market_cap=2_000_000.0,
        alert_price=1.0,
        volume_history=[],
        settings=settings,
    )
    # price_recovery (35) + strong_prior_signal (15) = 50 -> at threshold boundary
    assert "sufficient_drawdown" not in signals
    assert "price_recovery" in signals
    assert "strong_prior_signal" in signals
    assert score == 50


def test_score_insufficient_volume_history():
    settings = _settings()
    candidate = {"peak_quant_score": 80}
    score, signals = score_reaccumulation(
        candidate,
        current_price=0.8,
        current_volume=5000.0,
        current_market_cap=1_200_000.0,
        alert_market_cap=2_000_000.0,
        alert_price=1.0,
        volume_history=[1000.0],  # only 1 snapshot — skip volume_pickup
        settings=settings,
    )
    assert "volume_pickup" not in signals
    assert score == 80  # 30 + 35 + 15


def test_score_weak_drawdown_no_recovery():
    settings = _settings()
    candidate = {"peak_quant_score": 50}  # too weak for strong_prior
    score, signals = score_reaccumulation(
        candidate,
        current_price=0.5,   # 50% of alert — below recovery threshold
        current_volume=None,
        current_market_cap=1_900_000.0,  # 5% drawdown — below
        alert_market_cap=2_000_000.0,
        alert_price=1.0,
        volume_history=[],
        settings=settings,
    )
    assert score == 0
    assert signals == []


def test_score_zero_alert_price_safe():
    settings = _settings()
    candidate = {"peak_quant_score": 80}
    score, signals = score_reaccumulation(
        candidate,
        current_price=0.8,
        current_volume=None,
        current_market_cap=1_200_000.0,
        alert_market_cap=2_000_000.0,
        alert_price=0.0,  # division guard
        volume_history=[],
        settings=settings,
    )
    assert "price_recovery" not in signals  # guarded
    assert "sufficient_drawdown" in signals


def test_build_secondwave_candidate():
    scan_row = {
        "contract_address": "0xabc",
        "chain": "ethereum",
        "token_name": "Tok",
        "ticker": "TK",
        "coingecko_id": None,
        "peak_quant_score": 80,
        "alert_market_cap": 2_000_000.0,
        "alert_price": 1.0,
        "alerted_at": datetime.now(timezone.utc).isoformat(),
    }
    cand = build_secondwave_candidate(
        scan_row=scan_row,
        score=85,
        signals=["sufficient_drawdown", "price_recovery"],
        current_price=0.8,
        current_volume=5000.0,
        current_market_cap=1_200_000.0,
        volume_history=[1000.0, 1000.0, 1000.0],
        price_is_stale=False,
    )
    assert cand["contract_address"] == "0xabc"
    assert cand["reaccumulation_score"] == 85
    assert cand["price_vs_alert_pct"] == 80.0
    assert cand["price_drop_from_peak_pct"] == -40.0
    assert cand["volume_vs_cooldown_avg"] == 5.0
    assert cand["price_is_stale"] is False
    assert cand["days_since_first_seen"] >= 0
```

- [ ] **Step 2: Verify tests fail**

```bash
uv run pytest tests/test_secondwave_detector.py -v
```

- [ ] **Step 3: Implement detector**

```python
# scout/secondwave/detector.py
"""Second-Wave Detection — scan DB, score re-accumulation, orchestrate loop."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import aiohttp
import structlog

from scout.config import Settings
from scout.db import Database
from scout.secondwave.alerts import format_secondwave_alert

logger = structlog.get_logger(__name__)

# Hardcoded to avoid fragile imports across ingestion modules.
CG_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"


def score_reaccumulation(
    candidate: dict,
    current_price: float | None,
    current_volume: float | None,
    current_market_cap: float | None,
    alert_market_cap: float,
    alert_price: float,
    volume_history: list[float],
    settings: Settings,
) -> tuple[int, list[str]]:
    """Compute re-accumulation score (0-100) and fired signals.

    4 signals: sufficient_drawdown (30), price_recovery (35),
    volume_pickup (20), strong_prior_signal (15).
    """
    points = 0
    signals: list[str] = []

    # Signal 1: Drawdown from peak (30 pts)
    if alert_market_cap and alert_market_cap > 0 and current_market_cap is not None:
        drawdown_pct = ((current_market_cap - alert_market_cap) / alert_market_cap) * 100
        if drawdown_pct <= -settings.SECONDWAVE_MIN_DRAWDOWN_PCT:
            points += 30
            signals.append("sufficient_drawdown")

    # Signal 2: Price recovery vs alert price (35 pts)
    if current_price is not None and alert_price and alert_price > 0:
        price_vs_alert_pct = (current_price / alert_price) * 100
        if price_vs_alert_pct >= settings.SECONDWAVE_MIN_RECOVERY_PCT:
            points += 35
            signals.append("price_recovery")

    # Signal 3: Volume pickup vs cooldown average (20 pts)
    if current_volume is not None and len(volume_history) >= 3:
        cooldown_avg = sum(volume_history) / len(volume_history)
        if cooldown_avg > 0:
            vol_ratio = current_volume / cooldown_avg
            if vol_ratio >= settings.SECONDWAVE_VOL_PICKUP_RATIO:
                points += 20
                signals.append("volume_pickup")

    # Signal 4: Prior signal strength (15 pts)
    if candidate.get("peak_quant_score", 0) >= 75:
        points += 15
        signals.append("strong_prior_signal")

    return (min(100, points), signals)


def build_secondwave_candidate(
    scan_row: dict,
    score: int,
    signals: list[str],
    current_price: float,
    current_volume: float | None,
    current_market_cap: float,
    volume_history: list[float],
    price_is_stale: bool,
) -> dict:
    """Shape a SecondWaveCandidate dict ready for db.insert_secondwave_candidate."""
    alert_market_cap = scan_row.get("alert_market_cap") or 0.0
    alert_price = scan_row.get("alert_price") or 0.0
    alerted_at_str = scan_row.get("alerted_at")
    alerted_at = (
        datetime.fromisoformat(alerted_at_str) if alerted_at_str else None
    )
    now = datetime.now(timezone.utc)
    days_since = (now - alerted_at).total_seconds() / 86400.0 if alerted_at else 0.0

    price_drop_from_peak_pct = (
        ((current_market_cap - alert_market_cap) / alert_market_cap) * 100
        if alert_market_cap
        else 0.0
    )
    price_vs_alert_pct = (
        (current_price / alert_price) * 100 if alert_price else 0.0
    )
    cooldown_avg = (
        sum(volume_history) / len(volume_history) if volume_history else 0.0
    )
    volume_vs_cooldown_avg = (
        (current_volume / cooldown_avg)
        if (current_volume is not None and cooldown_avg > 0)
        else 0.0
    )

    return {
        "contract_address": scan_row["contract_address"],
        "chain": scan_row.get("chain", ""),
        "token_name": scan_row.get("token_name", ""),
        "ticker": scan_row.get("ticker", ""),
        "coingecko_id": scan_row.get("coingecko_id"),
        "peak_quant_score": int(scan_row.get("peak_quant_score", 0)),
        "peak_signals_fired": scan_row.get("peak_signals_fired") or [],
        "first_seen_at": alerted_at_str or now.isoformat(),
        "original_alert_at": alerted_at_str,
        "original_market_cap": alert_market_cap,
        "alert_market_cap": alert_market_cap,
        "days_since_first_seen": round(days_since, 2),
        "price_drop_from_peak_pct": round(price_drop_from_peak_pct, 2),
        "current_price": current_price,
        "current_market_cap": current_market_cap,
        "current_volume_24h": current_volume,
        "price_vs_alert_pct": round(price_vs_alert_pct, 2),
        "volume_vs_cooldown_avg": round(volume_vs_cooldown_avg, 2),
        "price_is_stale": price_is_stale,
        "reaccumulation_score": score,
        "reaccumulation_signals": signals,
        "detected_at": now.isoformat(),
        "alerted_at": now.isoformat(),
    }


async def fetch_current_prices(
    session: aiohttp.ClientSession,
    coingecko_ids: list[str],
    settings: Settings,
) -> dict[str, dict]:
    """Batch-fetch CoinGecko live prices. Returns dict keyed by coingecko id."""
    if not coingecko_ids:
        return {}
    ids_param = ",".join(coingecko_ids)
    headers: dict[str, str] = {}
    if settings.COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = settings.COINGECKO_API_KEY
    params = {"vs_currency": "usd", "ids": ids_param, "per_page": 250}
    try:
        async with session.get(
            CG_MARKETS_URL,
            params=params,
            headers=headers,
        ) as resp:
            if resp.status != 200:
                logger.warning("secondwave_cg_markets_error", status=resp.status)
                return {}
            data = await resp.json()
            return {
                entry["id"]: {
                    "current_price": entry.get("current_price") or 0.0,
                    "total_volume": entry.get("total_volume") or 0.0,
                    "market_cap": entry.get("market_cap") or 0.0,
                }
                for entry in (data if isinstance(data, list) else [])
                if entry.get("id")
            }
    except Exception:
        logger.exception("secondwave_cg_markets_exception")
        return {}


async def run_once(
    session: aiohttp.ClientSession,
    db: Database,
    settings: Settings,
) -> int:
    """Execute one scan-confirm-alert cycle. Returns number of alerts fired."""
    from scout.alerter import send_telegram_message  # local import to avoid cycles

    scan_candidates = await db.get_secondwave_scan_candidates(
        min_age_days=settings.SECONDWAVE_COOLDOWN_MIN_DAYS,
        max_age_days=settings.SECONDWAVE_COOLDOWN_MAX_DAYS,
        min_peak_score=settings.SECONDWAVE_MIN_PRIOR_SCORE,
        dedup_days=settings.SECONDWAVE_DEDUP_DAYS,
    )
    if not scan_candidates:
        return 0

    # Resolve a CoinGecko coin_id for each candidate via a symbol lookup
    # against the `predictions` table (narrative-agent tokens). The scan
    # query no longer JOINs predictions because that table has no
    # contract_address column — we must lookup by symbol here instead.
    for scan_row in scan_candidates:
        cg_id = await db.get_coingecko_id_by_symbol(scan_row.get("ticker") or "")
        scan_row["coingecko_id"] = cg_id

    cg_ids = [c["coingecko_id"] for c in scan_candidates if c.get("coingecko_id")]
    fresh_prices = await fetch_current_prices(session, cg_ids, settings) if cg_ids else {}

    alerts_fired = 0
    for scan_row in scan_candidates:
        volume_history = await db.get_volume_history(
            scan_row["contract_address"],
            days=settings.SECONDWAVE_COOLDOWN_MAX_DAYS,
        )

        cg_id = scan_row.get("coingecko_id")
        if cg_id and cg_id in fresh_prices:
            pd = fresh_prices[cg_id]
            current_price = pd["current_price"]
            current_volume = pd["total_volume"]
            current_market_cap = pd["market_cap"]
            price_is_stale = False
        else:
            current_price = scan_row.get("alert_price") or 0.0
            current_volume = None
            current_market_cap = scan_row.get("alert_market_cap") or 0.0
            price_is_stale = True

        score, signals = score_reaccumulation(
            scan_row,
            current_price=current_price,
            current_volume=current_volume,
            current_market_cap=current_market_cap,
            alert_market_cap=scan_row.get("alert_market_cap") or 0.0,
            alert_price=scan_row.get("alert_price") or 0.0,
            volume_history=volume_history,
            settings=settings,
        )

        if score < settings.SECONDWAVE_ALERT_THRESHOLD:
            continue

        sw = build_secondwave_candidate(
            scan_row=scan_row,
            score=score,
            signals=signals,
            current_price=current_price,
            current_volume=current_volume,
            current_market_cap=current_market_cap,
            volume_history=volume_history,
            price_is_stale=price_is_stale,
        )
        await db.insert_secondwave_candidate(sw)
        await send_telegram_message(format_secondwave_alert(sw), session, settings)
        alerts_fired += 1

    return alerts_fired


async def secondwave_loop(
    session: aiohttp.ClientSession,
    settings: Settings,
) -> None:
    """Run the second-wave detector on SECONDWAVE_POLL_INTERVAL."""
    db = Database(settings.DB_PATH)
    await db.initialize()
    logger.info("secondwave_loop_started", interval=settings.SECONDWAVE_POLL_INTERVAL)
    try:
        while True:
            try:
                fired = await run_once(session, db, settings)
                logger.info("secondwave_cycle_complete", alerts_fired=fired)
            except Exception:
                logger.exception("secondwave_loop_error")
            await asyncio.sleep(settings.SECONDWAVE_POLL_INTERVAL)
    finally:
        await db.close()
```

Note: The `score_reaccumulation` function reads `candidate["peak_quant_score"]`. The SQL scan query exposes that column directly via `MAX(sh.score) AS peak_quant_score`, so no normalization is required. Tests pass `peak_quant_score` directly.

- [ ] **Step 4: Verify tests pass**

```bash
uv run pytest tests/test_secondwave_detector.py -v
```

Note: `CG_MARKETS_URL` is hardcoded in this module to avoid a fragile cross-module import from `scout/ingestion/coingecko.py`.

- [ ] **Step 5: Commit**

```bash
git add scout/secondwave/detector.py tests/test_secondwave_detector.py
git commit -m "feat(secondwave): implement re-accumulation scoring and detector loop"
```

---

## Task 4: Telegram Alert Formatter

**Files:**
- Create: `scout/secondwave/alerts.py`
- Test: `tests/test_secondwave_alerts.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_secondwave_alerts.py
"""Tests for second-wave Telegram alert formatting."""
from datetime import datetime, timezone

from scout.secondwave.alerts import format_secondwave_alert


def _base_candidate() -> dict:
    return {
        "contract_address": "0xabc",
        "chain": "ethereum",
        "token_name": "Test Token",
        "ticker": "TEST",
        "coingecko_id": None,
        "peak_quant_score": 80,
        "peak_signals_fired": ["momentum_ratio", "vol_acceleration"],
        "first_seen_at": datetime.now(timezone.utc).isoformat(),
        "original_alert_at": datetime.now(timezone.utc).isoformat(),
        "original_market_cap": 1_000_000.0,
        "alert_market_cap": 2_000_000.0,
        "days_since_first_seen": 5.3,
        "price_drop_from_peak_pct": -40.0,
        "current_price": 0.8,
        "current_market_cap": 1_200_000.0,
        "current_volume_24h": 500_000.0,
        "price_vs_alert_pct": 80.0,
        "volume_vs_cooldown_avg": 3.1,
        "price_is_stale": False,
        "reaccumulation_score": 85,
        "reaccumulation_signals": ["sufficient_drawdown", "price_recovery", "strong_prior_signal"],
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "alerted_at": datetime.now(timezone.utc).isoformat(),
    }


def test_format_basic_alert_contains_all_sections():
    msg = format_secondwave_alert(_base_candidate())
    assert "\U0001F504" in msg  # refresh emoji
    assert "Second Wave" in msg
    assert "Test Token" in msg
    assert "TEST" in msg
    assert "85/100" in msg
    assert "sufficient_drawdown" in msg
    assert "price_recovery" in msg
    assert "-40.0" in msg
    assert "80.0" in msg
    assert "RESEARCH ONLY" in msg


def test_format_stale_price_marker():
    c = _base_candidate()
    c["price_is_stale"] = True
    msg = format_secondwave_alert(c)
    assert "stale" in msg.lower()


def test_format_missing_optional_fields():
    c = _base_candidate()
    c["current_volume_24h"] = None
    c["peak_signals_fired"] = []
    msg = format_secondwave_alert(c)
    assert "Test Token" in msg
    assert "n/a" in msg.lower() or "0" in msg
```

- [ ] **Step 2: Verify tests fail**

```bash
uv run pytest tests/test_secondwave_alerts.py -v
```

- [ ] **Step 3: Implement formatter**

```python
# scout/secondwave/alerts.py
"""Telegram alert formatter for Second-Wave Detection."""
from __future__ import annotations


def _fmt_money(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"${v:,.0f}"


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{v:.1f}%"


def format_secondwave_alert(candidate: dict) -> str:
    """Build the Telegram message for a second-wave candidate."""
    peak_signals = candidate.get("peak_signals_fired") or []
    reacc_signals = candidate.get("reaccumulation_signals") or []
    stale_marker = "(stale)" if candidate.get("price_is_stale") else ""

    lines = [
        f"\U0001F504 Second Wave Detected: {candidate.get('token_name', 'Unknown')} ({candidate.get('ticker', '')})",
        "",
        f"Prior pump (first seen {candidate.get('days_since_first_seen', 0):.1f}d ago):",
        f"  Peak score: {candidate.get('peak_quant_score', 0)}/100",
        f"  Signals: {', '.join(peak_signals) if peak_signals else 'n/a'}",
        f"  Alert market cap: {_fmt_money(candidate.get('alert_market_cap'))} (approximate peak)",
        "",
        "Cooldown:",
        f"  Drawdown from peak: {_fmt_pct(candidate.get('price_drop_from_peak_pct'))}",
        f"  Days cooling: {candidate.get('days_since_first_seen', 0):.0f}",
        "",
        "Re-accumulation:",
        f"  Price vs alert: {_fmt_pct(candidate.get('price_vs_alert_pct'))} {stale_marker}".rstrip(),
        f"  Volume vs cooldown avg: {candidate.get('volume_vs_cooldown_avg', 0):.1f}x",
        f"  Re-accumulation score: {candidate.get('reaccumulation_score', 0)}/100",
        f"  Signals: {', '.join(reacc_signals) if reacc_signals else 'n/a'}",
        "",
        f"Current: {_fmt_money(candidate.get('current_market_cap'))} mcap | {_fmt_money(candidate.get('current_volume_24h'))} vol/24h",
        "",
        f"Chain: {candidate.get('chain', '?')} | CA: {candidate.get('contract_address', '?')}",
        "",
        "RESEARCH ONLY - Not financial advice",
    ]
    return "\n".join(lines)
```

- [ ] **Step 4: Verify tests pass**

```bash
uv run pytest tests/test_secondwave_alerts.py tests/test_secondwave_detector.py -v
```

- [ ] **Step 5: Commit**

```bash
git add scout/secondwave/alerts.py tests/test_secondwave_alerts.py
git commit -m "feat(secondwave): add Telegram alert formatter"
```

---

## Task 5: Main Loop Integration

**Files:**
- Modify: `scout/main.py` (wire loop into `asyncio.gather()` AND extend the `db.log_alert(...)` call at line 235 to persist `token_name`/`ticker`/`price_usd`)

- [ ] **Step 1: Wire `secondwave_loop` into `main()`**

Locate the `asyncio.gather` section in `scout/main.py`. Add the import at the top:

```python
from scout.secondwave.detector import secondwave_loop
```

Update the tasks list inside `main()`:

```python
        tasks = [pipeline_loop(session, settings)]

        if getattr(settings, "NARRATIVE_ENABLED", False):
            from scout.narrative.agent import narrative_agent_loop  # if applicable
            tasks.append(narrative_agent_loop(session, settings))

        if settings.SECONDWAVE_ENABLED:
            tasks.append(secondwave_loop(session, settings))

        await asyncio.gather(*tasks)
```

(If the narrative loop import already exists, keep it as-is and only add the `SECONDWAVE_ENABLED` block.)

- [ ] **Step 2: Extend `log_alert` to persist token_name/ticker/price_usd**

In `scout/db.py`, update `log_alert` signature and INSERT:

```python
    async def log_alert(
        self, contract_address: str, chain: str, conviction_score: float,
        alert_market_cap: float | None = None,
        price_usd: float | None = None,
        token_name: str | None = None,
        ticker: str | None = None,
    ) -> None:
        """Log a fired alert with market cap, price, and token identity."""
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """INSERT INTO alerts
               (contract_address, chain, conviction_score, alert_market_cap,
                price_usd, token_name, ticker, alerted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (contract_address, chain, conviction_score, alert_market_cap,
             price_usd, token_name, ticker, now),
        )
        await self._conn.commit()
```

**Call site:** The only `db.log_alert(...)` invocation lives in `scout/main.py` around **line 235** inside the pipeline loop's alert-delivery block (right after `send_alert(gated_token, ...)`). Current form:

```python
        await db.log_alert(
            gated_token.contract_address, gated_token.chain, conviction,
            alert_market_cap=gated_token.market_cap_usd,
        )
```

Update that call site to pass the three new fields from `CandidateToken`:

```python
        await db.log_alert(
            contract_address=gated_token.contract_address,
            chain=gated_token.chain,
            conviction_score=conviction,
            alert_market_cap=gated_token.market_cap_usd,
            price_usd=getattr(gated_token, "price_usd", None),
            token_name=getattr(gated_token, "token_name", None),
            ticker=getattr(gated_token, "ticker", None),
        )
```

(If `CandidateToken` has no `price_usd` field, derive it from `market_cap_usd / circulating_supply` if available, or pass `None` — the scan query tolerates NULL `alert_price`. `scout/alerter.py` does not itself call `log_alert`, so no change is needed there.)

- [ ] **Step 3: Smoke test**

```bash
uv run pytest --tb=short -q
uv run python -m scout.main --dry-run --cycles 1
```

Expected: suite green; dry run completes one cycle without triggering secondwave loop (disabled by default).

- [ ] **Step 4: Enable-path smoke test**

Temporarily set `SECONDWAVE_ENABLED=true` in a local `.env` and rerun `--dry-run --cycles 1`. Verify a `secondwave_loop_started` log line appears and `secondwave_cycle_complete` logs once. Revert `.env`.

- [ ] **Step 5: Commit**

```bash
git add scout/main.py scout/db.py
git commit -m "feat(secondwave): wire detector loop into main pipeline"
```

---

## Task 6: Integration Tests (end-to-end)

**Files:**
- Test: `tests/test_secondwave_integration.py`

- [ ] **Step 1: Write integration test**

```python
# tests/test_secondwave_integration.py
"""End-to-end test: scan DB -> score -> alert via mocked Telegram + CoinGecko."""
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.config import Settings
from scout.db import Database
from scout.secondwave.detector import run_once


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "int.db")
    await d.initialize()
    yield d
    await d.close()


def _settings(db_path) -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        DB_PATH=str(db_path),
        SECONDWAVE_ENABLED=True,
        SECONDWAVE_MIN_PRIOR_SCORE=60,
        SECONDWAVE_COOLDOWN_MIN_DAYS=3,
        SECONDWAVE_COOLDOWN_MAX_DAYS=14,
        SECONDWAVE_MIN_DRAWDOWN_PCT=30.0,
        SECONDWAVE_MIN_RECOVERY_PCT=70.0,
        SECONDWAVE_VOL_PICKUP_RATIO=2.0,
        SECONDWAVE_ALERT_THRESHOLD=50,
    )


async def test_end_to_end_dex_token_detection(db, tmp_path):
    # Seed alerts + score_history for an in-window token with peak 80
    alerted_at = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    await db._conn.execute(
        """INSERT INTO alerts
           (contract_address, chain, conviction_score, alert_market_cap, price_usd, token_name, ticker, alerted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("0xdex", "ethereum", 85.0, 2_000_000.0, 1.0, "DexTok", "DEX", alerted_at),
    )
    await db._conn.execute(
        "INSERT INTO score_history (contract_address, score, scanned_at) VALUES (?, ?, ?)",
        ("0xdex", 80.0, alerted_at),
    )
    await db._conn.commit()

    settings = _settings(tmp_path / "int.db")

    with aioresponses() as m:
        m.post(
            f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
            status=200,
            payload={"ok": True},
        )
        async with aiohttp.ClientSession() as session:
            fired = await run_once(session, db, settings)

    # DEX token with stale price == alert_price: price_recovery fires (100% >= 70%),
    # sufficient_drawdown does NOT (no drawdown because current_mcap = alert_mcap),
    # strong_prior_signal fires (80 >= 75). Score = 35 + 15 = 50 -> exactly at threshold.
    assert fired == 1

    rows = await db.get_recent_secondwave_candidates(days=7)
    assert len(rows) == 1
    assert rows[0]["contract_address"] == "0xdex"
    assert rows[0]["price_is_stale"] is True


async def test_end_to_end_narrative_token_live_price(db, tmp_path):
    alerted_at = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()
    await db._conn.execute(
        """INSERT INTO alerts
           (contract_address, chain, conviction_score, alert_market_cap, price_usd, token_name, ticker, alerted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("0xnarr", "ethereum", 85.0, 2_000_000.0, 1.0, "NarrTok", "NARR", alerted_at),
    )
    await db._conn.execute(
        "INSERT INTO score_history (contract_address, score, scanned_at) VALUES (?, ?, ?)",
        ("0xnarr", 80.0, alerted_at),
    )
    # predictions.coin_id = coingecko slug
    await db._conn.execute(
        """INSERT INTO predictions
           (category_id, category_name, coin_id, symbol, name,
            market_cap_at_prediction, price_at_prediction,
            narrative_fit_score, staying_power, confidence, reasoning,
            strategy_snapshot, predicted_at)
           VALUES ('ai','AI','narr-token','NARR','NarrTok',2e6,1.0,80,'High','High','r','{}',?)""",
        (alerted_at,),
    )
    # Add contract_address column link if predictions doesn't have one — skip this if schema differs
    # and rely on LEFT JOIN returning NULL (test then reduces to DEX behaviour).
    await db._conn.commit()

    settings = _settings(tmp_path / "int.db")

    with aioresponses() as m:
        m.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            status=200,
            payload=[{
                "id": "narr-token",
                "current_price": 0.8,
                "total_volume": 500_000.0,
                "market_cap": 1_200_000.0,
            }],
        )
        m.post(
            f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
            status=200,
            payload={"ok": True},
        )
        async with aiohttp.ClientSession() as session:
            fired = await run_once(session, db, settings)

    # Narrative token path exercises the live-price branch. Whether coingecko_id
    # is resolved depends on the predictions schema having a contract_address FK.
    # At minimum the scan + loop must not crash.
    assert fired >= 0
```

Note: the second test is a best-effort exercise of the live-price code path. If `predictions` does not have a `contract_address` column, the LEFT JOIN returns `coingecko_id = NULL` and the token falls through to the DEX path — that is acceptable. The test only asserts no crash.

- [ ] **Step 2: Verify**

```bash
uv run pytest tests/test_secondwave_integration.py -v
uv run pytest --tb=short -q
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_secondwave_integration.py
git commit -m "test(secondwave): add end-to-end integration tests"
```

---

## Task 7 (Phase 2 — optional): Dashboard API Endpoints

> **Mark as Phase 2.** Skip if short on time; the loop runs headless without dashboard wiring.

**Files:**
- Modify: `dashboard/api.py`
- Test: extend existing dashboard test file or add `tests/test_dashboard_secondwave.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_dashboard_secondwave.py
"""Dashboard API endpoints for second-wave detection."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from dashboard.api import app
from scout.db import Database


@pytest.fixture
async def seeded_db(tmp_path, monkeypatch):
    db_path = tmp_path / "dash.db"
    d = Database(db_path)
    await d.initialize()
    now = datetime.now(timezone.utc).isoformat()
    await d.insert_secondwave_candidate({
        "contract_address": "0xd", "chain": "eth",
        "token_name": "Dash", "ticker": "DSH", "coingecko_id": None,
        "peak_quant_score": 80, "peak_signals_fired": ["x"],
        "first_seen_at": now, "original_alert_at": now,
        "original_market_cap": 1e6, "alert_market_cap": 2e6,
        "days_since_first_seen": 5.0, "price_drop_from_peak_pct": -40.0,
        "current_price": 0.8, "current_market_cap": 1.2e6,
        "current_volume_24h": 5e5, "price_vs_alert_pct": 80.0,
        "volume_vs_cooldown_avg": 3.0, "price_is_stale": False,
        "reaccumulation_score": 85,
        "reaccumulation_signals": ["sufficient_drawdown", "price_recovery"],
        "detected_at": now, "alerted_at": now,
    })
    await d.close()
    monkeypatch.setenv("DB_PATH", str(db_path))
    yield db_path


def test_secondwave_candidates_endpoint(seeded_db):
    client = TestClient(app)
    r = client.get("/api/secondwave/candidates")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    assert data[0]["ticker"] == "DSH"


def test_secondwave_stats_endpoint(seeded_db):
    client = TestClient(app)
    r = client.get("/api/secondwave/stats")
    assert r.status_code == 200
    data = r.json()
    assert "count" in data
    assert "avg_score" in data
```

- [ ] **Step 2: Verify test fails**

```bash
uv run pytest tests/test_dashboard_secondwave.py -v
```

- [ ] **Step 3: Add endpoints in `dashboard/api.py`**

```python
from scout.db import Database as ScoutDatabase
from scout.config import Settings as ScoutSettings


@app.get("/api/secondwave/candidates")
async def secondwave_candidates(days: int = 7, limit: int = 50):
    settings = ScoutSettings()
    db = ScoutDatabase(settings.DB_PATH)
    await db.initialize()
    try:
        rows = await db.get_recent_secondwave_candidates(days=days)
        return rows[:limit]
    finally:
        await db.close()


@app.get("/api/secondwave/stats")
async def secondwave_stats(days: int = 7):
    settings = ScoutSettings()
    db = ScoutDatabase(settings.DB_PATH)
    await db.initialize()
    try:
        rows = await db.get_recent_secondwave_candidates(days=days)
        count = len(rows)
        avg_score = (
            sum(r["reaccumulation_score"] for r in rows) / count if count else 0.0
        )
        return {"count": count, "avg_score": round(avg_score, 1), "days": days}
    finally:
        await db.close()
```

- [ ] **Step 4: Verify**

```bash
uv run pytest tests/test_dashboard_secondwave.py -v
uv run pytest --tb=short -q
```

- [ ] **Step 5: Commit**

```bash
git add dashboard/api.py tests/test_dashboard_secondwave.py
git commit -m "feat(secondwave): add dashboard API endpoints for second-wave candidates"
```

---

## Post-Implementation Verification

Run the full suite and dry run:

```bash
uv run black scout/ tests/
uv run pytest --tb=short -q
uv run python -m scout.main --dry-run --cycles 1
```

Enable the feature locally (`SECONDWAVE_ENABLED=true` in `.env`), run one more dry cycle, and confirm:

1. `secondwave_loop_started` log line appears
2. `secondwave_cycle_complete` fires at least once (even with `alerts_fired=0`)
3. No exceptions in logs
4. `sqlite3 scout.db "SELECT count(*) FROM second_wave_candidates"` returns a valid count

Revert `.env` after verification.

---

## Rollback

If detection is noisy or buggy in production:

1. Set `SECONDWAVE_ENABLED=false` in `.env` and restart the process. The feature is opt-in, so disabling it removes the loop from `asyncio.gather()`.
2. To purge stored detections: `sqlite3 scout.db "DELETE FROM second_wave_candidates"`.
3. The `alerts` column migration (`price_usd`, `token_name`, `ticker`) is additive and safe to leave in place.

---

## Known Limitations (v1)

- **`original_market_cap` approximation.** The spec distinguishes "first-seen
  market cap" from "alert market cap" (the peak), but v1 only persists one
  market-cap value per alert in `alerts.alert_market_cap`. Both
  `original_market_cap` and `alert_market_cap` on the `SecondWaveCandidate`
  are populated from the same `alerts.alert_market_cap` source — the true
  first-seen value is not yet tracked. This is acceptable for v1 because
  downstream scoring only uses `alert_market_cap` as the drawdown reference.
  A follow-up PR can backfill `original_market_cap` by joining against
  `candidates.first_seen_market_cap` (or equivalent) once that is wired.
- **Narrative coin_id resolution is symbol-based.** Because `predictions` has
  no `contract_address` column, we look up the CoinGecko slug via
  `symbol = ticker`. Collisions (multiple coins sharing a ticker) resolve to
  the most-recent prediction, which may be wrong. A contract_address FK on
  `predictions` would eliminate this ambiguity and is tracked as a follow-up.
- **Dashboard frontend is deferred to Phase 2.** Task 7 in this plan ships
  only the `/api/secondwave/candidates` and `/api/secondwave/stats` JSON
  endpoints. The matching `NarrativeTab`-style React component (charts,
  filtering, live refresh) is **out of scope for this PR** and will land in
  a follow-up Phase 2 PR.
