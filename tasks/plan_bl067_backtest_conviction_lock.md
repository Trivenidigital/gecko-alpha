# BL-067 backtest: Conviction-lock simulation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**New primitives introduced:** new script `scripts/backtest_conviction_lock.py` (read-only against `scout.db`); new internal helpers `_count_stacked_signals_in_window`, `_simulate_conviction_locked_exit`, `_reconstruct_price_path`, `_summarize_pnl_delta`; new test file `tests/test_backtest_conviction_lock.py`; new findings doc `tasks/findings_bl067_backtest_conviction_lock.md` (output of the run, not part of build). NO production code changes. NO new DB tables, columns, or settings.

**Prerequisites:** master ≥ `07875db` (BL-076 deployed — symbol+name now populated for narrative_prediction / volume_spike / chain_completed paths, makes case-study output more readable).

**Resume protocol authority:** per `backlog.md:412` — *"FIRST step is the backtest script. Do not write `scout/trading/conviction.py` until the backtest output justifies it."* This plan delivers ONLY the backtest script + findings; no production conviction code lands.

## Hermes-first analysis

**Domains checked against the 671-skill hub at `hermes-agent.nousresearch.com/docs/skills` (verified 2026-05-04):**

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Trading strategy backtesting | None found (closest: MLOps category covers ML model evaluation, not financial strategy replay) | Build inline (extending existing `scripts/backtest_v1_signal_stacking.py` pattern) |
| Paper-trade replay / point-in-time simulation | None found | Build inline (read-only sqlite + Python) |
| Signal-stack counter / multi-signal aggregation | None found | Reuse existing `_distinct_stack_count` helper at `scripts/backtest_v1_signal_stacking.py:69-156` |
| Exit-policy simulation (trail/sl/max_duration replay) | None found | Build inline |
| Historical price-path reconstruction from sqlite | None found | Build inline (JOIN gainers_snapshots / volume_history_cg / volume_spikes by coin_id + ts) |

**Awesome-hermes-agent ecosystem check:** No relevant repos.

**Verdict:** Pure project-internal research script. No Hermes-skill replacement. Reusing the existing `backtest_v1_signal_stacking.py` pattern (same project, same db.py shape, same output style) for consistency.

---

## Drift grounding (per alignment doc Part 3)

**Read before drafting (verified):**

- `backlog.md:367-413` — BL-067 spec: per-stack params table (1: defaults; 2: +72h max_duration / +5pp trail / +5pp sl; 3: +168h / +10pp / +10pp; ≥4: +336h / +15pp / +15pp), 9 open design questions, decision gate ≥10% PnL lift.
- `scripts/backtest_v1_signal_stacking.py:69-156` — existing `_distinct_stack_count(conn, token_id, opened_at, end_at)` helper. Returns `(count, source_list)`. Counts: gainers_snapshots, losers_snapshots, trending_snapshots, chain_matches, predictions (narrative), velocity_alerts, volume_spikes, tg_social_signals, plus `paper_trades` distinct signal_types on same token. **Reuse this — DO NOT reimplement.**
- `scripts/backtest_v1_signal_stacking.py:36-50` — fixed `TRADE_SIZE_USD = 300.0`, `DB_PATH = Path("scout.db")`, `_h(title)` header helper, `_conn()` factory. **Inherit conventions.**
- `scout/db.py:557-600` — `paper_trades` schema: NOT NULL on `entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price, opened_at`. Has `peak_pct REAL` (peak captured during actual open), `peak_price REAL`, `closed_at`, `exit_reason TEXT`, `signal_type`, `signal_data` (JSON), `pnl_pct`, `pnl_usd`.
- Snapshot tables for price-path reconstruction:
  - `gainers_snapshots(coin_id, price_at_snapshot, snapshot_at)` — most authoritative; populated from CoinGecko `/coins/markets`
  - `volume_history_cg(coin_id, price, recorded_at)` — CoinGecko volume telemetry
  - `volume_spikes(coin_id, price, detected_at)` — DexScreener-side
  - `losers_snapshots`, `trending_snapshots` — secondary sources
- `scout/trading/params.py` — Tier 1a per-signal params; defaults applicable as the simulation baseline.
- `scout/trading/evaluator.py:evaluate_paper_trades` — production exit logic: ladder + trailing-stop + SL + max_duration + moonshot-trail + peak-fade. **Simulator must mirror this logic** but with conviction-locked params overlaid.

**Pattern conformance:**
- New script lives in `scripts/` matching `backtest_v1_signal_stacking.py` shape
- Read-only (`mode=ro` URI not used — script runs against snapshot, not live DB)
- Output: stdout sectioned with `_h()` headers, results-as-text + machine-parseable JSON appendix
- Tests in `tests/test_backtest_conviction_lock.py` cover unit behavior of helpers + simulator

---

**Goal:** Quantify the simulated PnL lift from BL-067 conviction-locked exit gates against the last 30-90d of paper trades, AND replay BIO + LAB case studies, AND survey "BIO-like" cohort prevalence — to inform the decision gate (≥10% PnL lift = greenlight implementation).

**Architecture:** Single `scripts/backtest_conviction_lock.py` script with 4 sections (A–D). Section A computes per-trade stack counts at open-time + during-window. Section B simulates conviction-locked exits by reconstructing price paths from snapshot tables and applying extended trail/sl/max_duration per the stack count. Section C replays BIO + LAB specifically. Section D surveys the 30d cohort (how many tokens hit N≥3 stacked signals). Output: stdout report + JSON appendix at `findings_bl067_backtest_conviction_lock.md`.

**Tech Stack:** Python 3.12, sqlite3 (stdlib), no aiosqlite (sync; script not async). pytest for unit tests.

---

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `scripts/backtest_conviction_lock.py` | 4-section backtest script | Create |
| `tests/test_backtest_conviction_lock.py` | Unit tests for helpers + simulator | Create |
| `tasks/findings_bl067_backtest_conviction_lock.md` | Output template (filled in §"Run" task — NOT part of build) | Create at run-time only |

---

## Tasks

### Task 1: Stack-count + window helpers (reuse existing)

**Files:**
- Create: `scripts/backtest_conviction_lock.py` (initial skeleton)
- Test: `tests/test_backtest_conviction_lock.py`

- [ ] **Step 1: Write failing test for the existing helper reuse**

```python
# tests/test_backtest_conviction_lock.py
"""BL-067 backtest tests."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# Add scripts/ to sys.path so the test can import the script as a module.
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


@pytest.fixture
def db(tmp_path):
    """In-memory sqlite with schema seeded for backtest tests."""
    db_path = tmp_path / "t.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # Minimal schema for backtest helpers (only tables the helpers query)
    conn.executescript("""
        CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY,
            token_id TEXT, signal_type TEXT, signal_data TEXT,
            entry_price REAL, amount_usd REAL, quantity REAL,
            tp_pct REAL, sl_pct REAL, tp_price REAL, sl_price REAL,
            status TEXT, opened_at TEXT, closed_at TEXT,
            pnl_usd REAL, pnl_pct REAL, peak_pct REAL, peak_price REAL,
            exit_reason TEXT, signal_combo TEXT,
            symbol TEXT, name TEXT, chain TEXT
        );
        CREATE TABLE gainers_snapshots (
            coin_id TEXT, symbol TEXT, name TEXT, price_at_snapshot REAL,
            market_cap REAL, price_change_24h REAL, snapshot_at TEXT
        );
        CREATE TABLE losers_snapshots (
            coin_id TEXT, symbol TEXT, name TEXT, price_at_snapshot REAL,
            market_cap REAL, price_change_24h REAL, snapshot_at TEXT
        );
        CREATE TABLE trending_snapshots (
            coin_id TEXT, symbol TEXT, name TEXT, price_at_snapshot REAL,
            snapshot_at TEXT
        );
        CREATE TABLE chain_matches (
            id INTEGER PRIMARY KEY, token_id TEXT, pattern_name TEXT,
            outcome_change_pct REAL, completed_at TEXT
        );
        CREATE TABLE predictions (
            id INTEGER PRIMARY KEY, coin_id TEXT, predicted_at TEXT
        );
        CREATE TABLE velocity_alerts (
            id INTEGER PRIMARY KEY, coin_id TEXT, detected_at TEXT
        );
        CREATE TABLE volume_spikes (
            id INTEGER PRIMARY KEY, coin_id TEXT, symbol TEXT, name TEXT,
            price REAL, detected_at TEXT
        );
        CREATE TABLE tg_social_signals (
            id INTEGER PRIMARY KEY, token_id TEXT, created_at TEXT
        );
        CREATE TABLE volume_history_cg (
            coin_id TEXT, symbol TEXT, name TEXT, price REAL, recorded_at TEXT
        );
        CREATE TABLE price_cache (
            coin_id TEXT PRIMARY KEY, current_price REAL, market_cap REAL,
            updated_at TEXT
        );
    """)
    conn.commit()
    yield conn
    conn.close()


def test_count_stacked_signals_returns_zero_for_isolated_token(db):
    """T1 — token with no signals returns stack=0."""
    from backtest_conviction_lock import _count_stacked_signals_in_window
    n, sources = _count_stacked_signals_in_window(
        db, "lonely-coin", "2026-05-01T00:00:00+00:00", "2026-05-02T00:00:00+00:00"
    )
    assert n == 0
    assert sources == []


def test_count_stacked_signals_counts_distinct_sources(db):
    """T1b — same token in 3 different signal sources counts as 3."""
    from backtest_conviction_lock import _count_stacked_signals_in_window
    db.execute(
        "INSERT INTO gainers_snapshots (coin_id, symbol, name, "
        "price_at_snapshot, market_cap, price_change_24h, snapshot_at) "
        "VALUES ('multi', 'M', 'Multi', 1.0, 1e6, 12.0, '2026-05-01T01:00:00+00:00')"
    )
    db.execute(
        "INSERT INTO trending_snapshots (coin_id, symbol, name, "
        "price_at_snapshot, snapshot_at) "
        "VALUES ('multi', 'M', 'Multi', 1.0, '2026-05-01T02:00:00+00:00')"
    )
    db.execute(
        "INSERT INTO volume_spikes (coin_id, symbol, name, price, detected_at) "
        "VALUES ('multi', 'M', 'Multi', 1.0, '2026-05-01T03:00:00+00:00')"
    )
    db.commit()
    n, sources = _count_stacked_signals_in_window(
        db, "multi", "2026-05-01T00:00:00+00:00", "2026-05-02T00:00:00+00:00"
    )
    assert n == 3
    assert "gainers" in sources
    assert "trending" in sources
    assert "volume_spike" in sources
```

- [ ] **Step 2: Run test to verify it fails**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_backtest_conviction_lock.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'backtest_conviction_lock'`.

- [ ] **Step 3: Create the script with the helper**

```python
# scripts/backtest_conviction_lock.py
"""BL-067 backtest: Conviction-lock simulation.

Per backlog.md:367 BL-067 spec, this script:

A. Computes the signal-stack distribution across closed paper trades in
   the last 30d. (Reused from scripts/backtest_v1_signal_stacking.py
   helper pattern but adapted to the conviction-lock framing.)

B. Simulates conviction-locked exit gates against the actual closed
   trades. For each trade with stack >= 2, replays the exit logic with
   extended max_duration / trail_pct / sl_pct per the BL-067 table.
   Computes simulated PnL delta vs actual.

C. Replays BIO + LAB case studies (operator-flagged 2026-04-30 +
   2026-05-04) — shows actual vs simulated for these specific tokens.

D. BIO-like cohort survey: counts tokens in 30d window that hit N>=3
   stacked signals. Decision-gate input: if cohort is 1 token, BL-067
   is poor ROI; if >10, the case for implementation strengthens.

Run on VPS:
    cd /root/gecko-alpha && uv run python scripts/backtest_conviction_lock.py

No production changes. Pure analysis. Per backlog.md:412 resume protocol,
the conviction-lock production code (`scout/trading/conviction.py`) is
NOT in scope of this PR — it's gated on this backtest's output showing
>=10% PnL lift on simulated 30d window vs actual.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Iterable

DB_PATH = Path("scout.db")
TRADE_SIZE_USD = 300.0


def _h(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _conn(db_path: Path = DB_PATH) -> sqlite3.Connection:
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    return c


# ---------------------------------------------------------------------------
# Stack-count helper — same pattern as scripts/backtest_v1_signal_stacking.py
# (kept independent so this script is standalone; helper signature parallels)
# ---------------------------------------------------------------------------

# (table, ts_column, label) — DISTINCT-source counting (BIO study principle:
# signal class diversity matters, not signal-event volume).
_SIGNAL_SOURCES = [
    ("gainers_snapshots", "snapshot_at", "gainers"),
    ("losers_snapshots", "snapshot_at", "losers"),
    ("trending_snapshots", "snapshot_at", "trending"),
    ("chain_matches", "completed_at", "chains"),
]


def _count_stacked_signals_in_window(
    conn: sqlite3.Connection,
    token_id: str,
    opened_at: str,
    end_at: str,
) -> tuple[int, list[str]]:
    """Count DISTINCT signal-source firings on token_id within [opened_at, end_at].

    Each source contributes at most 1 to the stack count. A token grinding
    on gainers for 100 ticks counts as 1 'gainers', not 100. This is the
    BIO/LAB-derived principle: class diversity, not event volume.

    Sources counted: gainers_snapshots, losers_snapshots, trending_snapshots,
    chain_matches, predictions (narrative agent), velocity_alerts,
    volume_spikes, tg_social_signals, plus other paper_trades signal_types
    on same token (independent confirmation).
    """
    sources: list[str] = []
    for table, ts_col, label in _SIGNAL_SOURCES:
        token_col = "token_id" if table == "chain_matches" else "coin_id"
        cur = conn.execute(
            f"""SELECT 1 FROM {table}
                WHERE {token_col} = ?
                  AND datetime({ts_col}) >= datetime(?)
                  AND datetime({ts_col}) <= datetime(?)
                LIMIT 1""",
            (token_id, opened_at, end_at),
        )
        if cur.fetchone() is not None:
            sources.append(label)

    for table, ts_col, label in [
        ("predictions", "predicted_at", "narrative"),
        ("velocity_alerts", "detected_at", "velocity"),
        ("volume_spikes", "detected_at", "volume_spike"),
        ("tg_social_signals", "created_at", "tg_social"),
    ]:
        try:
            token_col = "token_id" if table == "tg_social_signals" else "coin_id"
            cur = conn.execute(
                f"""SELECT 1 FROM {table}
                    WHERE {token_col} = ?
                      AND datetime({ts_col}) >= datetime(?)
                      AND datetime({ts_col}) <= datetime(?)
                    LIMIT 1""",
                (token_id, opened_at, end_at),
            )
            if cur.fetchone() is not None:
                sources.append(label)
        except sqlite3.OperationalError:
            # Table or column missing on this branch — skip
            pass

    cur = conn.execute(
        """SELECT DISTINCT signal_type FROM paper_trades
           WHERE token_id = ?
             AND datetime(opened_at) >= datetime(?)
             AND datetime(opened_at) <= datetime(?)""",
        (token_id, opened_at, end_at),
    )
    other_signal_types = {r[0] for r in cur.fetchall()}
    for st in other_signal_types:
        sources.append(f"trade:{st}")

    return len(sources), sources


if __name__ == "__main__":
    pass  # Sections wired in subsequent tasks
```

- [ ] **Step 4: Run test to verify it passes**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_backtest_conviction_lock.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/backtest_conviction_lock.py tests/test_backtest_conviction_lock.py
git commit -m "feat(BL-067 backtest): script skeleton + stack-count helper + tests"
```

---

### Task 2: Conviction-lock param table + composition

**Files:**
- Modify: `scripts/backtest_conviction_lock.py`
- Test: `tests/test_backtest_conviction_lock.py`

- [ ] **Step 1: Write failing test for param composition**

```python
def test_conviction_locked_params_for_stack_count():
    """T2 — pins BL-067 spec table at backlog.md:374-380."""
    from backtest_conviction_lock import conviction_locked_params

    # Defaults: stack=1 returns base params unchanged
    p = conviction_locked_params(
        stack=1,
        base={"max_duration_hours": 168, "trail_pct": 20, "sl_pct": 25},
    )
    assert p["max_duration_hours"] == 168
    assert p["trail_pct"] == 20
    assert p["sl_pct"] == 25

    # Stack=2: +72h max, +5pp trail (cap 35), +5pp sl (cap 35)
    p = conviction_locked_params(
        stack=2,
        base={"max_duration_hours": 168, "trail_pct": 20, "sl_pct": 25},
    )
    assert p["max_duration_hours"] == 240
    assert p["trail_pct"] == 25
    assert p["sl_pct"] == 30

    # Stack=3: +168h, +10pp trail, +10pp sl (caps 35 / 40)
    p = conviction_locked_params(
        stack=3,
        base={"max_duration_hours": 168, "trail_pct": 20, "sl_pct": 25},
    )
    assert p["max_duration_hours"] == 336
    assert p["trail_pct"] == 30
    assert p["sl_pct"] == 35

    # Stack>=4: +336h, +15pp trail (cap 35), +15pp sl (cap 40)
    p = conviction_locked_params(
        stack=4,
        base={"max_duration_hours": 168, "trail_pct": 20, "sl_pct": 25},
    )
    assert p["max_duration_hours"] == 504
    assert p["trail_pct"] == 35  # cap hit
    assert p["sl_pct"] == 40

    # Stack=10: same as stack=4 (saturated)
    p = conviction_locked_params(
        stack=10,
        base={"max_duration_hours": 168, "trail_pct": 20, "sl_pct": 25},
    )
    assert p["max_duration_hours"] == 504
    assert p["trail_pct"] == 35
    assert p["sl_pct"] == 40
```

- [ ] **Step 2: Run test to verify it fails**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_backtest_conviction_lock.py::test_conviction_locked_params_for_stack_count -v
```

Expected: FAIL — `conviction_locked_params` not defined.

- [ ] **Step 3: Implement `conviction_locked_params`**

Add to `scripts/backtest_conviction_lock.py`:

```python
# BL-067 spec table (backlog.md:374-380):
#   stack=1: defaults
#   stack=2: +72h max, +5pp trail (cap 35), +5pp sl (cap 35)
#   stack=3: +168h max, +10pp trail (cap 35), +10pp sl (cap 40)
#   stack>=4: +336h max, +15pp trail (cap 35), +15pp sl (cap 40)
_CONVICTION_LOCK_DELTAS = {
    1: {"max_duration_hours": 0, "trail_pct": 0, "sl_pct": 0,
        "trail_cap": 35, "sl_cap": 25},  # base
    2: {"max_duration_hours": 72, "trail_pct": 5, "sl_pct": 5,
        "trail_cap": 35, "sl_cap": 35},
    3: {"max_duration_hours": 168, "trail_pct": 10, "sl_pct": 10,
        "trail_cap": 35, "sl_cap": 40},
    4: {"max_duration_hours": 336, "trail_pct": 15, "sl_pct": 15,
        "trail_cap": 35, "sl_cap": 40},
}


def conviction_locked_params(stack: int, base: dict) -> dict:
    """Return base params with BL-067 conviction-lock deltas applied.

    Saturates at stack=4 (any stack >= 4 gets the same params). Stack=1
    returns base unchanged. Trail and SL caps applied AFTER addition so
    a generous base + max delta doesn't exceed caps.
    """
    bucket = min(max(stack, 1), 4)
    delta = _CONVICTION_LOCK_DELTAS[bucket]
    return {
        "max_duration_hours": base["max_duration_hours"] + delta["max_duration_hours"],
        "trail_pct": min(base["trail_pct"] + delta["trail_pct"], delta["trail_cap"]),
        "sl_pct": min(base["sl_pct"] + delta["sl_pct"], delta["sl_cap"]),
    }
```

- [ ] **Step 4: Run test to verify it passes**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_backtest_conviction_lock.py::test_conviction_locked_params_for_stack_count -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/backtest_conviction_lock.py tests/test_backtest_conviction_lock.py
git commit -m "feat(BL-067 backtest): conviction_locked_params composition (T2)"
```

---

### Task 3: Price-path reconstruction helper

**Files:**
- Modify: `scripts/backtest_conviction_lock.py`
- Test: `tests/test_backtest_conviction_lock.py`

For each closed trade we need PRICES AFTER actual exit (to know what would have happened under extended hold). Reconstruct from snapshot tables.

- [ ] **Step 1: Write failing test**

```python
def test_reconstruct_price_path_returns_chronological_prices(db):
    """T3 — gather all (timestamp, price) tuples for coin_id from snapshot
    tables in [start, end], deduplicated, sorted ascending by ts."""
    from backtest_conviction_lock import _reconstruct_price_path

    db.executescript("""
        INSERT INTO gainers_snapshots (coin_id, symbol, name,
            price_at_snapshot, market_cap, price_change_24h, snapshot_at)
        VALUES ('coin', 'C', 'Coin', 1.0, 1e6, 5.0, '2026-05-01T01:00:00+00:00');
        INSERT INTO gainers_snapshots (coin_id, symbol, name,
            price_at_snapshot, market_cap, price_change_24h, snapshot_at)
        VALUES ('coin', 'C', 'Coin', 1.5, 1e6, 5.0, '2026-05-01T05:00:00+00:00');
        INSERT INTO volume_history_cg (coin_id, symbol, name, price, recorded_at)
        VALUES ('coin', 'C', 'Coin', 1.2, '2026-05-01T03:00:00+00:00');
        INSERT INTO volume_spikes (coin_id, symbol, name, price, detected_at)
        VALUES ('coin', 'C', 'Coin', 0.9, '2026-04-30T23:00:00+00:00');
    """)
    db.commit()

    path = _reconstruct_price_path(
        db, "coin",
        start="2026-05-01T00:00:00+00:00",
        end="2026-05-01T06:00:00+00:00",
    )
    # Chronological, in window, prices > 0
    assert len(path) == 3  # gainers x2 + volume_history (volume_spike out of window)
    assert path[0][1] == 1.0
    assert path[1][1] == 1.2
    assert path[2][1] == 1.5
```

- [ ] **Step 2: Run test to verify it fails**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_backtest_conviction_lock.py::test_reconstruct_price_path_returns_chronological_prices -v
```

Expected: FAIL.

- [ ] **Step 3: Implement `_reconstruct_price_path`**

Add to script:

```python
def _reconstruct_price_path(
    conn: sqlite3.Connection,
    coin_id: str,
    *,
    start: str,
    end: str,
) -> list[tuple[str, float]]:
    """Reconstruct (timestamp, price) chronologically from snapshot tables.

    Sources (UNION ALL): gainers_snapshots.price_at_snapshot,
    volume_history_cg.price, volume_spikes.price. losers_snapshots and
    trending_snapshots also have price_at_snapshot — included.

    Filters: prices > 0 (skip mcap=0 rows from the BL-075 detection-blind-spot
    case), within [start, end] inclusive.

    Returns chronologically-sorted list. May contain duplicate timestamps
    from different sources (acceptable — simulator picks max peak).
    """
    rows: list[tuple[str, float]] = []
    queries = [
        ("SELECT snapshot_at, price_at_snapshot FROM gainers_snapshots "
         "WHERE coin_id = ? AND price_at_snapshot > 0 "
         "AND datetime(snapshot_at) >= datetime(?) "
         "AND datetime(snapshot_at) <= datetime(?)"),
        ("SELECT snapshot_at, price_at_snapshot FROM losers_snapshots "
         "WHERE coin_id = ? AND price_at_snapshot > 0 "
         "AND datetime(snapshot_at) >= datetime(?) "
         "AND datetime(snapshot_at) <= datetime(?)"),
        ("SELECT snapshot_at, price_at_snapshot FROM trending_snapshots "
         "WHERE coin_id = ? AND price_at_snapshot > 0 "
         "AND datetime(snapshot_at) >= datetime(?) "
         "AND datetime(snapshot_at) <= datetime(?)"),
        ("SELECT recorded_at, price FROM volume_history_cg "
         "WHERE coin_id = ? AND price > 0 "
         "AND datetime(recorded_at) >= datetime(?) "
         "AND datetime(recorded_at) <= datetime(?)"),
        ("SELECT detected_at, price FROM volume_spikes "
         "WHERE coin_id = ? AND price > 0 "
         "AND datetime(detected_at) >= datetime(?) "
         "AND datetime(detected_at) <= datetime(?)"),
    ]
    for q in queries:
        try:
            cur = conn.execute(q, (coin_id, start, end))
            for ts, price in cur.fetchall():
                if ts and price and price > 0:
                    rows.append((ts, float(price)))
        except sqlite3.OperationalError:
            # Table missing — skip
            pass
    rows.sort(key=lambda r: r[0])
    return rows
```

- [ ] **Step 4: Run test to verify it passes**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_backtest_conviction_lock.py::test_reconstruct_price_path_returns_chronological_prices -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/backtest_conviction_lock.py tests/test_backtest_conviction_lock.py
git commit -m "feat(BL-067 backtest): _reconstruct_price_path helper (T3)"
```

---

### Task 4: Exit simulator

**Files:**
- Modify: `scripts/backtest_conviction_lock.py`
- Test: `tests/test_backtest_conviction_lock.py`

The simulator: given (entry_price, opened_at, params, price_path), replay exit logic and return (exit_price, exit_reason, hold_hours, peak_pct, pnl_pct).

**Logic mirrors `scout/trading/evaluator.py:evaluate_paper_trades`:**
- Track running peak from entry forward
- If peak_pct ≥ trail_pct, arm trailing stop at `peak * (1 - trail_pct/100)`. Once armed, close at first price ≤ trail_stop_price.
- If price ≤ entry * (1 - sl_pct/100) (and trail not yet armed), close at SL.
- If hold_hours ≥ max_duration, close at expiry (use last price in window).
- (Skipping ladder + moonshot + peak-fade in v1 — those are the 3 most complex production paths; simulating them adds complexity without changing the BL-067 question. Document in §"Honest scope".)

- [ ] **Step 1: Write failing test (basic SL)**

```python
def test_simulate_exit_hits_stop_loss():
    """T4 — entry=1.0, sl_pct=20: price drops to 0.79 → SL fires at 0.80."""
    from backtest_conviction_lock import _simulate_conviction_locked_exit

    path = [
        ("2026-05-01T00:30:00+00:00", 0.95),
        ("2026-05-01T01:00:00+00:00", 0.85),
        ("2026-05-01T02:00:00+00:00", 0.79),
    ]
    result = _simulate_conviction_locked_exit(
        entry_price=1.0,
        opened_at="2026-05-01T00:00:00+00:00",
        params={"max_duration_hours": 168, "trail_pct": 20, "sl_pct": 20},
        price_path=path,
    )
    assert result["exit_reason"] == "stop_loss"
    assert result["exit_price"] == pytest.approx(0.80, rel=0.01)
    assert result["pnl_pct"] == pytest.approx(-20.0, abs=1.0)


def test_simulate_exit_hits_trailing_stop():
    """T4b — peak 50%, trail 20%: closes at peak*(1-0.20) = 1.20."""
    from backtest_conviction_lock import _simulate_conviction_locked_exit

    path = [
        ("2026-05-01T01:00:00+00:00", 1.30),
        ("2026-05-01T02:00:00+00:00", 1.50),  # peak +50%
        ("2026-05-01T03:00:00+00:00", 1.20),  # = 1.50 * 0.80 — trail fires
    ]
    result = _simulate_conviction_locked_exit(
        entry_price=1.0,
        opened_at="2026-05-01T00:00:00+00:00",
        params={"max_duration_hours": 168, "trail_pct": 20, "sl_pct": 20},
        price_path=path,
    )
    assert result["exit_reason"] == "trailing_stop"
    assert result["peak_pct"] == pytest.approx(50.0, abs=0.5)
    assert result["pnl_pct"] == pytest.approx(20.0, abs=0.5)


def test_simulate_exit_max_duration():
    """T4c — no peak ≥ trail, no SL: closes at expiry with last price."""
    from backtest_conviction_lock import _simulate_conviction_locked_exit

    path = [
        ("2026-05-01T01:00:00+00:00", 1.05),
        ("2026-05-01T02:00:00+00:00", 1.10),
        ("2026-05-08T00:00:00+00:00", 1.15),  # 168h later
    ]
    result = _simulate_conviction_locked_exit(
        entry_price=1.0,
        opened_at="2026-05-01T00:00:00+00:00",
        params={"max_duration_hours": 168, "trail_pct": 20, "sl_pct": 20},
        price_path=path,
    )
    assert result["exit_reason"] == "expired"
    assert result["pnl_pct"] == pytest.approx(15.0, abs=0.5)
```

- [ ] **Step 2: Run tests to verify they fail**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_backtest_conviction_lock.py -k simulate_exit -v
```

Expected: 3 FAIL.

- [ ] **Step 3: Implement `_simulate_conviction_locked_exit`**

```python
from datetime import datetime


def _parse_iso(ts: str) -> datetime:
    """Parse ISO-8601 timestamp; tolerate trailing 'Z' and '+00:00'."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _simulate_conviction_locked_exit(
    *,
    entry_price: float,
    opened_at: str,
    params: dict,
    price_path: list[tuple[str, float]],
) -> dict:
    """Replay exit logic against price_path with conviction-locked params.

    Mirrors scout/trading/evaluator.py:evaluate_paper_trades simplified
    to: trailing_stop + stop_loss + max_duration. Skips ladder + moonshot
    + peak-fade (v1 — see §"Honest scope" for rationale).

    Returns dict with: exit_price, exit_reason, hold_hours, peak_pct,
    pnl_pct. exit_reason ∈ {trailing_stop, stop_loss, expired, no_data}.
    """
    if not price_path:
        return {
            "exit_price": entry_price,
            "exit_reason": "no_data",
            "hold_hours": 0.0,
            "peak_pct": 0.0,
            "pnl_pct": 0.0,
        }
    open_dt = _parse_iso(opened_at)
    sl_price = entry_price * (1 - params["sl_pct"] / 100.0)
    trail_arm_threshold = params["trail_pct"]  # peak_pct must reach this to arm
    max_hours = params["max_duration_hours"]
    peak_price = entry_price
    peak_pct = 0.0
    trail_armed = False
    trail_stop_price = 0.0

    for ts, price in price_path:
        cur_dt = _parse_iso(ts)
        hours = (cur_dt - open_dt).total_seconds() / 3600.0
        if hours > max_hours:
            # Close at last in-window price (price_path is in-window — see
            # _reconstruct_price_path filter — so this is the expiry price).
            return {
                "exit_price": price,
                "exit_reason": "expired",
                "hold_hours": hours,
                "peak_pct": peak_pct,
                "pnl_pct": (price - entry_price) / entry_price * 100.0,
            }
        # SL check (only if trail not yet armed; trailing stops up the SL)
        if not trail_armed and price <= sl_price:
            return {
                "exit_price": sl_price,
                "exit_reason": "stop_loss",
                "hold_hours": hours,
                "peak_pct": peak_pct,
                "pnl_pct": -params["sl_pct"],
            }
        # Track peak
        if price > peak_price:
            peak_price = price
            peak_pct = (peak_price - entry_price) / entry_price * 100.0
            if peak_pct >= trail_arm_threshold:
                trail_armed = True
                trail_stop_price = peak_price * (1 - params["trail_pct"] / 100.0)
        # Trail check
        if trail_armed and price <= trail_stop_price:
            return {
                "exit_price": trail_stop_price,
                "exit_reason": "trailing_stop",
                "hold_hours": hours,
                "peak_pct": peak_pct,
                "pnl_pct": (trail_stop_price - entry_price) / entry_price * 100.0,
            }
    # Walked entire path without exit and within max_duration — return as
    # held-to-end (use last price + last timestamp).
    last_ts, last_price = price_path[-1]
    last_hours = (_parse_iso(last_ts) - open_dt).total_seconds() / 3600.0
    return {
        "exit_price": last_price,
        "exit_reason": "held_to_end",
        "hold_hours": last_hours,
        "peak_pct": peak_pct,
        "pnl_pct": (last_price - entry_price) / entry_price * 100.0,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_backtest_conviction_lock.py -k simulate_exit -v
```

Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/backtest_conviction_lock.py tests/test_backtest_conviction_lock.py
git commit -m "feat(BL-067 backtest): _simulate_conviction_locked_exit (T4)"
```

---

### Task 5: Section A — stack-distribution + Section B — simulation

**Files:**
- Modify: `scripts/backtest_conviction_lock.py`

- [ ] **Step 1: Add Section A (stack histogram across closed paper trades)**

```python
def section_a(conn: sqlite3.Connection, *, days: int = 30) -> dict:
    """Section A: stack-distribution histogram for paper trades closed in
    last N days. Reuses scripts/backtest_v1_signal_stacking.py findings
    but reframes for the BL-067 decision."""
    _h(f"SECTION A — Stack distribution (closed paper trades, last {days}d)")
    cur = conn.execute(
        f"""SELECT id, token_id, signal_type, status, opened_at, closed_at,
                   pnl_usd, pnl_pct, peak_pct, exit_reason
            FROM paper_trades
            WHERE status LIKE 'closed_%'
              AND datetime(opened_at) >= datetime('now','-{days} days')
            ORDER BY opened_at""",
    )
    trades = cur.fetchall()
    by_stack: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for t in trades:
        end_at = t["closed_at"] or "now"
        if end_at == "now":
            cur2 = conn.execute("SELECT datetime('now')")
            end_at = cur2.fetchone()[0]
        n, _ = _count_stacked_signals_in_window(
            conn, t["token_id"], t["opened_at"], end_at,
        )
        by_stack[n].append(t)

    print(f"Total closed trades: {len(trades)}")
    print()
    print(f"{'Stack':<7} {'n':<5} {'avg_pnl_usd':<14} {'avg_peak%':<12} {'win%':<8} {'expired%':<10}")
    print("-" * 60)
    summary: dict[int, dict] = {}
    for stack in sorted(by_stack):
        ts = by_stack[stack]
        n = len(ts)
        if n == 0:
            continue
        avg_pnl = sum(t["pnl_usd"] or 0 for t in ts) / n
        avg_peak = sum(t["peak_pct"] or 0 for t in ts) / n
        wins = sum(1 for t in ts if (t["pnl_usd"] or 0) > 0)
        expired = sum(1 for t in ts if t["status"] == "closed_expired")
        print(
            f"{stack:<7} {n:<5} ${avg_pnl:>10.2f}    "
            f"{avg_peak:>8.2f}%    {100*wins/n:>5.1f}%   "
            f"{100*expired/n:>5.1f}%"
        )
        summary[stack] = {
            "n": n, "avg_pnl_usd": avg_pnl, "avg_peak_pct": avg_peak,
            "win_pct": 100 * wins / n, "expired_pct": 100 * expired / n,
        }
    return {"section_a": summary, "trade_count": len(trades), "window_days": days}
```

- [ ] **Step 2: Add Section B (simulation per-trade)**

```python
def section_b(conn: sqlite3.Connection, *, days: int = 30) -> dict:
    """Section B: simulate conviction-locked exit on every closed trade
    in the window where stack >= 2. Compare simulated PnL vs actual.
    Decision-gate input: ≥ 10% lift on aggregate net PnL = greenlight."""
    _h(f"SECTION B — Conviction-lock simulation (last {days}d)")
    cur = conn.execute(
        f"""SELECT id, token_id, symbol, signal_type, status, opened_at,
                   closed_at, entry_price, pnl_usd, pnl_pct, peak_pct,
                   exit_reason, tp_pct, sl_pct
            FROM paper_trades
            WHERE status LIKE 'closed_%'
              AND datetime(opened_at) >= datetime('now','-{days} days')
            ORDER BY opened_at""",
    )
    trades = cur.fetchall()
    actual_total = 0.0
    sim_total = 0.0
    locked_count = 0
    deltas: list[dict] = []

    for t in trades:
        actual_pnl = t["pnl_usd"] or 0.0
        actual_total += actual_pnl
        end_at = t["closed_at"]
        n, _ = _count_stacked_signals_in_window(
            conn, t["token_id"], t["opened_at"], end_at,
        )
        if n < 2:
            sim_total += actual_pnl  # No lock — same outcome
            continue
        locked_count += 1
        # Compute extended-window end based on conviction-locked max_duration
        base_max = (
            (_parse_iso(end_at) - _parse_iso(t["opened_at"])).total_seconds() / 3600.0
        )
        # Use trade's own tp/sl as "base" (could read signal_params, but
        # this approximates the per-trade actual configuration well enough
        # for v1 backtest)
        base_params = {
            "max_duration_hours": max(base_max, 168.0),  # at least default
            "trail_pct": 20.0,  # current default; could read signal_params
            "sl_pct": float(t["sl_pct"] or 25.0),
        }
        locked = conviction_locked_params(stack=n, base=base_params)
        sim_window_end = (
            _parse_iso(t["opened_at"])
            .replace(microsecond=0)
            .isoformat()
        )
        # Window: opened_at + locked["max_duration_hours"] OR now (whichever earlier)
        from datetime import timedelta
        window_end_dt = _parse_iso(t["opened_at"]) + timedelta(
            hours=locked["max_duration_hours"]
        )
        cur_now = conn.execute("SELECT datetime('now')")
        now_str = cur_now.fetchone()[0]
        end_window = min(window_end_dt.isoformat(), now_str + "+00:00")
        path = _reconstruct_price_path(
            conn, t["token_id"],
            start=t["opened_at"],
            end=end_window,
        )
        if not path:
            sim_total += actual_pnl
            continue
        sim = _simulate_conviction_locked_exit(
            entry_price=float(t["entry_price"]),
            opened_at=t["opened_at"],
            params=locked,
            price_path=path,
        )
        sim_pnl = TRADE_SIZE_USD * sim["pnl_pct"] / 100.0
        sim_total += sim_pnl
        deltas.append({
            "id": t["id"], "token_id": t["token_id"], "stack": n,
            "actual_pnl": actual_pnl, "sim_pnl": sim_pnl,
            "delta": sim_pnl - actual_pnl,
            "actual_reason": t["exit_reason"], "sim_reason": sim["exit_reason"],
            "actual_peak": t["peak_pct"], "sim_peak": sim["peak_pct"],
            "sim_hold_hours": sim["hold_hours"],
        })

    delta_total = sim_total - actual_total
    lift_pct = 100 * delta_total / abs(actual_total) if actual_total else 0.0
    print(f"Closed trades in window:        {len(trades)}")
    print(f"Locked (stack >= 2):            {locked_count}")
    print(f"Actual aggregate PnL:           ${actual_total:>10.2f}")
    print(f"Simulated aggregate PnL:        ${sim_total:>10.2f}")
    print(f"Delta (sim − actual):           ${delta_total:>+10.2f}")
    print(f"Lift vs actual:                 {lift_pct:>+6.1f}%")
    print(f"Decision gate (>= +10% lift):   {'PASS' if lift_pct >= 10 else 'FAIL'}")
    if deltas:
        print()
        print("Top 10 simulated lifts:")
        deltas.sort(key=lambda d: -d["delta"])
        for d in deltas[:10]:
            print(f"  trade #{d['id']:<5} {d['token_id']:<22} stack={d['stack']} "
                  f"actual={d['actual_pnl']:>+8.2f} sim={d['sim_pnl']:>+8.2f} "
                  f"Δ=${d['delta']:>+7.2f}")
    return {
        "actual_total": actual_total, "sim_total": sim_total,
        "delta_total": delta_total, "lift_pct": lift_pct,
        "locked_count": locked_count, "deltas": deltas,
    }
```

- [ ] **Step 3: Wire `__main__` to call both sections**

```python
if __name__ == "__main__":
    conn = _conn()
    results: dict = {}
    results.update(section_a(conn, days=30))
    results.update({"section_b": section_b(conn, days=30)})
    # Persist machine-parseable JSON for the findings doc
    out_path = Path("tasks/findings_bl067_backtest_conviction_lock.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump(results, f, indent=2, default=str)
    print()
    print(f"Machine-parseable JSON written to {out_path}")
```

- [ ] **Step 4: Smoke-test against in-memory DB (no test required for stdout)**

Run: `SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_backtest_conviction_lock.py -v` — Expected: PASS for all helper tests.

- [ ] **Step 5: Commit**

```bash
git add scripts/backtest_conviction_lock.py tests/test_backtest_conviction_lock.py
git commit -m "feat(BL-067 backtest): Section A stack histogram + Section B simulation"
```

---

### Task 6: Section C (BIO + LAB case studies) + Section D (cohort survey)

**Files:**
- Modify: `scripts/backtest_conviction_lock.py`

- [ ] **Step 1: Add Section C — BIO + LAB replay**

```python
def section_c(conn: sqlite3.Connection) -> dict:
    """Section C: BIO + LAB case studies — operator-flagged. List all paper
    trades + simulated lift per trade for the two known multi-signal tokens."""
    _h("SECTION C — BIO + LAB case studies")
    case_studies: dict[str, list[dict]] = {}
    for token_id in ("bio-protocol", "lab"):
        cur = conn.execute(
            """SELECT id, signal_type, status, entry_price, exit_price,
                      pnl_usd, pnl_pct, peak_pct, opened_at, closed_at,
                      exit_reason
               FROM paper_trades
               WHERE token_id = ? AND status LIKE 'closed_%'
               ORDER BY opened_at""",
            (token_id,),
        )
        trades = cur.fetchall()
        print()
        print(f"-- {token_id} ({len(trades)} closed paper trades) --")
        rows: list[dict] = []
        for t in trades:
            n, _ = _count_stacked_signals_in_window(
                conn, t["token_id"], t["opened_at"], t["closed_at"],
            )
            base_params = {
                "max_duration_hours": 168.0,
                "trail_pct": 20.0,
                "sl_pct": 25.0,
            }
            locked = conviction_locked_params(stack=max(n, 1), base=base_params)
            from datetime import timedelta
            end_window_dt = _parse_iso(t["opened_at"]) + timedelta(
                hours=locked["max_duration_hours"],
            )
            now_cur = conn.execute("SELECT datetime('now')")
            now_str = now_cur.fetchone()[0]
            end_window = min(
                end_window_dt.isoformat(), now_str + "+00:00",
            )
            path = _reconstruct_price_path(
                conn, token_id,
                start=t["opened_at"], end=end_window,
            )
            sim = _simulate_conviction_locked_exit(
                entry_price=float(t["entry_price"]),
                opened_at=t["opened_at"],
                params=locked,
                price_path=path,
            )
            sim_pnl = TRADE_SIZE_USD * sim["pnl_pct"] / 100.0
            print(
                f"  #{t['id']:<5} {t['signal_type']:<22} "
                f"actual {t['pnl_pct']:>+7.2f}% (${t['pnl_usd'] or 0:>+8.2f}) | "
                f"stack={n} sim {sim['pnl_pct']:>+7.2f}% "
                f"(${sim_pnl:>+8.2f}) {sim['exit_reason']}"
            )
            rows.append({
                "id": t["id"], "signal_type": t["signal_type"],
                "actual_pnl_usd": t["pnl_usd"], "actual_pnl_pct": t["pnl_pct"],
                "stack": n, "sim_pnl_usd": sim_pnl, "sim_pnl_pct": sim["pnl_pct"],
                "sim_exit_reason": sim["exit_reason"], "sim_hold_hours": sim["hold_hours"],
            })
        case_studies[token_id] = rows
    return {"section_c": case_studies}
```

- [ ] **Step 2: Add Section D — cohort survey**

```python
def section_d(conn: sqlite3.Connection, *, days: int = 30) -> dict:
    """Section D: how many DISTINCT tokens hit N>=3 stacked signals over
    a 7d rolling window in the last `days` days? Decision input: 1 token
    = poor ROI; >10 tokens = strong case."""
    _h(f"SECTION D — BIO-like cohort survey (last {days}d, 7d windows)")
    # Get all distinct tokens that appeared in any signal source in window
    cur = conn.execute(
        f"""SELECT DISTINCT coin_id FROM gainers_snapshots
            WHERE datetime(snapshot_at) >= datetime('now','-{days} days')
            UNION
            SELECT DISTINCT coin_id FROM volume_spikes
            WHERE datetime(detected_at) >= datetime('now','-{days} days')
            UNION
            SELECT DISTINCT coin_id FROM losers_snapshots
            WHERE datetime(snapshot_at) >= datetime('now','-{days} days')
            UNION
            SELECT DISTINCT coin_id FROM trending_snapshots
            WHERE datetime(snapshot_at) >= datetime('now','-{days} days')""",
    )
    candidates = [r[0] for r in cur.fetchall() if r[0]]
    print(f"Distinct tokens seen in any signal source: {len(candidates)}")
    # For each, scan their full window and count distinct sources at peak
    cohort_n3: list[tuple[str, int]] = []
    cohort_n5: list[tuple[str, int]] = []
    for token_id in candidates:
        # Use entire `days` window — not a 7d rolling — to keep this O(N).
        # Backlog spec says 7d window; v1 approximation is full window.
        # If the cohort is very small, refine later.
        cur2 = conn.execute(
            f"SELECT datetime('now','-{days} days')",
        )
        start = cur2.fetchone()[0]
        cur3 = conn.execute("SELECT datetime('now')")
        now = cur3.fetchone()[0]
        n, _ = _count_stacked_signals_in_window(conn, token_id, start, now)
        if n >= 3:
            cohort_n3.append((token_id, n))
        if n >= 5:
            cohort_n5.append((token_id, n))
    print(f"Tokens with N>=3 stacked signals (full {days}d window): {len(cohort_n3)}")
    print(f"Tokens with N>=5 stacked signals (full {days}d window): {len(cohort_n5)}")
    if cohort_n3:
        cohort_n3.sort(key=lambda x: -x[1])
        print()
        print("Top 20 most-stacked tokens:")
        for tok, n in cohort_n3[:20]:
            print(f"  {tok:<28} stack={n}")
    return {
        "section_d": {
            "candidates_count": len(candidates),
            "n3_count": len(cohort_n3), "n5_count": len(cohort_n5),
            "top_n3": cohort_n3[:20],
        }
    }
```

- [ ] **Step 3: Update `__main__` to call all 4 sections**

```python
if __name__ == "__main__":
    conn = _conn()
    results: dict = {}
    results.update(section_a(conn, days=30))
    results.update({"section_b": section_b(conn, days=30)})
    results.update(section_c(conn))
    results.update(section_d(conn, days=30))
    out_path = Path("tasks/findings_bl067_backtest_conviction_lock.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump(results, f, indent=2, default=str)
    print()
    print(f"Machine-parseable JSON written to {out_path}")
```

- [ ] **Step 4: Commit**

```bash
git add scripts/backtest_conviction_lock.py
git commit -m "feat(BL-067 backtest): Section C BIO+LAB + Section D cohort"
```

---

### Task 7: Final regression sweep + push

- [ ] **Step 1: Run BL-067 backtest test file**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_backtest_conviction_lock.py -v
```

Expected: all helper unit tests PASS.

- [ ] **Step 2: Targeted regression on adjacent tests**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_db.py tests/test_dashboard_tg_social_extensions.py tests/test_bl065_cashtag_dispatch.py -q --tb=short
```

Expected: all PASS (no regression — script is read-only against scout.db).

- [ ] **Step 3: Push branch**

```bash
git push origin feat/bl-067-backtest-conviction-lock
```

---

## Run + findings doc (Task #107 — separate from build)

After PR squash-merge to master:

1. SSH-pull a fresh `scout.db` snapshot (avoid running on production read-write)
2. Run `cd /root/gecko-alpha && uv run python scripts/backtest_conviction_lock.py | tee /tmp/bl067_run.txt`
3. Copy `/tmp/bl067_run.txt` + `/root/gecko-alpha/tasks/findings_bl067_backtest_conviction_lock.json` back locally
4. Synthesize into `tasks/findings_bl067_backtest_conviction_lock.md` with:
   - **§1** Sections A-D output (verbatim)
   - **§2** Decision: PASS (≥+10% lift) or FAIL — go/no-go for production conviction-lock
   - **§3** BIO + LAB case-study summary (which trades simulate as kept vs exited)
   - **§4** Cohort size: 1 token → poor ROI; >10 → strong case
   - **§5** Open design questions resolved by data (per backlog.md:382-394 — lookback window, per-signal opt-in, etc.)
5. Present findings + recommendation to operator

---

## Self-Review

1. **Spec coverage:**
   - Backlog.md:367 spec — Tasks 1-6 ✓
   - Backlog.md:374-380 param table → Task 2 ✓
   - Backlog.md:395-399 required research deliverables (backtest replay, BIO-like cohort survey, edge cases) → Tasks 5-6 ✓
   - Backlog.md:412 resume protocol (script first, no production code) → respected ✓
2. **Placeholder scan:** none — every step has either exact code or exact command.
3. **Type consistency:** helper return types pinned (`tuple[int, list[str]]`, `list[tuple[str, float]]`, `dict`); param dict keys consistent across composition + simulator (`max_duration_hours`, `trail_pct`, `sl_pct`).
4. **New primitives marker:** present at top with all helpers + script + tests + findings doc; no DB schema changes.
5. **Hermes-first marker:** present + 5 domain checks + verdict.
6. **Drift grounding:** explicit file:line refs to existing backtest pattern, BL-067 spec, evaluator logic, snapshot tables.
7. **TDD discipline:** every helper has failing-test → impl → passing-test → commit.
8. **No production code:** verified — only `scripts/` + `tests/` + `tasks/findings_*.json` (run output). NO `scout/trading/conviction.py`, NO Database method changes, NO Settings changes.
9. **Honest scope:**
   - **NOT in scope:** ladder-exit + moonshot-trail + peak-fade simulation (production evaluator has these; v1 simulator skips them — adds 200+ lines without changing the BL-067 question of "does extending hold help?"). Documented in `_simulate_conviction_locked_exit` docstring.
   - **NOT in scope:** per-signal-type opt-in (BL-067 design question #2). Defer to implementation phase IF backtest greenlights.
   - **NOT in scope:** PR #6 (Multi-Signal Conviction Chains) reconciliation. Defer.
   - **NOT in scope:** simulating tg_social_signals as a stack source (BL-067 design question #8) — `_count_stacked_signals_in_window` already counts it.
10. **Decision gate:** ≥10% PnL lift on aggregate net = greenlight. < 10% = report findings + close BL-067 as won't-fix on this axis.
