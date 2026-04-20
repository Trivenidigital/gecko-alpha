# BL-054 — Binance/Bybit Perp WebSocket Anomaly Detector — Design

**Date:** 2026-04-20
**Author:** autonomous build loop (Claude Opus 4.7)
**Status:** Draft pending parallel-reviewer sign-off
**Backlog ref:** `backlog.md` PR #30 — perp/funding anomaly tier, free, 2–3 d, seconds lead time
**Scope discipline:** Research-only. Default OFF. Zero production impact when disabled.

---

## 1. Problem

Perpetual-futures markets lead spot by seconds-to-minutes. Two specific signals:

- **Funding-rate flip** — funding-rate sign change (positive → negative or vice-versa) above a minimum magnitude signals regime change in leverage positioning.
- **Open-interest spike** — short-window OI ratio against a rolling baseline signals a sudden influx of leveraged positioning, which for small-cap alts is frequently the lit fuse before a spot pump.

Binance + Bybit both expose public WebSocket streams with funding-rate and OI updates. No auth, no cost, sub-second push latency. We want to observe anomalies on these streams, store them, and tag pipeline candidates whose ticker matches an observed anomaly within a short lookback window.

**Explicitly NOT in this PR:**
- OKX (deferred)
- Live alert path (just tag; scoring signal is gated separately)
- Historical backfill / backtest harness
- Rate-limit-tuned fan-out of every symbol (MVP: curated symbol list + always-on alt-universe catch-all stream)
- SCORER_MAX_RAW recalibration (signal contributes only when `PERP_SCORING_ENABLED=true` — same default-off escape hatch used for BL-053 `cryptopanic_bullish`)

## 2. Architecture

New package `scout/perp/`:

```
scout/perp/
  __init__.py
  schemas.py      # Pydantic models for WS payloads + anomaly events
  baseline.py     # Rolling EWMA baseline store (pure, in-memory)
  anomaly.py      # Pure functions: classify_funding_flip, classify_oi_spike
  binance.py      # Binance WS client + parser
  bybit.py        # Bybit WS client + parser
  watcher.py      # Long-lived async task: connect, parse, classify, persist
```

Data flow:

```
 Binance/Bybit WS ──► parser ──► BaselineStore ──► anomaly classifier
                                                         │
                                                         ▼
                                            db.insert_perp_anomaly()
                                                         │
                                                         ▼
                 main.run_cycle → enrich_candidates_with_perp_anomalies()
                                                         │
                                                         ▼
                                        scorer Signal 14 (gated by flag)
```

The WS watcher is an independent background task launched at pipeline start (same shape as the existing heartbeat / narrative / chains loops). It does not block `run_cycle`. Enrichment is a DB lookup: for each candidate ticker, query `perp_anomalies` for rows in the last `PERP_ANOMALY_LOOKBACK_MIN` minutes.

## 3. Modules

### 3.1 `scout/perp/schemas.py`

```python
from datetime import datetime
from typing import Literal
from pydantic import BaseModel

AnomalyKind = Literal["funding_flip", "oi_spike"]
Exchange = Literal["binance", "bybit"]

class PerpTick(BaseModel):
    exchange: Exchange
    symbol: str            # e.g. "BTCUSDT", "DOGEUSDT" — exchange-native
    ticker: str            # normalized base asset, upper-case: "BTC", "DOGE"
    funding_rate: float | None = None
    mark_price: float | None = None
    open_interest: float | None = None   # contracts (count), not USD
    open_interest_usd: float | None = None
    timestamp: datetime

class PerpAnomaly(BaseModel):
    exchange: Exchange
    symbol: str
    ticker: str            # normalized
    kind: AnomalyKind
    magnitude: float       # funding_pct for flip; ratio_to_baseline for spike
    baseline: float | None # baseline value at detection time
    observed_at: datetime
```

### 3.2 `scout/perp/baseline.py`

In-memory, per-symbol, EWMA baselines. Pure functions. Keyed by `(exchange, symbol)`. No DB — baseline is rebuilt on restart from the first 15 minutes of ticks.

```python
class BaselineStore:
    def __init__(self, alpha: float = 0.1): ...
    def update(self, key: tuple[str, str], oi: float | None, funding: float | None) -> None: ...
    def oi_baseline(self, key: tuple[str, str]) -> float | None: ...
    def funding_baseline(self, key: tuple[str, str]) -> float | None: ...
    def sample_count(self, key: tuple[str, str]) -> int: ...
```

Warmup gate: no anomaly fires until `sample_count >= PERP_BASELINE_MIN_SAMPLES` (default 30 ≈ 15 min of 30s samples).

### 3.3 `scout/perp/anomaly.py`

Pure classifier functions, no I/O:

```python
def classify_funding_flip(
    prev_rate: float | None,
    new_rate: float,
    *,
    min_magnitude_pct: float,
) -> PerpAnomaly | None: ...

def classify_oi_spike(
    current_oi: float,
    baseline_oi: float | None,
    *,
    sample_count: int,
    min_samples: int,
    spike_ratio: float,
) -> PerpAnomaly | None: ...
```

Neither function persists; the watcher is responsible for DB writes. Failing a warmup gate / magnitude threshold → returns `None`.

### 3.4 `scout/perp/binance.py`, `scout/perp/bybit.py`

Exchange-specific WS clients. Each exports a single async generator:

```python
async def stream_ticks(
    session: aiohttp.ClientSession, settings: Settings
) -> AsyncIterator[PerpTick]: ...
```

Binance uses `wss://fstream.binance.com/stream?streams=<streams>`. MVP subscribes to the shared `!markPrice@arr@1s` multiplex (all perps, 1-sec interval, funding + mark only) and on-demand per-symbol `@openInterest` streams for a curated symbol list.

Bybit uses `wss://stream.bybit.com/v5/public/linear`. Subscribes to `tickers.<symbol>` topics for the curated list and relies on the per-tick `fundingRate` + `openInterest` fields delivered by v5.

Each client handles:
- Connection + subscribe handshake
- JSON parse + validate
- 20-second ping (Binance) / 20-second `ping` op (Bybit)
- Reconnect with exponential backoff (2s, 4s, 8s, capped at 60s; unlimited retries)
- Yield normalized `PerpTick` objects. Raw message errors are logged + dropped; never raise.

### 3.5 `scout/perp/watcher.py`

Long-lived async task, one per exchange, plus a top-level supervisor:

```python
async def run_perp_watcher(session, db, settings) -> None:
    # Fan out Binance + Bybit. Each is a self-healing infinite loop.
    # Both feeds converge into one BaselineStore + one anomaly pipeline.
    ...
```

For each yielded tick:
1. Update BaselineStore.
2. Run both classifiers.
3. For any non-None anomaly: debounce per `(exchange, symbol, kind)` via in-memory cooldown (`PERP_ANOMALY_DEDUP_MIN`), then `db.insert_perp_anomaly(anomaly)`.
4. Cheap counter metrics logged every 60s.

Supervisor catches every exception on the inner task and restarts after a 5-second sleep. If `PERP_MAX_CONSECUTIVE_RESTARTS` is breached, the supervisor stops and logs loudly (same pattern as `LUNARCRUSH_MAX_CONSECUTIVE_RESTARTS`).

### 3.6 `scout/models.py` additions

```python
class CandidateToken(BaseModel):
    # ... existing fields ...
    perp_funding_flip: bool | None = None
    perp_oi_spike_ratio: float | None = None   # last ratio observed
    perp_last_anomaly_at: datetime | None = None
    perp_exchange: Exchange | None = None
```

Note on dependency direction: `scout/models.py` will import `Exchange` from `scout/perp/schemas.py`. This mirrors the existing `Sentiment` import from `scout/news/schemas.py` introduced in BL-053 (and flagged by the PR #36 architecture reviewer as an inverted dependency). Fixing that architectural concern belongs to a separate refactor PR before adding the next source — not to this feature branch. Ship BL-054 with the same directional compromise; capture the refactor as a follow-up.

### 3.7 `scout/db.py` additions

Tables:

```sql
CREATE TABLE IF NOT EXISTS perp_anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    ticker TEXT NOT NULL,
    kind TEXT NOT NULL,
    magnitude REAL NOT NULL,
    baseline REAL,
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_perp_anomalies_ticker_observed
    ON perp_anomalies (ticker, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_perp_anomalies_observed
    ON perp_anomalies (observed_at);
```

Methods:
- `insert_perp_anomaly(anomaly: PerpAnomaly) -> None`
- `fetch_recent_perp_anomalies(*, tickers: list[str], since: datetime) -> list[PerpAnomaly]`
- `prune_perp_anomalies(*, keep_days: int) -> int`

### 3.8 `scout/main.py` wiring

Two edits:

1. **Background task launch in `start_pipeline()`** (alongside heartbeat etc.):

```python
if settings.PERP_ENABLED:
    asyncio.create_task(run_perp_watcher(session, db, settings), name="perp-watcher")
```

2. **Stage 2.5 enrichment inside `run_cycle()`**, after aggregation and before scoring:

```python
if settings.PERP_ENABLED:
    tokens = await enrich_candidates_with_perp_anomalies(tokens, db, settings)
```

`enrich_candidates_with_perp_anomalies` is a new helper (either in `scout/perp/watcher.py` or co-located beside `enrich_candidates_with_news`). It does a single SQL fetch for the union of candidate tickers, builds an in-memory index, and sets the three candidate fields. Pure-ish (still reads DB); no network.

Prune hook added to the same hourly maintenance block used for `cryptopanic_posts`.

### 3.9 `scout/scorer.py` — Signal 14

```python
# Signal 14: Perp anomaly (BL-054) -- 10 points, gated.
# SCORER_MAX_RAW stays at 183. Bumping it (and recalibrating co-occurrence
# math) is deferred until CRYPTOPANIC_SCORING_ENABLED and PERP_SCORING_ENABLED
# are both flipped on; doing that in one recalibration PR keeps distribution
# shifts reviewable.
if (
    settings.PERP_SCORING_ENABLED
    and token.perp_last_anomaly_at is not None
    and (
        token.perp_funding_flip
        or (token.perp_oi_spike_ratio or 0) >= settings.PERP_OI_SPIKE_RATIO
    )
):
    points += 10
    signals.append("perp_anomaly")
```

### 3.10 `scout/config.py` additions

```python
# -------- Perp WebSocket Anomaly Detector (BL-054) --------
PERP_ENABLED: bool = False
PERP_SCORING_ENABLED: bool = False   # scorer signal kill-switch (separate)
PERP_BINANCE_ENABLED: bool = True
PERP_BYBIT_ENABLED: bool = True
PERP_SYMBOLS: list[str] = []          # curated list, empty = markPrice-arr only
PERP_FUNDING_FLIP_MIN_PCT: float = 0.01   # 0.01% = 1 bp per 8h
PERP_OI_SPIKE_RATIO: float = 3.0
PERP_BASELINE_ALPHA: float = 0.1
PERP_BASELINE_MIN_SAMPLES: int = 30       # ~15 min at 30s cadence
PERP_ANOMALY_LOOKBACK_MIN: int = 15
PERP_ANOMALY_DEDUP_MIN: int = 5
PERP_ANOMALY_RETENTION_DAYS: int = 7
PERP_MAX_CONSECUTIVE_RESTARTS: int = 5
PERP_WS_PING_INTERVAL_SEC: int = 20
PERP_WS_RECONNECT_MAX_SEC: int = 60
```

`PERP_SYMBOLS` is parsed via the same comma-delimited field_validator pattern as `CHAINS`.

## 4. Failure modes & defensive behavior

| Failure | Behavior |
|---------|----------|
| Binance WS disconnect | reconnect with exponential backoff; unlimited retries |
| Both exchanges down | watcher burns restart budget; after 5 consecutive failures, logs loud and exits task (pipeline keeps running) |
| Malformed WS frame | log + drop; do not raise |
| DB locked | skip this anomaly write; baseline continues |
| Pipeline startup with `PERP_ENABLED=false` | watcher never launched; zero CPU/IO overhead |
| `PERP_ENABLED=true` but both exchange flags false | startup log warning, no task launched |
| No anomalies for a ticker in lookback window | candidate fields remain `None`; scorer no-op |
| `PERP_SCORING_ENABLED=false` but data collecting | anomalies persist + enrichment runs + scorer signal does NOT fire (for shadow observation) |

## 5. Testing

Every public surface gets a corresponding test. Test categories:

1. **Pure classifier tests** (no I/O): funding flip with/without magnitude gate, OI spike with warmup / baseline / ratio gate edge cases.
2. **Baseline store tests**: EWMA math for N samples, warmup gate, no-mutation contract.
3. **Binance/Bybit parser tests**: real payload fixtures committed to `tests/fixtures/perp/`, assert `PerpTick` shape.
4. **WS client tests** using `aioresponses`' WS mock or a hand-rolled async mock: subscribe handshake, pings, reconnect-after-close; max one dedicated slow test marked `@pytest.mark.slow`.
5. **Watcher integration test**: feed synthetic `PerpTick` stream into classifier pipeline, assert DB row count and cooldown enforcement.
6. **DB tests**: insert/fetch/prune round-trip, index presence, cross-ticker query correctness.
7. **`main.py` integration tests** mirroring `test_main_cryptopanic_integration.py` shape: enabled path, disabled path, enrichment fields populated, scoring signal fires only when both flags set, watcher-task-never-launched-when-disabled.
8. **Scorer tests**: signal fires on both funding_flip and oi_spike conditions; does NOT fire when `PERP_SCORING_ENABLED=false`; does NOT fire when `perp_last_anomaly_at` is None; fires correctly under co-occurrence counting.
9. **Regression**: full suite continues to pass. Target: `881 passed, 1 skipped` plus BL-054 additions.

## 6. Delivery milestones (map to plan tasks)

| # | Task | Files | Notes |
|---|------|-------|-------|
| 1 | Config knobs + schemas | `config.py`, `perp/schemas.py`, `perp/__init__.py` | types first |
| 2 | Classifier + baseline pure functions | `perp/anomaly.py`, `perp/baseline.py` | TDD |
| 3 | Binance parser + WS client | `perp/binance.py` | uses aiohttp WS |
| 4 | Bybit parser + WS client | `perp/bybit.py` | same shape as Binance |
| 5 | DB layer | `db.py` | migrations + CRUD |
| 6 | CandidateToken fields + enrichment helper | `models.py`, `perp/watcher.py` | |
| 7 | Scorer Signal 14 | `scorer.py` | gated, SCORER_MAX_RAW unchanged |
| 8 | Main pipeline wiring | `main.py` | background task + stage 2.5 |
| 9 | Watcher supervisor + restart budget | `perp/watcher.py` | ties it all together |
| 10 | Docs / README stub + regression sweep | `README.md` | |

Frequent-commit rhythm: one commit per task, per the plan template.

## 7. Rollout

- Ship with `PERP_ENABLED=false`, `PERP_SCORING_ENABLED=false`.
- Merge is allowed (research-only, no deployment impact).
- Operator flips `PERP_ENABLED=true` on VPS to start collecting data (zero scoring impact).
- After 7 days of anomaly data, separate PR flips `PERP_SCORING_ENABLED=true` AND bumps `SCORER_MAX_RAW` to 193 with recalibrated tests (bundled with BL-053 `CRYPTOPANIC_SCORING_ENABLED` flip if timing aligns).

## 8. Open questions (auto-resolved)

| Q | Resolution |
|---|------------|
| OKX included? | No — deferred. Binance + Bybit cover the deepest alt-perp liquidity. |
| Curated symbol list default empty? | Yes — rely on Binance `!markPrice@arr@1s` for universe coverage; `PERP_SYMBOLS` is additive for targeted OI streams. |
| Block pipeline on watcher warmup? | No — warmup is purely internal to BaselineStore; pipeline is never blocked. |
| Shared DB session with main pipeline? | Yes — same `Database` instance passed in. SQLite handles the write concurrency; watcher writes are rare (only on anomaly). |

## 9. Success criteria

- All 10 tasks land with tests, TDD-green.
- Full suite ≥ 881 passed (BL-053 baseline) + BL-054 additions, zero regressions.
- With `PERP_ENABLED=false`: no new tasks launched, no new DB writes, no scorer behavior change. Provable by: new tests asserting `asyncio.all_tasks()` contains no `perp-watcher` when disabled.
- With `PERP_ENABLED=true`, `PERP_SCORING_ENABLED=false`: anomalies land in DB, candidates are tagged, but quant scores remain bitwise identical to disabled run on the same input.
- With both flags on: `perp_anomaly` appears in `signals_fired` exactly when the documented conditions hold.

---

**Ready for parallel review.** Reviewers: architecture/scope + security/ops.
