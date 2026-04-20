# BL-054 Perp WebSocket Anomaly Detector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Full design rationale lives in `docs/superpowers/specs/2026-04-20-bl054-perp-ws-anomaly-detector-design.md` — reviewers and implementers should read it for context.

**Goal:** Ship a research-only, default-off Binance/Bybit perpetual-futures WebSocket anomaly detector that tags pipeline candidates with funding-flip / OI-spike signals. Zero production scoring impact until a subsequent recalibration PR lands.

**Architecture:** Long-lived WS watcher (one supervisor, two exchange-specific inner tasks) feeds a bounded `asyncio.Queue` consumed by a single classifier coroutine that updates an in-memory EWMA `BaselineStore` and persists anomalies via batched SQLite writes. Candidate-token enrichment is a DB read per pipeline cycle. Scorer Signal 14 is gated by BOTH `PERP_SCORING_ENABLED=true` AND a runtime constant `SCORER_MAX_RAW >= 203` (tamper-resistant — the flag alone cannot affect scoring output).

**Tech Stack:** Python 3.11+, asyncio, `aiohttp.ClientSession.ws_connect` (no new dependencies), `aiosqlite` (WAL already enabled), Pydantic v2 with `field_validator`, structlog, pytest-asyncio auto mode. TDD discipline: red → green → refactor → commit per step.

**Zero-regression contract:** full test suite MUST continue to pass throughout (baseline 881 passed, 1 skipped) plus new BL-054 tests.

---

## File Structure

- Create `scout/perp/__init__.py`
- Create `scout/perp/schemas.py` — Pydantic models + `Exchange` / `AnomalyKind` literals
- Create `scout/perp/normalize.py` — ticker normalization pure helpers
- Create `scout/perp/baseline.py` — LRU-capped EWMA baseline store
- Create `scout/perp/anomaly.py` — pure classifier functions
- Create `scout/perp/binance.py` — Binance WS client (pong-on-ping)
- Create `scout/perp/bybit.py` — Bybit WS client (op:ping)
- Create `scout/perp/enrichment.py` — `enrich_candidates_with_perp_anomalies`
- Create `scout/perp/watcher.py` — supervisor + circuit-breaker + batched flush + heartbeat shadow stats
- Modify `scout/config.py` — add PERP_* settings block
- Modify `scout/models.py` — add four `perp_*` candidate fields + `Exchange` import
- Modify `scout/db.py` — `perp_anomalies` table + CRUD + batch insert + prune
- Modify `scout/scorer.py` — Signal 14 + denominator-ready runtime guard
- Modify `scout/main.py` — background task launch (wrapped in `PERP_ENABLED` gate) + Stage 2.5 enrichment call + hourly prune
- Create `tests/test_perp_schemas.py`
- Create `tests/test_perp_normalize.py`
- Create `tests/test_perp_baseline.py`
- Create `tests/test_perp_anomaly.py`
- Create `tests/test_perp_binance.py`
- Create `tests/test_perp_bybit.py`
- Create `tests/test_perp_db.py`
- Create `tests/test_perp_enrichment.py`
- Create `tests/test_perp_watcher.py`
- Create `tests/test_perp_scorer.py`
- Create `tests/test_main_perp_integration.py`
- Create `tests/test_perp_flag_off_snapshot.py`
- Create `tests/fixtures/perp/binance_markprice.json`
- Create `tests/fixtures/perp/binance_openinterest.json`
- Create `tests/fixtures/perp/bybit_ticker.json`

All tests MUST run with `uv run pytest -q` and pass under the existing asyncio-auto mode configured in `pyproject.toml`.

---

### Task 1: Config knobs + schemas + ticker normalizer

**Files:**
- Modify: `scout/config.py` (append new PERP_* block + add comma-parse validator for `PERP_SYMBOLS`)
- Create: `scout/perp/__init__.py`
- Create: `scout/perp/schemas.py`
- Create: `scout/perp/normalize.py`
- Create: `tests/test_perp_schemas.py`
- Create: `tests/test_perp_normalize.py`

- [ ] **Step 1: Write failing test for `Exchange` + `AnomalyKind` literals and `PerpTick` / `PerpAnomaly` Pydantic models**

```python
# tests/test_perp_schemas.py
import pytest
from datetime import datetime, timezone
from pydantic import ValidationError
from scout.perp.schemas import PerpTick, PerpAnomaly, Exchange, AnomalyKind

def test_perp_tick_requires_upper_ticker_charset():
    with pytest.raises(ValidationError):
        PerpTick(
            exchange="binance", symbol="BTCUSDT", ticker="btc-lower",
            timestamp=datetime.now(timezone.utc),
        )

def test_perp_tick_rejects_oversized_ticker():
    with pytest.raises(ValidationError):
        PerpTick(
            exchange="binance", symbol="BTCUSDT", ticker="A" * 21,
            timestamp=datetime.now(timezone.utc),
        )

def test_perp_tick_rejects_oversized_symbol():
    with pytest.raises(ValidationError):
        PerpTick(
            exchange="binance", symbol="X" * 33, ticker="BTC",
            timestamp=datetime.now(timezone.utc),
        )

def test_perp_tick_happy_path():
    t = PerpTick(
        exchange="binance", symbol="BTCUSDT", ticker="BTC",
        funding_rate=0.0001, mark_price=50000.0, open_interest=12345.0,
        timestamp=datetime.now(timezone.utc),
    )
    assert t.exchange == "binance"
    assert t.ticker == "BTC"

def test_perp_anomaly_happy_path():
    a = PerpAnomaly(
        exchange="bybit", symbol="DOGEUSDT", ticker="DOGE",
        kind="oi_spike", magnitude=4.2, baseline=1.0,
        observed_at=datetime.now(timezone.utc),
    )
    assert a.kind == "oi_spike"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_perp_schemas.py -q`
Expected: ImportError on `scout.perp.schemas`.

- [ ] **Step 3: Write `scout/perp/__init__.py` (empty) and `scout/perp/schemas.py`**

```python
# scout/perp/__init__.py
"""Binance/Bybit perp WebSocket anomaly detector (BL-054)."""
```

```python
# scout/perp/schemas.py
"""Pydantic models for perp WebSocket ticks and anomaly events."""

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, field_validator

AnomalyKind = Literal["funding_flip", "oi_spike"]
Exchange = Literal["binance", "bybit"]

_TICKER_RE = re.compile(r"^[A-Z0-9]{1,20}$")


class PerpTick(BaseModel):
    exchange: Exchange
    symbol: str
    ticker: str
    funding_rate: float | None = None
    mark_price: float | None = None
    open_interest: float | None = None
    open_interest_usd: float | None = None
    timestamp: datetime

    @field_validator("ticker")
    @classmethod
    def _ticker_charset(cls, v: str) -> str:
        if not _TICKER_RE.match(v):
            raise ValueError(f"invalid ticker: {v!r}")
        return v

    @field_validator("symbol")
    @classmethod
    def _symbol_len(cls, v: str) -> str:
        if not (1 <= len(v) <= 32):
            raise ValueError("symbol length out of bounds")
        return v


class PerpAnomaly(BaseModel):
    exchange: Exchange
    symbol: str
    ticker: str
    kind: AnomalyKind
    magnitude: float
    baseline: float | None = None
    observed_at: datetime
```

- [ ] **Step 4: Run schemas test to verify pass**

Run: `uv run pytest tests/test_perp_schemas.py -q`
Expected: 5 passed.

- [ ] **Step 5: Write failing ticker-normalization tests**

```python
# tests/test_perp_normalize.py
import pytest
from scout.perp.normalize import normalize_ticker

@pytest.mark.parametrize("raw,expected", [
    ("BTCUSDT",     "BTC"),
    ("BTCUSDC",     "BTC"),
    ("1000PEPEUSDT","PEPE"),
    ("DOGEUSD",     "DOGE"),      # inverse collapse
    ("ETH-PERP",    "ETH"),
    ("SOLBUSD",     "SOL"),
    ("btcusdt",     "BTC"),        # upper-casing
])
def test_normalize_ticker_happy(raw, expected):
    assert normalize_ticker(raw) == expected

@pytest.mark.parametrize("raw", [
    "../etc/passwd",
    "SYMBOL WITH SPACE",
    "A" * 33,
    "",
    "USDT",        # after strip => empty
    "1000USDT",    # strip 1000 + USDT => empty
])
def test_normalize_ticker_drops_malformed(raw):
    assert normalize_ticker(raw) is None
```

- [ ] **Step 6: Run normalize test to verify it fails**

Run: `uv run pytest tests/test_perp_normalize.py -q`
Expected: ImportError on `scout.perp.normalize`.

- [ ] **Step 7: Write `scout/perp/normalize.py`**

```python
# scout/perp/normalize.py
"""Ticker normalization from exchange-native symbol to base-asset ticker."""

import re

_SUFFIXES = ("USDT", "USDC", "BUSD", "USD", "-PERP")
_VALID = re.compile(r"^[A-Z0-9]{1,20}$")


def normalize_ticker(symbol: str) -> str | None:
    """Return normalized base-asset ticker, or None if input is malformed.

    Rules:
      * Upper-case.
      * Strip one trailing suffix from {USDT, USDC, BUSD, USD, -PERP}.
      * Strip leading "1000" (Binance cosmetic multiplier convention).
      * Validate against ``^[A-Z0-9]{1,20}$``.
    """
    if not isinstance(symbol, str):
        return None
    up = symbol.upper()
    for suffix in _SUFFIXES:
        if up.endswith(suffix) and len(up) > len(suffix):
            up = up[: -len(suffix)]
            break
    if up.startswith("1000") and len(up) > 4:
        up = up[4:]
    if not _VALID.match(up):
        return None
    return up
```

- [ ] **Step 8: Run normalize test to verify pass**

Run: `uv run pytest tests/test_perp_normalize.py -q`
Expected: 13 passed.

- [ ] **Step 9: Append PERP_* settings to `scout/config.py`**

Append immediately after the existing `-------- Paper Trading Engine --------` / feedback-loop block (i.e., before `@field_validator("PAPER_SL_PCT")`):

```python
    # -------- Perp WebSocket Anomaly Detector (BL-054) --------
    # Research-only, default-off. PERP_ENABLED gates data collection;
    # PERP_SCORING_ENABLED gates scorer signal separately. Flipping
    # PERP_SCORING_ENABLED alone does NOT affect scoring -- the scorer
    # also requires SCORER_MAX_RAW >= 203 (runtime guard in scorer.py),
    # which ships as 183 in this PR. Full design in
    # docs/superpowers/specs/2026-04-20-bl054-perp-ws-anomaly-detector-design.md.
    PERP_ENABLED: bool = False
    PERP_SCORING_ENABLED: bool = False
    PERP_BINANCE_ENABLED: bool = True
    PERP_BYBIT_ENABLED: bool = True
    PERP_SYMBOLS: list[str] = []
    PERP_FUNDING_FLIP_MIN_PCT: float = 0.05
    PERP_OI_SPIKE_RATIO: float = 3.0
    PERP_BASELINE_ALPHA: float = 0.1
    PERP_BASELINE_MIN_SAMPLES: int = 30
    PERP_BASELINE_MAX_KEYS: int = 1000
    PERP_BASELINE_IDLE_EVICT_SEC: int = 3600
    PERP_ANOMALY_LOOKBACK_MIN: int = 15
    PERP_ANOMALY_DEDUP_MIN: int = 5
    PERP_ANOMALY_RETENTION_DAYS: int = 7
    PERP_MAX_CONSECUTIVE_RESTARTS: int = 5
    PERP_CIRCUIT_BREAK_SEC: int = 3600
    PERP_WS_PING_INTERVAL_SEC: int = 20
    PERP_WS_RECONNECT_MAX_SEC: int = 60
    PERP_QUEUE_MAXSIZE: int = 2048
    PERP_DB_FLUSH_INTERVAL_SEC: float = 2.0
    PERP_DB_FLUSH_MAX_ROWS: int = 100
```

Add a comma-parsing validator, mirroring the `CHAINS` pattern:

```python
    @field_validator("PERP_SYMBOLS", mode="before")
    @classmethod
    def parse_perp_symbols(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [s.strip().upper() for s in v.split(",") if s.strip()]
        return v
```

- [ ] **Step 10: Run config + new tests to verify no regression**

Run: `uv run pytest tests/test_config.py tests/test_perp_schemas.py tests/test_perp_normalize.py -q`
Expected: all tests pass, PERP settings load with defaults.

- [ ] **Step 11: Commit**

```bash
git add scout/perp/ scout/config.py tests/test_perp_schemas.py tests/test_perp_normalize.py
git commit -m "feat(bl-054): schemas + normalizer + config knobs

Pydantic PerpTick/PerpAnomaly with charset-validated ticker,
flat ticker normalizer (USDT/USDC/USD/BUSD/-PERP + 1000 prefix),
default-off config block for the perp detector."
```

---

### Task 2: Baseline store + pure classifier functions

**Files:**
- Create: `scout/perp/baseline.py`
- Create: `scout/perp/anomaly.py`
- Create: `tests/test_perp_baseline.py`
- Create: `tests/test_perp_anomaly.py`

- [ ] **Step 1: Write failing tests for `BaselineStore`**

```python
# tests/test_perp_baseline.py
from datetime import datetime, timezone, timedelta
from scout.perp.baseline import BaselineStore

def _key(sym: str) -> tuple[str, str]:
    return ("binance", sym)

def test_baseline_ewma_cold_start():
    s = BaselineStore(alpha=0.5, max_keys=100, idle_evict_seconds=3600)
    k = _key("BTCUSDT")
    s.update(k, oi=100.0, funding=0.0001, now=datetime.now(timezone.utc))
    assert s.oi_baseline(k) == 100.0
    assert s.sample_count(k) == 1

def test_baseline_ewma_convergence():
    s = BaselineStore(alpha=0.5, max_keys=100, idle_evict_seconds=3600)
    k = _key("BTCUSDT")
    now = datetime.now(timezone.utc)
    for v in (10.0, 20.0, 30.0, 40.0):
        s.update(k, oi=v, funding=0.0, now=now)
    # With alpha=0.5: 10 -> 10, 15, 22.5, 31.25
    assert abs(s.oi_baseline(k) - 31.25) < 1e-6
    assert s.sample_count(k) == 4

def test_baseline_lru_evicts_oldest_touched():
    s = BaselineStore(alpha=0.1, max_keys=2, idle_evict_seconds=3600)
    now = datetime.now(timezone.utc)
    s.update(_key("A"), oi=1.0, funding=0.0, now=now)
    s.update(_key("B"), oi=2.0, funding=0.0, now=now + timedelta(seconds=1))
    s.update(_key("A"), oi=1.5, funding=0.0, now=now + timedelta(seconds=2))  # A touched last
    s.update(_key("C"), oi=3.0, funding=0.0, now=now + timedelta(seconds=3))  # should evict B
    assert s.oi_baseline(_key("A")) is not None
    assert s.oi_baseline(_key("B")) is None
    assert s.oi_baseline(_key("C")) is not None

def test_baseline_idle_evict():
    s = BaselineStore(alpha=0.1, max_keys=100, idle_evict_seconds=60)
    t0 = datetime.now(timezone.utc)
    s.update(_key("STALE"), oi=1.0, funding=0.0, now=t0)
    s.update(_key("FRESH"), oi=2.0, funding=0.0, now=t0 + timedelta(seconds=30))
    evicted = s.evict_idle(now=t0 + timedelta(seconds=120))
    assert evicted == 1
    assert s.oi_baseline(_key("STALE")) is None
    assert s.oi_baseline(_key("FRESH")) is not None

def test_baseline_ignores_none_inputs():
    s = BaselineStore(alpha=0.5, max_keys=10, idle_evict_seconds=3600)
    k = _key("X")
    s.update(k, oi=None, funding=None, now=datetime.now(timezone.utc))
    assert s.oi_baseline(k) is None
    assert s.funding_baseline(k) is None
    assert s.sample_count(k) == 0
```

- [ ] **Step 2: Run to verify fails**

Run: `uv run pytest tests/test_perp_baseline.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement `scout/perp/baseline.py`**

```python
# scout/perp/baseline.py
"""Per-(exchange,symbol) EWMA baseline store with LRU + idle eviction."""

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime


@dataclass
class _Entry:
    oi_ewma: float | None
    funding_ewma: float | None
    sample_count: int
    last_seen: datetime


class BaselineStore:
    """In-memory EWMA baseline state keyed by (exchange, symbol).

    Bounded: ``max_keys`` LRU cap on insert, plus an opt-in ``evict_idle``
    pass that drops keys untouched for ``idle_evict_seconds``. Both
    paths are intentionally lightweight -- no background threads.
    """

    def __init__(
        self,
        *,
        alpha: float = 0.1,
        max_keys: int = 1000,
        idle_evict_seconds: int = 3600,
    ):
        if not 0 < alpha <= 1:
            raise ValueError("alpha must be in (0, 1]")
        self._alpha = alpha
        self._max_keys = max_keys
        self._idle = idle_evict_seconds
        self._entries: "OrderedDict[tuple[str, str], _Entry]" = OrderedDict()

    def update(
        self,
        key: tuple[str, str],
        *,
        oi: float | None,
        funding: float | None,
        now: datetime,
    ) -> None:
        if oi is None and funding is None:
            return
        entry = self._entries.get(key)
        if entry is None:
            if len(self._entries) >= self._max_keys:
                self._entries.popitem(last=False)
            entry = _Entry(oi, funding, 1, now)
            self._entries[key] = entry
            return
        if oi is not None:
            entry.oi_ewma = (
                oi if entry.oi_ewma is None
                else self._alpha * oi + (1 - self._alpha) * entry.oi_ewma
            )
        if funding is not None:
            entry.funding_ewma = (
                funding if entry.funding_ewma is None
                else self._alpha * funding + (1 - self._alpha) * entry.funding_ewma
            )
        entry.sample_count += 1
        entry.last_seen = now
        self._entries.move_to_end(key)

    def oi_baseline(self, key: tuple[str, str]) -> float | None:
        entry = self._entries.get(key)
        return None if entry is None else entry.oi_ewma

    def funding_baseline(self, key: tuple[str, str]) -> float | None:
        entry = self._entries.get(key)
        return None if entry is None else entry.funding_ewma

    def sample_count(self, key: tuple[str, str]) -> int:
        entry = self._entries.get(key)
        return 0 if entry is None else entry.sample_count

    def evict_idle(self, *, now: datetime) -> int:
        cutoff = now.timestamp() - self._idle
        victims = [
            k for k, e in self._entries.items()
            if e.last_seen.timestamp() < cutoff
        ]
        for k in victims:
            self._entries.pop(k, None)
        return len(victims)

    def __len__(self) -> int:
        return len(self._entries)
```

- [ ] **Step 4: Run baseline tests to verify pass**

Run: `uv run pytest tests/test_perp_baseline.py -q`
Expected: 5 passed.

- [ ] **Step 5: Write failing classifier tests**

```python
# tests/test_perp_anomaly.py
from datetime import datetime, timezone
from scout.perp.anomaly import classify_funding_flip, classify_oi_spike

NOW = datetime.now(timezone.utc)

def test_funding_flip_positive_to_negative():
    a = classify_funding_flip(
        prev_rate=0.0002, new_rate=-0.0001,
        exchange="binance", symbol="BTCUSDT", ticker="BTC",
        observed_at=NOW, min_magnitude_pct=0.01,
    )
    assert a is not None and a.kind == "funding_flip"

def test_funding_flip_below_magnitude_gate():
    assert classify_funding_flip(
        prev_rate=0.00009, new_rate=-0.00001,
        exchange="binance", symbol="BTCUSDT", ticker="BTC",
        observed_at=NOW, min_magnitude_pct=0.05,
    ) is None

def test_funding_flip_same_sign():
    assert classify_funding_flip(
        prev_rate=0.0001, new_rate=0.0002,
        exchange="binance", symbol="BTCUSDT", ticker="BTC",
        observed_at=NOW, min_magnitude_pct=0.01,
    ) is None

def test_funding_flip_no_prev():
    assert classify_funding_flip(
        prev_rate=None, new_rate=0.0001,
        exchange="binance", symbol="BTCUSDT", ticker="BTC",
        observed_at=NOW, min_magnitude_pct=0.01,
    ) is None

def test_oi_spike_triggered():
    a = classify_oi_spike(
        current_oi=400.0, baseline_oi=100.0,
        exchange="binance", symbol="BTCUSDT", ticker="BTC",
        observed_at=NOW, sample_count=40, min_samples=30, spike_ratio=3.0,
    )
    assert a is not None and a.magnitude == 4.0

def test_oi_spike_cold_warmup_gate():
    assert classify_oi_spike(
        current_oi=400.0, baseline_oi=100.0,
        exchange="binance", symbol="BTCUSDT", ticker="BTC",
        observed_at=NOW, sample_count=5, min_samples=30, spike_ratio=3.0,
    ) is None

def test_oi_spike_below_ratio():
    assert classify_oi_spike(
        current_oi=200.0, baseline_oi=100.0,
        exchange="binance", symbol="BTCUSDT", ticker="BTC",
        observed_at=NOW, sample_count=40, min_samples=30, spike_ratio=3.0,
    ) is None

def test_oi_spike_no_baseline():
    assert classify_oi_spike(
        current_oi=400.0, baseline_oi=None,
        exchange="binance", symbol="BTCUSDT", ticker="BTC",
        observed_at=NOW, sample_count=40, min_samples=30, spike_ratio=3.0,
    ) is None
```

- [ ] **Step 6: Run to verify fails**

Run: `uv run pytest tests/test_perp_anomaly.py -q`
Expected: ImportError.

- [ ] **Step 7: Implement `scout/perp/anomaly.py`**

```python
# scout/perp/anomaly.py
"""Pure classifier functions: funding flip + OI spike. No I/O."""

from datetime import datetime

from scout.perp.schemas import Exchange, PerpAnomaly


def classify_funding_flip(
    *,
    prev_rate: float | None,
    new_rate: float,
    exchange: Exchange,
    symbol: str,
    ticker: str,
    observed_at: datetime,
    min_magnitude_pct: float,
) -> PerpAnomaly | None:
    """Fire when funding rate flips sign and |new_rate| >= threshold."""
    if prev_rate is None:
        return None
    if (prev_rate >= 0) == (new_rate >= 0):
        return None
    magnitude = abs(new_rate) * 100.0  # rate is fractional; convert to pct
    if magnitude < min_magnitude_pct:
        return None
    return PerpAnomaly(
        exchange=exchange, symbol=symbol, ticker=ticker,
        kind="funding_flip", magnitude=magnitude, baseline=prev_rate,
        observed_at=observed_at,
    )


def classify_oi_spike(
    *,
    current_oi: float,
    baseline_oi: float | None,
    exchange: Exchange,
    symbol: str,
    ticker: str,
    observed_at: datetime,
    sample_count: int,
    min_samples: int,
    spike_ratio: float,
) -> PerpAnomaly | None:
    """Fire when current OI / baseline >= spike_ratio past warmup."""
    if baseline_oi is None or baseline_oi <= 0:
        return None
    if sample_count < min_samples:
        return None
    ratio = current_oi / baseline_oi
    if ratio < spike_ratio:
        return None
    return PerpAnomaly(
        exchange=exchange, symbol=symbol, ticker=ticker,
        kind="oi_spike", magnitude=ratio, baseline=baseline_oi,
        observed_at=observed_at,
    )
```

- [ ] **Step 8: Run all new tests**

Run: `uv run pytest tests/test_perp_baseline.py tests/test_perp_anomaly.py -q`
Expected: 13 passed.

- [ ] **Step 9: Commit**

```bash
git add scout/perp/baseline.py scout/perp/anomaly.py tests/test_perp_baseline.py tests/test_perp_anomaly.py
git commit -m "feat(bl-054): baseline store + pure classifier

BaselineStore EWMA with LRU cap + idle-eviction contract.
Pure classifier functions for funding-flip and OI-spike with
warmup + magnitude gates."
```

---

### Task 3: Binance WS client

**Files:**
- Create: `scout/perp/binance.py`
- Create: `tests/test_perp_binance.py`
- Create: `tests/fixtures/perp/binance_markprice.json`
- Create: `tests/fixtures/perp/binance_openinterest.json`

**Fixtures (real Binance payload shapes, pared down):**

```json
// tests/fixtures/perp/binance_markprice.json
{
  "stream": "!markPrice@arr@1s",
  "data": [
    {"e":"markPriceUpdate","E":1713600000000,"s":"BTCUSDT","p":"50000.0","r":"0.00010000","T":1713628800000},
    {"e":"markPriceUpdate","E":1713600000000,"s":"1000PEPEUSDT","p":"0.000012","r":"-0.00030000","T":1713628800000}
  ]
}
```

```json
// tests/fixtures/perp/binance_openinterest.json
{
  "stream": "btcusdt@openInterest",
  "data": {"e":"openInterest","E":1713600000000,"s":"BTCUSDT","o":"123456.789"}
}
```

- [ ] **Step 1: Write failing parser test**

```python
# tests/test_perp_binance.py
import json
import pytest
from pathlib import Path
from scout.perp.binance import parse_frame

FIXTURES = Path(__file__).parent / "fixtures" / "perp"

def test_parse_markprice_array_yields_two_ticks():
    raw = json.loads((FIXTURES / "binance_markprice.json").read_text())
    ticks = list(parse_frame(raw))
    assert len(ticks) == 2
    btc, pepe = ticks
    assert btc.ticker == "BTC"
    assert btc.mark_price == 50000.0
    assert btc.funding_rate == 0.0001
    assert pepe.ticker == "PEPE"
    assert pepe.funding_rate == -0.0003

def test_parse_openinterest_yields_one_tick():
    raw = json.loads((FIXTURES / "binance_openinterest.json").read_text())
    ticks = list(parse_frame(raw))
    assert len(ticks) == 1
    assert ticks[0].open_interest == 123456.789
    assert ticks[0].ticker == "BTC"

def test_parse_frame_drops_malformed():
    assert list(parse_frame({"garbage": True})) == []
    assert list(parse_frame({"stream": "unknown", "data": {}})) == []
```

- [ ] **Step 2: Run to verify fails**

Run: `uv run pytest tests/test_perp_binance.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement Binance parser in `scout/perp/binance.py`**

```python
# scout/perp/binance.py
"""Binance futures WS client + parser for perp anomaly detector."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import aiohttp
import structlog

from scout.config import Settings
from scout.perp.normalize import normalize_ticker
from scout.perp.schemas import PerpTick

logger = structlog.get_logger()

WS_URL = "wss://fstream.binance.com/ws"


def parse_frame(frame: dict[str, Any]) -> list[PerpTick]:
    """Yield PerpTicks from a single Binance WS frame.

    Supports:
      * ``!markPrice@arr@1s`` — array of markPrice updates.
      * ``<symbol>@openInterest`` — single OI update.

    Malformed or unknown streams silently yield empty. Never raises.
    """
    ticks: list[PerpTick] = []
    stream = frame.get("stream") if isinstance(frame, dict) else None
    if stream and "markPrice@arr" in stream:
        data = frame.get("data") or []
        if isinstance(data, list):
            for item in data:
                tick = _parse_mark(item)
                if tick is not None:
                    ticks.append(tick)
    elif stream and "openInterest" in stream:
        data = frame.get("data") or {}
        if isinstance(data, dict):
            tick = _parse_oi(data)
            if tick is not None:
                ticks.append(tick)
    # OI and markPrice frames are snapshots of current value, not deltas;
    # drop-oldest in the queue is safe for both stream types.
    return ticks


def _parse_mark(item: dict[str, Any]) -> PerpTick | None:
    try:
        symbol = str(item.get("s", ""))
        ticker = normalize_ticker(symbol)
        if ticker is None:
            return None
        return PerpTick(
            exchange="binance",
            symbol=symbol,
            ticker=ticker,
            mark_price=float(item["p"]),
            funding_rate=float(item["r"]),
            timestamp=datetime.fromtimestamp(
                float(item.get("E", 0)) / 1000, tz=timezone.utc
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _parse_oi(item: dict[str, Any]) -> PerpTick | None:
    try:
        symbol = str(item.get("s", ""))
        ticker = normalize_ticker(symbol)
        if ticker is None:
            return None
        return PerpTick(
            exchange="binance",
            symbol=symbol,
            ticker=ticker,
            open_interest=float(item["o"]),
            timestamp=datetime.fromtimestamp(
                float(item.get("E", 0)) / 1000, tz=timezone.utc
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None


async def stream_ticks(
    session: aiohttp.ClientSession,
    settings: Settings,
) -> AsyncIterator[PerpTick]:
    """Connect and yield PerpTicks forever. Self-healing via full-jitter backoff.

    Binance's server sends ping frames; aiohttp auto-replies pong. No
    outbound ping needed. Reconnect with jitter + 0.5s floor to avoid
    hammering a momentarily-recovering gateway.
    """
    attempt = 0
    while True:
        try:
            async with session.ws_connect(
                WS_URL,
                heartbeat=settings.PERP_WS_PING_INTERVAL_SEC,
                max_msg_size=0,
            ) as ws:
                # Subscribe to markPrice@arr@1s and any curated @openInterest streams.
                params: list[str] = ["!markPrice@arr@1s"]
                for sym in settings.PERP_SYMBOLS:
                    params.append(f"{sym.lower()}@openInterest")
                await ws.send_json({"method": "SUBSCRIBE", "params": params, "id": 1})
                attempt = 0  # connection successful; reset backoff
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    try:
                        frame = json.loads(msg.data)
                    except (ValueError, TypeError):
                        continue
                    for tick in parse_frame(frame):
                        yield tick
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("binance_ws_disconnect", error=str(exc), attempt=attempt)
        attempt += 1
        delay = random.uniform(0.5, min(
            settings.PERP_WS_RECONNECT_MAX_SEC,
            float(2**attempt),
        ))
        await asyncio.sleep(delay)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_perp_binance.py -q`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add scout/perp/binance.py tests/test_perp_binance.py tests/fixtures/perp/binance_markprice.json tests/fixtures/perp/binance_openinterest.json
git commit -m "feat(bl-054): binance WS client with markPrice + OI parsers"
```

---

### Task 4: Bybit WS client

**Files:**
- Create: `scout/perp/bybit.py`
- Create: `tests/test_perp_bybit.py`
- Create: `tests/fixtures/perp/bybit_ticker.json`

**Fixture:**

```json
// tests/fixtures/perp/bybit_ticker.json
{
  "topic": "tickers.BTCUSDT",
  "ts": 1713600000000,
  "type": "snapshot",
  "data": {
    "symbol": "BTCUSDT",
    "markPrice": "50000.00",
    "fundingRate": "0.0001",
    "openInterest": "12345.678",
    "openInterestValue": "617283900.00"
  }
}
```

- [ ] **Step 1: Write failing parser test**

```python
# tests/test_perp_bybit.py
import json
from pathlib import Path
from scout.perp.bybit import parse_frame

FIXTURES = Path(__file__).parent / "fixtures" / "perp"

def test_parse_bybit_ticker_snapshot():
    raw = json.loads((FIXTURES / "bybit_ticker.json").read_text())
    ticks = list(parse_frame(raw))
    assert len(ticks) == 1
    t = ticks[0]
    assert t.ticker == "BTC"
    assert t.funding_rate == 0.0001
    assert t.open_interest == 12345.678
    assert t.open_interest_usd == 617283900.0
    assert t.mark_price == 50000.0

def test_parse_bybit_pong_ignored():
    assert list(parse_frame({"op": "pong"})) == []

def test_parse_bybit_garbage_dropped():
    assert list(parse_frame({"topic": "orderbook.1.BTCUSDT", "data": {}})) == []
```

- [ ] **Step 2: Run to verify fails**

Run: `uv run pytest tests/test_perp_bybit.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement Bybit client in `scout/perp/bybit.py`**

```python
# scout/perp/bybit.py
"""Bybit v5 linear-perp WS client + parser."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import aiohttp
import structlog

from scout.config import Settings
from scout.perp.normalize import normalize_ticker
from scout.perp.schemas import PerpTick

logger = structlog.get_logger()

WS_URL = "wss://stream.bybit.com/v5/public/linear"


def parse_frame(frame: dict[str, Any]) -> list[PerpTick]:
    if not isinstance(frame, dict):
        return []
    topic = frame.get("topic", "")
    if not isinstance(topic, str) or not topic.startswith("tickers."):
        return []
    data = frame.get("data") or {}
    if not isinstance(data, dict):
        return []
    symbol = str(data.get("symbol", ""))
    ticker = normalize_ticker(symbol)
    if ticker is None:
        return []
    try:
        ts_ms = float(frame.get("ts", 0))
        tick = PerpTick(
            exchange="bybit",
            symbol=symbol,
            ticker=ticker,
            mark_price=(float(data["markPrice"])
                        if "markPrice" in data else None),
            funding_rate=(float(data["fundingRate"])
                          if "fundingRate" in data else None),
            open_interest=(float(data["openInterest"])
                           if "openInterest" in data else None),
            open_interest_usd=(float(data["openInterestValue"])
                               if "openInterestValue" in data else None),
            timestamp=datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
        )
        return [tick]
    except (KeyError, TypeError, ValueError):
        return []


async def stream_ticks(
    session: aiohttp.ClientSession,
    settings: Settings,
) -> AsyncIterator[PerpTick]:
    """Connect and yield PerpTicks forever.

    Bybit REQUIRES explicit JSON {"op": "ping"} every 20s. Different from
    Binance's server-sent ping; each client owns its own keepalive.
    """
    attempt = 0
    while True:
        try:
            async with session.ws_connect(WS_URL, max_msg_size=0) as ws:
                # Subscribe to configured symbols; if empty, skip this exchange.
                symbols = settings.PERP_SYMBOLS
                if not symbols:
                    logger.info("bybit_ws_no_symbols_sleeping")
                    await asyncio.sleep(30)
                    continue
                topics = [f"tickers.{s}" for s in symbols]
                await ws.send_json({"op": "subscribe", "args": topics})
                attempt = 0
                ping_task = asyncio.create_task(_ping_loop(ws, settings))
                try:
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            frame = json.loads(msg.data)
                        except (ValueError, TypeError):
                            continue
                        for tick in parse_frame(frame):
                            yield tick
                finally:
                    ping_task.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("bybit_ws_disconnect", error=str(exc), attempt=attempt)
        attempt += 1
        delay = random.uniform(0.5, min(
            settings.PERP_WS_RECONNECT_MAX_SEC,
            float(2**attempt),
        ))
        await asyncio.sleep(delay)


async def _ping_loop(ws: aiohttp.ClientWebSocketResponse, settings: Settings) -> None:
    while not ws.closed:
        try:
            await ws.send_json({"op": "ping"})
        except ConnectionResetError:
            return
        await asyncio.sleep(settings.PERP_WS_PING_INTERVAL_SEC)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_perp_bybit.py -q`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add scout/perp/bybit.py tests/test_perp_bybit.py tests/fixtures/perp/bybit_ticker.json
git commit -m "feat(bl-054): bybit v5 WS client with op:ping keepalive"
```

---

### Task 5: DB layer (schema + CRUD + batch + prune)

**Files:**
- Modify: `scout/db.py`
- Create: `tests/test_perp_db.py`

- [ ] **Step 1: Write failing DB tests**

```python
# tests/test_perp_db.py
import pytest
from datetime import datetime, timezone, timedelta
from scout.db import Database
from scout.perp.schemas import PerpAnomaly

@pytest.fixture
async def db(tmp_path):
    path = tmp_path / "test.db"
    database = Database(db_path=path)
    await database.connect()
    yield database
    await database.close()

def _anomaly(ticker: str = "BTC", *, observed_at: datetime | None = None,
             kind: str = "oi_spike", exchange: str = "binance") -> PerpAnomaly:
    return PerpAnomaly(
        exchange=exchange, symbol=f"{ticker}USDT", ticker=ticker,
        kind=kind, magnitude=3.5, baseline=100.0,
        observed_at=observed_at or datetime.now(timezone.utc),
    )

@pytest.mark.asyncio
async def test_insert_and_fetch_recent(db):
    a = _anomaly("BTC")
    await db.insert_perp_anomaly(a)
    since = datetime.now(timezone.utc) - timedelta(minutes=15)
    rows = await db.fetch_recent_perp_anomalies(tickers=["BTC"], since=since)
    assert len(rows) == 1 and rows[0].ticker == "BTC"

@pytest.mark.asyncio
async def test_batch_insert_is_atomic(db):
    batch = [_anomaly(t) for t in ("A", "B", "C")]
    inserted = await db.insert_perp_anomalies_batch(batch)
    assert inserted == 3
    rows = await db.fetch_recent_perp_anomalies(
        tickers=["A", "B", "C"],
        since=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    assert {r.ticker for r in rows} == {"A", "B", "C"}

@pytest.mark.asyncio
async def test_fetch_filters_by_ticker_and_time(db):
    await db.insert_perp_anomaly(_anomaly("BTC"))
    await db.insert_perp_anomaly(_anomaly("ETH"))
    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    rows = await db.fetch_recent_perp_anomalies(tickers=["BTC"], since=since)
    assert {r.ticker for r in rows} == {"BTC"}

@pytest.mark.asyncio
async def test_fetch_empty_ticker_list_returns_empty(db):
    await db.insert_perp_anomaly(_anomaly("BTC"))
    rows = await db.fetch_recent_perp_anomalies(
        tickers=[], since=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    assert rows == []

@pytest.mark.asyncio
async def test_prune(db):
    old = datetime.now(timezone.utc) - timedelta(days=30)
    fresh = datetime.now(timezone.utc)
    await db.insert_perp_anomaly(_anomaly("OLD", observed_at=old))
    await db.insert_perp_anomaly(_anomaly("FRESH", observed_at=fresh))
    pruned = await db.prune_perp_anomalies(keep_days=7)
    assert pruned == 1
    rows = await db.fetch_recent_perp_anomalies(
        tickers=["OLD", "FRESH"],
        since=old - timedelta(days=1),
    )
    assert [r.ticker for r in rows] == ["FRESH"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_perp_db.py -q`
Expected: AttributeError / missing methods on Database.

- [ ] **Step 3: Add table + indexes to the `_init_schema` block in `scout/db.py`**

Add to the existing schema DDL execution:

```python
await self._conn.execute("""
    CREATE TABLE IF NOT EXISTS perp_anomalies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        exchange TEXT NOT NULL,
        symbol TEXT NOT NULL,
        ticker TEXT NOT NULL,
        kind TEXT NOT NULL,
        magnitude REAL NOT NULL,
        baseline REAL,
        observed_at TEXT NOT NULL
    )
""")
await self._conn.execute(
    "CREATE INDEX IF NOT EXISTS idx_perp_anomalies_ticker_observed "
    "ON perp_anomalies (ticker, observed_at DESC)"
)
await self._conn.execute(
    "CREATE INDEX IF NOT EXISTS idx_perp_anomalies_observed "
    "ON perp_anomalies (observed_at)"
)
```

- [ ] **Step 4: Add CRUD methods to `scout/db.py`**

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scout.perp.schemas import PerpAnomaly


async def insert_perp_anomaly(self, anomaly: "PerpAnomaly") -> None:
    """Insert a single anomaly. Kept for tests; prefer batch in hot path."""
    await self._conn.execute(
        "INSERT INTO perp_anomalies "
        "(exchange, symbol, ticker, kind, magnitude, baseline, observed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (anomaly.exchange, anomaly.symbol, anomaly.ticker, anomaly.kind,
         anomaly.magnitude, anomaly.baseline, anomaly.observed_at.isoformat()),
    )
    await self._conn.commit()


async def insert_perp_anomalies_batch(
    self, rows: list["PerpAnomaly"]
) -> int:
    """Primary write path. Single transaction, returns row count."""
    if not rows:
        return 0
    payload = [
        (a.exchange, a.symbol, a.ticker, a.kind, a.magnitude, a.baseline,
         a.observed_at.isoformat())
        for a in rows
    ]
    await self._conn.executemany(
        "INSERT INTO perp_anomalies "
        "(exchange, symbol, ticker, kind, magnitude, baseline, observed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        payload,
    )
    await self._conn.commit()
    return len(rows)


async def fetch_recent_perp_anomalies(
    self, *, tickers: list[str], since: datetime
) -> list["PerpAnomaly"]:
    from scout.perp.schemas import PerpAnomaly
    if not tickers:
        return []
    placeholders = ",".join(["?"] * len(tickers))
    cur = await self._conn.execute(
        f"SELECT exchange, symbol, ticker, kind, magnitude, baseline, observed_at "
        f"FROM perp_anomalies "
        f"WHERE ticker IN ({placeholders}) AND observed_at >= ? "
        f"ORDER BY observed_at DESC",
        (*tickers, since.isoformat()),
    )
    rows = await cur.fetchall()
    return [
        PerpAnomaly(
            exchange=r[0], symbol=r[1], ticker=r[2], kind=r[3],
            magnitude=r[4], baseline=r[5],
            observed_at=datetime.fromisoformat(r[6]),
        )
        for r in rows
    ]


async def prune_perp_anomalies(self, *, keep_days: int) -> int:
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=keep_days)
    ).isoformat()
    cur = await self._conn.execute(
        "DELETE FROM perp_anomalies WHERE observed_at <= ?", (cutoff,)
    )
    await self._conn.commit()
    return cur.rowcount or 0
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_perp_db.py -q`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add scout/db.py tests/test_perp_db.py
git commit -m "feat(bl-054): perp_anomalies table + batch insert + prune"
```

---

### Task 6: CandidateToken fields + enrichment helper

**Files:**
- Modify: `scout/models.py`
- Create: `scout/perp/enrichment.py`
- Create: `tests/test_perp_enrichment.py`

- [ ] **Step 1: Write failing enrichment test**

```python
# tests/test_perp_enrichment.py
import pytest
from datetime import datetime, timezone, timedelta
from scout.db import Database
from scout.perp.enrichment import enrich_candidates_with_perp_anomalies
from scout.perp.schemas import PerpAnomaly

@pytest.fixture
async def db(tmp_path):
    database = Database(db_path=tmp_path / "t.db")
    await database.connect()
    yield database
    await database.close()

@pytest.mark.asyncio
async def test_enrich_matches_by_ticker_case_insensitive(db, token_factory, settings_factory):
    settings = settings_factory(PERP_ANOMALY_LOOKBACK_MIN=15)
    now = datetime.now(timezone.utc)
    await db.insert_perp_anomaly(PerpAnomaly(
        exchange="binance", symbol="DOGEUSDT", ticker="DOGE",
        kind="oi_spike", magnitude=4.5, baseline=1.0, observed_at=now,
    ))
    tokens = [
        token_factory(ticker="doge"),
        token_factory(ticker="SHIB"),
    ]
    enriched = await enrich_candidates_with_perp_anomalies(tokens, db, settings)
    assert enriched[0].perp_oi_spike_ratio == 4.5
    assert enriched[0].perp_exchange == "binance"
    assert enriched[0].perp_last_anomaly_at is not None
    assert enriched[1].perp_oi_spike_ratio is None

@pytest.mark.asyncio
async def test_enrich_funding_flip_sets_flag(db, token_factory, settings_factory):
    settings = settings_factory(PERP_ANOMALY_LOOKBACK_MIN=15)
    now = datetime.now(timezone.utc)
    await db.insert_perp_anomaly(PerpAnomaly(
        exchange="bybit", symbol="ETHUSDT", ticker="ETH",
        kind="funding_flip", magnitude=0.08, baseline=0.0001, observed_at=now,
    ))
    enriched = await enrich_candidates_with_perp_anomalies(
        [token_factory(ticker="ETH")], db, settings,
    )
    assert enriched[0].perp_funding_flip is True

@pytest.mark.asyncio
async def test_enrich_ignores_old_anomalies(db, token_factory, settings_factory):
    settings = settings_factory(PERP_ANOMALY_LOOKBACK_MIN=15)
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    await db.insert_perp_anomaly(PerpAnomaly(
        exchange="binance", symbol="BTCUSDT", ticker="BTC",
        kind="oi_spike", magnitude=5.0, baseline=1.0, observed_at=old,
    ))
    enriched = await enrich_candidates_with_perp_anomalies(
        [token_factory(ticker="BTC")], db, settings,
    )
    assert enriched[0].perp_oi_spike_ratio is None
```

- [ ] **Step 2: Run to verify fails**

Run: `uv run pytest tests/test_perp_enrichment.py -q`
Expected: ImportError / AttributeError.

- [ ] **Step 3: Add fields to `scout/models.py`**

Insert next to the other optional fields in `CandidateToken`:

```python
# CandidateToken body — add after news_tag_confidence:
    # Perp anomaly fields (BL-054)
    perp_funding_flip: bool | None = None
    perp_oi_spike_ratio: float | None = None
    perp_last_anomaly_at: datetime | None = None
    perp_exchange: str | None = None   # "binance" | "bybit" (string for flexibility)
```

Do NOT import `Exchange` literal into models.py; use `str` to avoid tightening the dependency beyond necessity. (Acknowledged design deviation vs spec §3.6 — `str` is lighter and sufficient for a tagging field; the ticker itself is already validated at schema boundary.)

- [ ] **Step 4: Implement `scout/perp/enrichment.py`**

```python
# scout/perp/enrichment.py
"""Enrich CandidateTokens with perp anomaly tags from the DB."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from scout.config import Settings
    from scout.db import Database
    from scout.models import CandidateToken

logger = structlog.get_logger()


async def enrich_candidates_with_perp_anomalies(
    tokens: list["CandidateToken"],
    db: "Database",
    settings: "Settings",
) -> list["CandidateToken"]:
    """Attach perp-anomaly fields to matching candidates. Pure-ish (DB read)."""
    if not tokens:
        return tokens
    tickers = list({t.ticker.upper() for t in tokens if t.ticker})
    if not tickers:
        return tokens
    since = datetime.now(timezone.utc) - timedelta(
        minutes=settings.PERP_ANOMALY_LOOKBACK_MIN
    )
    anomalies = await db.fetch_recent_perp_anomalies(
        tickers=tickers, since=since
    )
    if not anomalies:
        return tokens

    # Index by ticker -> best-effort most-recent first (fetch returns DESC)
    by_ticker: dict[str, list] = {}
    for a in anomalies:
        by_ticker.setdefault(a.ticker.upper(), []).append(a)

    enriched: list[CandidateToken] = []
    for token in tokens:
        matches = by_ticker.get((token.ticker or "").upper())
        if not matches:
            enriched.append(token)
            continue
        latest = matches[0]
        funding_flip = any(a.kind == "funding_flip" for a in matches)
        oi_spike_ratio = max(
            (a.magnitude for a in matches if a.kind == "oi_spike"),
            default=None,
        )
        enriched.append(token.model_copy(update={
            "perp_funding_flip": funding_flip or None,
            "perp_oi_spike_ratio": oi_spike_ratio,
            "perp_last_anomaly_at": latest.observed_at,
            "perp_exchange": latest.exchange,
        }))
    logger.debug(
        "perp_enrichment_done",
        token_count=len(tokens),
        matches=sum(1 for t in enriched if t.perp_last_anomaly_at is not None),
    )
    return enriched
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_perp_enrichment.py -q`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add scout/models.py scout/perp/enrichment.py tests/test_perp_enrichment.py
git commit -m "feat(bl-054): CandidateToken perp fields + enrichment helper"
```

---

### Task 7: Scorer Signal 14 with denominator-ready guard

**Files:**
- Modify: `scout/scorer.py`
- Create: `tests/test_perp_scorer.py`

- [ ] **Step 1: Write failing scorer tests**

```python
# tests/test_perp_scorer.py
from datetime import datetime, timezone
from unittest.mock import patch
from scout import scorer as scorer_mod
from scout.scorer import score

def _tagged(token_factory, **extra):
    defaults = {
        "ticker": "DOGE",
        "liquidity_usd": 50_000,
        "perp_last_anomaly_at": datetime.now(timezone.utc),
        "perp_oi_spike_ratio": 4.5,
    }
    defaults.update(extra)
    return token_factory(**defaults)

def test_perp_signal_does_not_fire_when_flag_off(token_factory, settings_factory):
    settings = settings_factory(PERP_SCORING_ENABLED=False)
    token = _tagged(token_factory)
    points, signals = score(token, settings)
    assert "perp_anomaly" not in signals

def test_perp_signal_does_not_fire_when_denominator_not_ready(token_factory, settings_factory):
    settings = settings_factory(PERP_SCORING_ENABLED=True)
    token = _tagged(token_factory)
    # SCORER_MAX_RAW ships at 183, so denominator-not-ready — signal must NOT fire.
    assert scorer_mod.SCORER_MAX_RAW == 183
    points, signals = score(token, settings)
    assert "perp_anomaly" not in signals

def test_perp_signal_fires_when_both_flag_and_denominator_ready(
    token_factory, settings_factory,
):
    settings = settings_factory(PERP_SCORING_ENABLED=True, PERP_OI_SPIKE_RATIO=3.0)
    token = _tagged(token_factory)
    with patch.object(scorer_mod, "SCORER_MAX_RAW", 203), \
         patch.object(scorer_mod, "_PERP_SCORING_DENOMINATOR_READY", True):
        points, signals = score(token, settings)
    assert "perp_anomaly" in signals

def test_perp_signal_funding_flip_path(token_factory, settings_factory):
    settings = settings_factory(PERP_SCORING_ENABLED=True)
    token = _tagged(token_factory, perp_funding_flip=True, perp_oi_spike_ratio=None)
    with patch.object(scorer_mod, "SCORER_MAX_RAW", 203), \
         patch.object(scorer_mod, "_PERP_SCORING_DENOMINATOR_READY", True):
        points, signals = score(token, settings)
    assert "perp_anomaly" in signals

def test_perp_signal_skips_when_no_anomaly_timestamp(token_factory, settings_factory):
    settings = settings_factory(PERP_SCORING_ENABLED=True)
    token = _tagged(token_factory, perp_last_anomaly_at=None)
    with patch.object(scorer_mod, "SCORER_MAX_RAW", 203), \
         patch.object(scorer_mod, "_PERP_SCORING_DENOMINATOR_READY", True):
        points, signals = score(token, settings)
    assert "perp_anomaly" not in signals
```

- [ ] **Step 2: Run to verify fails**

Run: `uv run pytest tests/test_perp_scorer.py -q`
Expected: AttributeError on `SCORER_MAX_RAW` or missing `_PERP_SCORING_DENOMINATOR_READY`.

- [ ] **Step 3: Insert Signal 14 in `scout/scorer.py`**

Add a module-level constant alongside `SCORER_MAX_RAW`:

```python
# Runtime guard for Signal 14. See design spec §3.9.
# The constant and flag BOTH must be true for the signal to fire, preventing
# silent score inflation if PERP_SCORING_ENABLED is flipped ahead of the
# recalibration PR that bumps SCORER_MAX_RAW to 203.
_PERP_SCORING_DENOMINATOR_READY = SCORER_MAX_RAW >= 203
```

Insert the Signal 14 block BETWEEN Signal 13 (`cryptopanic_bullish` — which doesn't exist yet in master; since BL-053 hasn't merged, insert AFTER Signal 10 solana_bonus and BEFORE Signal 11 velocity, keeping consistent numbering with the existing scorer.py):

```python
    # Signal 14: Perp anomaly (BL-054) -- 10 points, gated.
    # Double-gate: PERP_SCORING_ENABLED + SCORER_MAX_RAW >= 203. The second
    # gate is the runtime guard that prevents the scoring flag from silently
    # inflating scores before the recalibration PR lands. Tests monkeypatch
    # both. See design spec docs/superpowers/specs/
    # 2026-04-20-bl054-perp-ws-anomaly-detector-design.md §3.9.
    if (
        settings.PERP_SCORING_ENABLED
        and _PERP_SCORING_DENOMINATOR_READY
        and token.perp_last_anomaly_at is not None
        and (
            token.perp_funding_flip
            or (token.perp_oi_spike_ratio or 0) >= settings.PERP_OI_SPIKE_RATIO
        )
    ):
        points += 10
        signals.append("perp_anomaly")
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_perp_scorer.py tests/test_scorer.py -q`
Expected: new tests pass + existing scorer tests still pass (zero regression).

- [ ] **Step 5: Commit**

```bash
git add scout/scorer.py tests/test_perp_scorer.py
git commit -m "feat(bl-054): scorer signal 14 with denominator-ready runtime guard"
```

---

### Task 8: Main pipeline wiring (task launch + Stage 2.5 + prune)

**Files:**
- Modify: `scout/main.py`
- Create: `tests/test_main_perp_integration.py`

- [ ] **Step 1: Write failing integration test (4 assertions)**

```python
# tests/test_main_perp_integration.py
import asyncio
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from scout.perp.schemas import PerpAnomaly

@pytest.mark.asyncio
async def test_perp_disabled_no_task_launched(settings_factory, tmp_path):
    settings = settings_factory(PERP_ENABLED=False, DB_PATH=tmp_path / "t.db")
    with patch("scout.main.run_perp_watcher") as mock_watcher:
        from scout.main import _maybe_start_perp_watcher
        await _maybe_start_perp_watcher(settings, db=None, session=None)
    mock_watcher.assert_not_called()

@pytest.mark.asyncio
async def test_perp_disabled_enrichment_skipped(
    settings_factory, token_factory, tmp_path,
):
    from scout.main import _maybe_enrich_perp
    settings = settings_factory(PERP_ENABLED=False)
    tokens = [token_factory(ticker="BTC")]
    result = await _maybe_enrich_perp(tokens, db=None, settings=settings)
    assert result is tokens  # unchanged, no DB call

@pytest.mark.asyncio
async def test_perp_enabled_invokes_enrichment(
    settings_factory, token_factory, tmp_path,
):
    from scout.db import Database
    from scout.main import _maybe_enrich_perp
    db = Database(db_path=tmp_path / "t.db")
    await db.connect()
    try:
        await db.insert_perp_anomaly(PerpAnomaly(
            exchange="binance", symbol="BTCUSDT", ticker="BTC",
            kind="oi_spike", magnitude=4.0, baseline=1.0,
            observed_at=datetime.now(timezone.utc),
        ))
        settings = settings_factory(PERP_ENABLED=True, PERP_ANOMALY_LOOKBACK_MIN=15)
        out = await _maybe_enrich_perp([token_factory(ticker="BTC")], db=db, settings=settings)
        assert out[0].perp_oi_spike_ratio == 4.0
    finally:
        await db.close()
```

- [ ] **Step 2: Run to verify fails**

Run: `uv run pytest tests/test_main_perp_integration.py -q`
Expected: ImportError on helpers.

- [ ] **Step 3: Add two small helpers near the top of `run_cycle` / startup block in `scout/main.py`**

```python
# scout/main.py — imports
from scout.perp.enrichment import enrich_candidates_with_perp_anomalies
from scout.perp.watcher import run_perp_watcher


async def _maybe_start_perp_watcher(settings, *, db, session) -> asyncio.Task | None:
    """Launch the perp watcher iff PERP_ENABLED. Returns the task or None."""
    if not settings.PERP_ENABLED:
        return None
    if not (settings.PERP_BINANCE_ENABLED or settings.PERP_BYBIT_ENABLED):
        logger.warning("perp_watcher_no_exchanges_enabled_noop")
        return None
    return asyncio.create_task(
        run_perp_watcher(session, db, settings), name="perp-watcher",
    )


async def _maybe_enrich_perp(tokens, *, db, settings):
    """Run perp enrichment iff PERP_ENABLED. Return tokens unchanged otherwise."""
    if not settings.PERP_ENABLED or db is None:
        return tokens
    return await enrich_candidates_with_perp_anomalies(tokens, db, settings)
```

- [ ] **Step 4: Wire the helpers into `start_pipeline` / `run_cycle`**

- In `start_pipeline`: after the existing `Database.connect()` and session creation, call `perp_task = await _maybe_start_perp_watcher(settings, db=db, session=session)`.
- In `run_cycle` after aggregation and before scoring: `tokens = await _maybe_enrich_perp(tokens, db=db, settings=settings)`.
- In the hourly maintenance block (the same place BL-053 adds `prune_cryptopanic_posts`): add `await db.prune_perp_anomalies(keep_days=settings.PERP_ANOMALY_RETENTION_DAYS)`.
- On graceful shutdown: `if perp_task is not None: perp_task.cancel()`.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_main_perp_integration.py tests/test_main_*.py -q`
Expected: new tests + existing main tests pass.

- [ ] **Step 6: Commit**

```bash
git add scout/main.py tests/test_main_perp_integration.py
git commit -m "feat(bl-054): wire perp watcher + enrichment + prune into main pipeline"
```

---

### Task 9: Watcher supervisor (backpressure queue + batched flush + circuit-breaker + shadow stats)

**Files:**
- Create: `scout/perp/watcher.py`
- Create: `tests/test_perp_watcher.py`

This is the most complex task. TDD it incrementally: build the pieces in order (classifier loop → backpressure test → circuit breaker test → supervisor).

- [ ] **Step 1: Write failing test: classifier consumes queue and batches flushes by size**

```python
# tests/test_perp_watcher.py
import asyncio
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from scout.perp.schemas import PerpTick
from scout.perp.watcher import (
    classifier_loop, ClassifierState,
)
from scout.perp.baseline import BaselineStore

def _make_tick(oi: float, ts: float = 0.0, ticker: str = "BTC") -> PerpTick:
    return PerpTick(
        exchange="binance", symbol=f"{ticker}USDT", ticker=ticker,
        open_interest=oi, funding_rate=None,
        timestamp=datetime.fromtimestamp(ts or 1713600000.0, tz=timezone.utc),
    )

@pytest.mark.asyncio
async def test_classifier_batch_flush_on_size(settings_factory):
    settings = settings_factory(
        PERP_BASELINE_MIN_SAMPLES=1,
        PERP_DB_FLUSH_INTERVAL_SEC=1000.0,  # prevent interval flush
        PERP_DB_FLUSH_MAX_ROWS=2,
        PERP_OI_SPIKE_RATIO=3.0,
        PERP_ANOMALY_DEDUP_MIN=0,
    )
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    db = AsyncMock()
    db.insert_perp_anomalies_batch = AsyncMock(return_value=2)
    state = ClassifierState(baseline=BaselineStore(
        alpha=0.5, max_keys=10, idle_evict_seconds=3600,
    ))
    # Prime baseline + trigger three spikes (two on first two ticks is unrealistic,
    # but with dedup=0 and aggressive alpha we can force it).
    for symbol in ("A", "B", "C"):
        await queue.put(_make_tick(oi=1.0, ticker=symbol))
        await queue.put(_make_tick(oi=10.0, ticker=symbol))  # spike
    await queue.put(None)  # sentinel to stop
    await classifier_loop(queue, state, db, settings)
    # Expect at least one batch flush when size cap hit.
    assert db.insert_perp_anomalies_batch.await_count >= 1

@pytest.mark.asyncio
async def test_classifier_dedup(settings_factory):
    settings = settings_factory(
        PERP_BASELINE_MIN_SAMPLES=1,
        PERP_DB_FLUSH_INTERVAL_SEC=0.01,
        PERP_DB_FLUSH_MAX_ROWS=1000,
        PERP_OI_SPIKE_RATIO=2.0,
        PERP_ANOMALY_DEDUP_MIN=5,
    )
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    db = AsyncMock()
    db.insert_perp_anomalies_batch = AsyncMock()
    state = ClassifierState(baseline=BaselineStore(
        alpha=0.5, max_keys=10, idle_evict_seconds=3600,
    ))
    await queue.put(_make_tick(oi=1.0))
    await queue.put(_make_tick(oi=10.0))  # spike fires
    await queue.put(_make_tick(oi=11.0))  # same (exchange,symbol,kind) within cooldown — suppressed
    await queue.put(None)
    await classifier_loop(queue, state, db, settings)
    # Collect all anomaly rows flushed across any batches.
    total = sum(len(call.args[0]) for call in db.insert_perp_anomalies_batch.await_args_list)
    assert total == 1
```

- [ ] **Step 2: Run to verify fails**

Run: `uv run pytest tests/test_perp_watcher.py -q`
Expected: ImportError / missing class.

- [ ] **Step 3: Implement classifier loop in `scout/perp/watcher.py`**

```python
# scout/perp/watcher.py
"""Perp watcher supervisor + classifier pipeline.

Architecture (see design spec §3.5):
  parser task(s) -> asyncio.Queue(maxsize=PERP_QUEUE_MAXSIZE) -> classifier_loop
                                                                   |
                                                                   v
                                              db.insert_perp_anomalies_batch
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import aiohttp
import structlog

from scout.perp.anomaly import classify_funding_flip, classify_oi_spike
from scout.perp.baseline import BaselineStore
from scout.perp.binance import stream_ticks as binance_stream
from scout.perp.bybit import stream_ticks as bybit_stream
from scout.perp.schemas import PerpAnomaly, PerpTick

if TYPE_CHECKING:
    from scout.config import Settings
    from scout.db import Database

logger = structlog.get_logger()


@dataclass
class ClassifierState:
    baseline: BaselineStore
    # last_fired[(exchange, symbol, kind)] = monotonic seconds
    last_fired: dict[tuple[str, str, str], float] = field(default_factory=dict)
    # last_funding[(exchange, symbol)] = previous funding rate
    last_funding: dict[tuple[str, str], float] = field(default_factory=dict)
    dropped_ticks: int = 0
    queue_high_water: int = 0


async def classifier_loop(
    queue: asyncio.Queue,
    state: ClassifierState,
    db: "Database",
    settings: "Settings",
) -> None:
    """Drain queue, run classifiers, batch-flush anomalies to DB.

    Stops when ``None`` sentinel is received.
    """
    batch: list[PerpAnomaly] = []
    last_flush = time.monotonic()
    now_mono = time.monotonic
    flush_interval = settings.PERP_DB_FLUSH_INTERVAL_SEC
    max_rows = settings.PERP_DB_FLUSH_MAX_ROWS
    dedup_sec = settings.PERP_ANOMALY_DEDUP_MIN * 60
    while True:
        try:
            tick = await asyncio.wait_for(queue.get(), timeout=flush_interval)
        except asyncio.TimeoutError:
            tick = None  # force flush check
        else:
            if tick is None:
                # sentinel: final flush and exit
                if batch:
                    await db.insert_perp_anomalies_batch(batch)
                    batch.clear()
                return
            state.queue_high_water = max(state.queue_high_water, queue.qsize())
            _process_tick(tick, state, batch, settings, now_mono(), dedup_sec)

        # Time- or size-based flush.
        if batch and (len(batch) >= max_rows or now_mono() - last_flush >= flush_interval):
            await db.insert_perp_anomalies_batch(batch)
            batch.clear()
            last_flush = now_mono()


def _process_tick(
    tick: PerpTick,
    state: ClassifierState,
    batch: list[PerpAnomaly],
    settings: "Settings",
    now_mono_s: float,
    dedup_sec: float,
) -> None:
    key = (tick.exchange, tick.symbol)
    state.baseline.update(
        key,
        oi=tick.open_interest,
        funding=tick.funding_rate,
        now=tick.timestamp,
    )
    # OI spike
    if tick.open_interest is not None:
        anomaly = classify_oi_spike(
            current_oi=tick.open_interest,
            baseline_oi=state.baseline.oi_baseline(key),
            exchange=tick.exchange, symbol=tick.symbol, ticker=tick.ticker,
            observed_at=tick.timestamp,
            sample_count=state.baseline.sample_count(key),
            min_samples=settings.PERP_BASELINE_MIN_SAMPLES,
            spike_ratio=settings.PERP_OI_SPIKE_RATIO,
        )
        if anomaly and _accept_dedup(state, tick, "oi_spike", now_mono_s, dedup_sec):
            batch.append(anomaly)
    # Funding flip
    if tick.funding_rate is not None:
        prev = state.last_funding.get(key)
        anomaly = classify_funding_flip(
            prev_rate=prev, new_rate=tick.funding_rate,
            exchange=tick.exchange, symbol=tick.symbol, ticker=tick.ticker,
            observed_at=tick.timestamp,
            min_magnitude_pct=settings.PERP_FUNDING_FLIP_MIN_PCT,
        )
        if anomaly and _accept_dedup(state, tick, "funding_flip", now_mono_s, dedup_sec):
            batch.append(anomaly)
        state.last_funding[key] = tick.funding_rate


def _accept_dedup(
    state: ClassifierState, tick: PerpTick, kind: str,
    now_mono_s: float, dedup_sec: float,
) -> bool:
    key = (tick.exchange, tick.symbol, kind)
    last = state.last_fired.get(key)
    if last is not None and now_mono_s - last < dedup_sec:
        return False
    state.last_fired[key] = now_mono_s
    return True
```

- [ ] **Step 4: Run classifier_loop tests**

Run: `uv run pytest tests/test_perp_watcher.py -q`
Expected: 2 passed.

- [ ] **Step 5: Add backpressure test + `push_with_drop_oldest` helper**

```python
# tests/test_perp_watcher.py — append
from scout.perp.watcher import push_with_drop_oldest, ClassifierState

@pytest.mark.asyncio
async def test_push_drops_oldest_on_full_queue():
    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    state = ClassifierState(baseline=BaselineStore(
        alpha=0.1, max_keys=10, idle_evict_seconds=3600))
    # Fill queue.
    await q.put(_make_tick(oi=1.0, ticker="A"))
    await q.put(_make_tick(oi=2.0, ticker="B"))
    # Third push MUST drop oldest and accept new.
    await push_with_drop_oldest(q, _make_tick(oi=3.0, ticker="C"), state)
    contents = []
    while not q.empty():
        contents.append(q.get_nowait())
    tickers = [t.ticker for t in contents]
    assert tickers == ["B", "C"]
    assert state.dropped_ticks == 1
```

- [ ] **Step 6: Run test (expect fail)** and then implement `push_with_drop_oldest` in watcher.py:

```python
async def push_with_drop_oldest(
    queue: asyncio.Queue, tick: PerpTick, state: ClassifierState,
) -> None:
    """Enqueue a tick; drop the oldest if queue is full.

    Both markPrice and openInterest frames are snapshots of current
    value, not deltas -- the freshest tick fully supersedes any older
    frame for the same (exchange, symbol). Drop-oldest therefore
    preserves correctness in both stream types.
    """
    try:
        queue.put_nowait(tick)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        state.dropped_ticks += 1
        try:
            queue.put_nowait(tick)
        except asyncio.QueueFull:
            # Extremely unlikely: someone else filled it between our get and put.
            state.dropped_ticks += 1
```

- [ ] **Step 7: Run push test**

Run: `uv run pytest tests/test_perp_watcher.py::test_push_drops_oldest_on_full_queue -q`
Expected: pass.

- [ ] **Step 8: Add supervisor + circuit-breaker test**

```python
# tests/test_perp_watcher.py — append

@pytest.mark.asyncio
async def test_circuit_breaker_parks_exchange(settings_factory):
    from scout.perp.watcher import _run_exchange_with_supervision
    settings = settings_factory(
        PERP_MAX_CONSECUTIVE_RESTARTS=2,
        PERP_CIRCUIT_BREAK_SEC=3600,
    )

    async def always_fail(*a, **kw):
        raise RuntimeError("boom")
        yield  # pragma: no cover

    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    state = ClassifierState(baseline=BaselineStore(
        alpha=0.1, max_keys=10, idle_evict_seconds=3600))
    # Run supervisor for a bounded number of attempts -- it must park after budget.
    task = asyncio.create_task(
        _run_exchange_with_supervision(
            "binance", always_fail, None, settings, queue, state,
            sleep=fake_sleep,
        )
    )
    # Give the task a few loop iterations; it must converge to the circuit-break sleep.
    await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert any(s >= settings.PERP_CIRCUIT_BREAK_SEC for s in sleeps), sleeps
```

- [ ] **Step 9: Implement supervisor in `scout/perp/watcher.py`**

```python
# scout/perp/watcher.py — append

async def _run_exchange_with_supervision(
    name: str,
    stream_fn,  # callable returning an AsyncIterator[PerpTick]
    session: aiohttp.ClientSession | None,
    settings: "Settings",
    queue: asyncio.Queue,
    state: ClassifierState,
    *,
    sleep=asyncio.sleep,
) -> None:
    """Run a single exchange's stream; on restart-budget exhaust, circuit-break.

    ``sleep`` is injectable for test fast-forwarding.
    """
    consecutive_failures = 0
    while True:
        try:
            async for tick in stream_fn(session, settings):
                await push_with_drop_oldest(queue, tick, state)
            # stream_fn should never return normally; treat return as failure.
            consecutive_failures += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            consecutive_failures += 1
            logger.warning(
                "perp_exchange_inner_task_crashed",
                exchange=name, failures=consecutive_failures, error=str(exc),
            )
        if consecutive_failures >= settings.PERP_MAX_CONSECUTIVE_RESTARTS:
            logger.error(
                "perp_exchange_circuit_break",
                exchange=name, cooldown_sec=settings.PERP_CIRCUIT_BREAK_SEC,
            )
            await sleep(settings.PERP_CIRCUIT_BREAK_SEC)
            consecutive_failures = 0
        else:
            await sleep(5)  # brief pause between inner-task restarts


async def run_perp_watcher(
    session: aiohttp.ClientSession,
    db: "Database",
    settings: "Settings",
) -> None:
    """Top-level supervisor: parsers + classifier share one BaselineStore + queue."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=settings.PERP_QUEUE_MAXSIZE)
    state = ClassifierState(
        baseline=BaselineStore(
            alpha=settings.PERP_BASELINE_ALPHA,
            max_keys=settings.PERP_BASELINE_MAX_KEYS,
            idle_evict_seconds=settings.PERP_BASELINE_IDLE_EVICT_SEC,
        )
    )
    tasks: list[asyncio.Task] = []
    if settings.PERP_BINANCE_ENABLED:
        tasks.append(asyncio.create_task(
            _run_exchange_with_supervision(
                "binance", binance_stream, session, settings, queue, state,
            ),
            name="perp-binance",
        ))
    if settings.PERP_BYBIT_ENABLED:
        tasks.append(asyncio.create_task(
            _run_exchange_with_supervision(
                "bybit", bybit_stream, session, settings, queue, state,
            ),
            name="perp-bybit",
        ))
    if not tasks:
        logger.warning("perp_watcher_no_exchanges_enabled_noop")
        return
    tasks.append(asyncio.create_task(
        classifier_loop(queue, state, db, settings), name="perp-classifier",
    ))
    tasks.append(asyncio.create_task(
        _shadow_stats_loop(state, settings), name="perp-shadow-stats",
    ))
    tasks.append(asyncio.create_task(
        _baseline_evict_loop(state, settings), name="perp-baseline-evict",
    ))
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
        raise


async def _shadow_stats_loop(state: ClassifierState, settings: "Settings") -> None:
    """Emit per-minute shadow-observability counters via structlog."""
    while True:
        await asyncio.sleep(60)
        logger.info(
            "perp_shadow_stats",
            dropped_ticks_last_min=state.dropped_ticks,
            queue_high_water=state.queue_high_water,
            baseline_keys=len(state.baseline),
        )
        state.dropped_ticks = 0
        state.queue_high_water = 0


async def _baseline_evict_loop(state: ClassifierState, settings: "Settings") -> None:
    while True:
        await asyncio.sleep(300)  # every 5 min
        evicted = state.baseline.evict_idle(now=datetime.now(timezone.utc))
        if evicted:
            logger.info("perp_baseline_evicted_idle", count=evicted)
```

- [ ] **Step 10: Run all watcher tests**

Run: `uv run pytest tests/test_perp_watcher.py -q`
Expected: 4 passed.

- [ ] **Step 11: Commit**

```bash
git add scout/perp/watcher.py tests/test_perp_watcher.py
git commit -m "feat(bl-054): watcher supervisor + bounded queue + circuit breaker + shadow stats"
```

---

### Task 10: Flag-off provability snapshot test + regression sweep

**Files:**
- Create: `tests/test_perp_flag_off_snapshot.py`

- [ ] **Step 1: Write the snapshot test**

```python
# tests/test_perp_flag_off_snapshot.py
"""Provability test: shadow mode (PERP_ENABLED=true, SCORING=false) MUST
produce byte-identical scorer output to fully-disabled mode. This is the
contract that lets operators flip PERP_ENABLED=true in production without
affecting scoring."""

from datetime import datetime, timezone
from scout.scorer import score


def _corpus(token_factory):
    # Must include >=1 token with populated perp fields so the test bites.
    return [
        token_factory(ticker="BTC", liquidity_usd=50_000),
        token_factory(
            ticker="DOGE",
            liquidity_usd=50_000,
            perp_last_anomaly_at=datetime.now(timezone.utc),
            perp_oi_spike_ratio=5.0,
            perp_funding_flip=True,
            perp_exchange="binance",
        ),
        token_factory(
            ticker="PEPE",
            liquidity_usd=50_000,
            perp_last_anomaly_at=datetime.now(timezone.utc),
            perp_oi_spike_ratio=0.5,  # below threshold
        ),
    ]


def test_shadow_mode_scorer_is_byte_identical_to_disabled(
    token_factory, settings_factory,
):
    corpus = _corpus(token_factory)
    disabled = settings_factory(PERP_ENABLED=False, PERP_SCORING_ENABLED=False)
    shadow = settings_factory(PERP_ENABLED=True, PERP_SCORING_ENABLED=False)
    disabled_out = [score(t, disabled) for t in corpus]
    shadow_out = [score(t, shadow) for t in corpus]
    assert disabled_out == shadow_out
```

- [ ] **Step 2: Run**

Run: `uv run pytest tests/test_perp_flag_off_snapshot.py -q`
Expected: passes immediately (scorer ignores `PERP_ENABLED` entirely; only `PERP_SCORING_ENABLED` + runtime guard matter).

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest -q`
Expected: 881 + new BL-054 tests, all passing, zero regressions.

- [ ] **Step 4: Update README**

Add a short "Perp Anomaly Detector (BL-054)" section to `README.md` after the CryptoPanic section (if it exists) or at the end of the feature list. Summary bullets only — link to the design spec for detail:

```markdown
### Perp Anomaly Detector (BL-054)

Research-only watcher for Binance/Bybit perpetual-futures anomalies:
funding-rate flips + OI spikes. Default OFF. Two independent
kill-switches (`PERP_ENABLED` for data collection, `PERP_SCORING_ENABLED`
for scorer impact). Scoring path is runtime-gated by `SCORER_MAX_RAW`
— flipping the scoring flag alone cannot affect pipeline output until
a follow-up recalibration PR lands. See
`docs/superpowers/specs/2026-04-20-bl054-perp-ws-anomaly-detector-design.md`.
```

- [ ] **Step 5: Commit + close the feature**

```bash
git add tests/test_perp_flag_off_snapshot.py README.md
git commit -m "feat(bl-054): flag-off provability snapshot + README entry"
```

---

## Post-Implementation

After all tasks complete and the full suite is green:

1. Dispatch two parallel full-branch reviewers (spec-compliance + code-quality), same as BL-053.
2. Push branch to origin, open PR titled `feat(bl-054): Binance/Bybit perp WebSocket anomaly detector (research-only, default-off)`.
3. Dispatch two parallel PR reviewers (architecture + security/ops).
4. **Do NOT merge. Do NOT deploy.** Research-only cadence per the autonomous build loop.

Success signals:
- Full test suite green (881 base + BL-054 additions).
- Reviewers return ✅ with only non-blocking follow-ups.
- `PERP_ENABLED=false` and `PERP_SCORING_ENABLED=false` defaults confirmed in `scout/config.py`.
- Runtime guard `_PERP_SCORING_DENOMINATOR_READY = SCORER_MAX_RAW >= 203` is False on merge (SCORER_MAX_RAW stays at 183 in this PR).
