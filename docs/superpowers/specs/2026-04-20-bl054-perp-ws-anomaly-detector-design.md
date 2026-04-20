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
  baseline.py     # Rolling EWMA baseline store (pure, in-memory, LRU-capped)
  anomaly.py      # Pure functions: classify_funding_flip, classify_oi_spike
  binance.py      # Binance WS client + parser
  bybit.py        # Bybit WS client + parser
  enrichment.py   # enrich_candidates_with_perp_anomalies (DB read, pure-ish)
  watcher.py      # Long-lived async task: connect, parse, classify, persist
```

**Enrichment lives in `scout/perp/enrichment.py`** (NOT co-located in `watcher.py` and NOT in `scout/perp/__init__.py`). This matches the BL-053 pattern of keeping the pipeline-facing helper separable from the streaming watcher.

**Dependency constraint:** the WebSocket implementation MUST use `aiohttp.ClientSession.ws_connect`. No new third-party libraries (no `websockets`, `pybit`, or similar). Any such addition is out of scope and requires its own RFC.

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
from pydantic import BaseModel, field_validator

AnomalyKind = Literal["funding_flip", "oi_spike"]
Exchange = Literal["binance", "bybit"]    # intentionally narrow; widen to str
                                          # when OKX (or another source) lands

_TICKER_RE = re.compile(r"^[A-Z0-9]{1,20}$")   # anti-injection + log hygiene

class PerpTick(BaseModel):
    exchange: Exchange
    symbol: str            # e.g. "BTCUSDT", "DOGEUSDT" — exchange-native
    ticker: str            # normalized base asset, upper-case: "BTC", "DOGE"
    funding_rate: float | None = None
    mark_price: float | None = None
    open_interest: float | None = None   # contracts (count), not USD
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
    ticker: str            # normalized
    kind: AnomalyKind
    magnitude: float       # funding_pct for flip; ratio_to_baseline for spike
    baseline: float | None # baseline value at detection time
    observed_at: datetime
```

**Ticker-normalization rules** (MUST be test-covered in `tests/perp/test_normalize.py`):
- Strip `USDT` / `USDC` / `USD` / `BUSD` / `-PERP` suffix (case-sensitive). Linear (`USDT`/`USDC`) and inverse (`USD`) perps both collapse to the base asset — enrichment is a ticker match, and the base asset is what matters. Inverse perps have distinct funding mechanics, but for candidate-token enrichment the base-asset collapse is the correct decision.
- Strip leading `1000` (Binance convention for `1000PEPEUSDT` etc.) and remember the multiplier is NOT applied to the ticker — it's a display-only cosmetic.
- Upper-case.
- If the normalized result fails `_TICKER_RE`, drop the tick (log + counter increment, do NOT raise).
- Fixture-driven table test: at least `BTCUSDT → BTC`, `BTCUSDC → BTC`, `1000PEPEUSDT → PEPE`, `DOGEUSD → DOGE` (inverse collapse), `ETH-PERP → ETH`, malformed input (`"../etc"`, `"SPACE SYM"`, 33-char string) → dropped.

### 3.2 `scout/perp/baseline.py`

In-memory, per-symbol, EWMA baselines. Pure functions. Keyed by `(exchange, symbol)`. No DB — baseline is rebuilt on restart from the first 15 minutes of ticks.

```python
class BaselineStore:
    def __init__(
        self,
        *,
        alpha: float = 0.1,
        max_keys: int = 1000,
        idle_evict_seconds: int = 3600,
    ): ...
    def update(self, key: tuple[str, str], oi: float | None, funding: float | None) -> None: ...
    def oi_baseline(self, key: tuple[str, str]) -> float | None: ...
    def funding_baseline(self, key: tuple[str, str]) -> float | None: ...
    def sample_count(self, key: tuple[str, str]) -> int: ...
    def evict_idle(self, now: datetime) -> int: ...   # returns #evicted
```

Warmup gate: no anomaly fires until `sample_count >= PERP_BASELINE_MIN_SAMPLES` (default 30 ≈ 15 min of 30s samples).

**Bounded growth contract** (anti-memory-leak):
- `max_keys` is a hard cap. When full, `update()` for a new key evicts the oldest-touched key (LRU).
- `evict_idle(now)` is called periodically from the watcher maintenance loop (every 5 min) and removes keys not touched in the last `idle_evict_seconds`. Default `idle_evict_seconds = 3600` — a symbol with no tick for an hour has either been delisted or fallen off the markPrice stream and can safely cold-start next time.
- Both eviction paths MUST be test-covered.
- Memory math: 1000 keys × (2 floats + 1 int + 1 last-seen timestamp) ≈ 40 KB worst case. Negligible, but the guarantee is what matters — we do not want to discover per-symbol state has accumulated over a 6-month service lifetime.

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
- JSON parse + validate (malformed frames drop with a rate-limited warning — see "Log-volume contract" below)
- **Ping format is exchange-specific.** Binance: the server sends ping frames; we MUST reply with pong — no outbound ping needed. Bybit: client MUST send JSON `{"op":"ping"}` every 20s and verify `{"op":"pong"}` echo. These are NOT a shared helper; each client owns its own keepalive.
- Reconnect with **full-jitter exponential backoff with floor**: `delay = random.uniform(0.5, min(60, 2**attempt))`. This prevents lockstep reconnect storms between the two exchanges after a shared network blip; the 0.5s floor prevents a sub-ms reconnect from hammering a momentarily-recovering gateway. Unlimited retries are bounded by the supervisor's `PERP_MAX_CONSECUTIVE_RESTARTS` budget (see §3.5).
- Yield normalized `PerpTick` objects. Raw message errors are logged + dropped; never raise.

**Log-volume contract.** Reconnect-log storms and malformed-frame bursts are real risks. Each client maintains a per-event-class counter flushed every 60 s via structlog: `"perp_client: dropped N frames in last 60s"`, `"perp_client: reconnected N times in last 60s"`. Individual events below the aggregation threshold are NOT logged. This matches the existing heartbeat convention.

### 3.5 `scout/perp/watcher.py`

Long-lived async task, one per exchange, plus a top-level supervisor:

```python
async def run_perp_watcher(session, db, settings) -> None:
    # Fan out Binance + Bybit. Each is a self-healing infinite loop.
    # Both feeds converge into one BaselineStore + one anomaly pipeline.
    # Parser task(s) push PerpTicks into a bounded asyncio.Queue; a single
    # classifier task drains the queue. This decouples network timing from
    # SQLite + classifier latency and bounds worst-case memory.
    ...
```

**Backpressure contract (required).** `!markPrice@arr@1s` ships the entire Binance perp universe (~400 symbols) every second. Without backpressure, a classifier slowdown would pile ticks into unbounded parser buffers until the exchange force-closes the connection.

- A single `asyncio.Queue(maxsize=PERP_QUEUE_MAXSIZE)` (default 2048) sits between parser and classifier.
- Parser uses `queue.put_nowait()` with a drop-oldest policy on `QueueFull`: pop one and retry-put; increment a `dropped_ticks` counter. Drop-oldest is correct for BOTH stream types we consume: `!markPrice@arr` frames are replacement-snapshots (newer frame supersedes older for the same symbol), and `@openInterest` frames report the current-value OI (again a snapshot — not a delta). Dropping oldest preserves the freshest baseline/comparison input in both cases; dropping newest would leave the classifier processing stale state while fresh data sits unread. The parser MUST include an inline comment stating "OI and markPrice frames are snapshots of current value, not deltas; drop-oldest is safe."
- Dropped counter flushed to a structlog aggregate every 60 s along with queue-depth high-water-mark.
- A classifier coroutine drains continuously; it is the ONLY writer to BaselineStore.

**Tick-processing loop (classifier task):**
1. `await queue.get()`.
2. Update BaselineStore.
3. Run both classifiers.
4. For any non-None anomaly: debounce per `(exchange, symbol, kind)` via in-memory cooldown (`PERP_ANOMALY_DEDUP_MIN`), append to an in-memory batch list.
5. Every `PERP_DB_FLUSH_INTERVAL_SEC` (default 2s) OR when batch length exceeds `PERP_DB_FLUSH_MAX_ROWS` (default 100), flush the batch via `db.insert_perp_anomalies_batch(rows)` (single transaction). The watcher MUST NOT hold the aiosqlite connection during classifier work.
6. Every 5 minutes, call `baseline.evict_idle(now)`.

**Debounce caveat (ack).** In-memory cooldown is reset on process restart; a restart within `PERP_ANOMALY_DEDUP_MIN` of an anomaly could double-write. Given 5-min granularity and rare restarts, this is acceptable — but it's an acknowledged failure mode, not an oversight.

**Supervisor + circuit-breaker.** The supervisor catches every exception on an inner exchange task and restarts after a full-jitter exponential backoff (floor 0.5s, cap 60s). If `PERP_MAX_CONSECUTIVE_RESTARTS` is breached **for a given exchange**, that exchange's inner task enters a **cooldown circuit-break** for `PERP_CIRCUIT_BREAK_SEC` (default 3600 s = 1 h) before retrying; it does NOT require a process restart to recover. The OTHER exchange's task is unaffected. The pipeline's main loop is unaffected in all cases (same isolation precedent as `LUNARCRUSH_MAX_CONSECUTIVE_RESTARTS`).

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
- `insert_perp_anomaly(anomaly: PerpAnomaly) -> None`  — single-row fallback (kept for tests)
- `insert_perp_anomalies_batch(rows: list[PerpAnomaly]) -> int` — **primary write path**, single transaction, returns row count
- `fetch_recent_perp_anomalies(*, tickers: list[str], since: datetime) -> list[PerpAnomaly]`
- `prune_perp_anomalies(*, keep_days: int) -> int`

SQLite WAL mode is already enabled at DB-open (`scout/db.py` sets `journal_mode=WAL`), so watcher batch writes do not block main-pipeline readers.

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

`enrich_candidates_with_perp_anomalies` is a new helper in `scout/perp/enrichment.py`. It does a single SQL fetch for the union of candidate tickers, builds an in-memory index, and sets the four candidate fields (`perp_funding_flip`, `perp_oi_spike_ratio`, `perp_last_anomaly_at`, `perp_exchange`). Pure-ish (still reads DB); no network.

Prune hook added to the same hourly maintenance block used for `cryptopanic_posts`.

### 3.9 `scout/scorer.py` — Signal 14

**Scoring-math contract (blocking-fix B1 from design review).** `SCORER_MAX_RAW` stays at 183 in this PR, but leaving `PERP_SCORING_ENABLED=true` without a recalibrated denominator would silently inflate scores in the feature-on cohort and distort the quant/narrative weighting in `gate.py`. This is the same latent trap BL-053 shipped with `CRYPTOPANIC_SCORING_ENABLED`. To prevent quietly doubling it, the scorer MUST refuse to fire Signal 14 unless `SCORER_MAX_RAW` has been bumped in a subsequent recalibration PR:

```python
# Signal 14: Perp anomaly (BL-054) -- 10 points, gated.
# This block is a NO-OP until a follow-up PR lands that (a) bumps
# SCORER_MAX_RAW to at least 203 (183 + 10 BL-053 + 10 BL-054), and
# (b) recalibrates co-occurrence thresholds + test fixtures. The runtime
# guard below enforces that contract: flipping PERP_SCORING_ENABLED=true
# before the recalibration PR does NOT change scorer output. The alert
# path (enrichment + tagging) still runs independently for shadow data.
_PERP_SCORING_DENOMINATOR_READY = SCORER_MAX_RAW >= 203
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

Tests MUST cover: flag-on + denominator-not-ready = signal never fires; flag-on + denominator-ready (monkeypatch the constant in the test) = signal fires under documented conditions.

### 3.10 `scout/config.py` additions

```python
# -------- Perp WebSocket Anomaly Detector (BL-054) --------
PERP_ENABLED: bool = False
PERP_SCORING_ENABLED: bool = False   # scorer signal kill-switch (separate)
PERP_BINANCE_ENABLED: bool = True
PERP_BYBIT_ENABLED: bool = True
PERP_SYMBOLS: list[str] = []          # curated list, empty = markPrice-arr only
# Funding-flip gate: 0.05% default. Binance funding is 8h-settled and 0.01%
# is too permissive for shadow-period calibration; real regime-change ticks
# at 0.05–0.1%. Post-shadow tuning may tighten or loosen.
PERP_FUNDING_FLIP_MIN_PCT: float = 0.05
PERP_OI_SPIKE_RATIO: float = 3.0
PERP_BASELINE_ALPHA: float = 0.1
PERP_BASELINE_MIN_SAMPLES: int = 30       # ~15 min at 30s cadence
PERP_BASELINE_MAX_KEYS: int = 1000        # LRU cap; see §3.2
PERP_BASELINE_IDLE_EVICT_SEC: int = 3600  # prune symbols idle > 1h
PERP_ANOMALY_LOOKBACK_MIN: int = 15
PERP_ANOMALY_DEDUP_MIN: int = 5
PERP_ANOMALY_RETENTION_DAYS: int = 7
PERP_MAX_CONSECUTIVE_RESTARTS: int = 5
PERP_CIRCUIT_BREAK_SEC: int = 3600        # cooldown when restart budget exhausted
PERP_WS_PING_INTERVAL_SEC: int = 20
PERP_WS_RECONNECT_MAX_SEC: int = 60
PERP_QUEUE_MAXSIZE: int = 2048            # bounded parser→classifier queue
PERP_DB_FLUSH_INTERVAL_SEC: float = 2.0   # batched anomaly writes
PERP_DB_FLUSH_MAX_ROWS: int = 100
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
| `PERP_SCORING_ENABLED=true` without SCORER_MAX_RAW bump | scorer runtime-guard refuses to fire signal; logs once; no score inflation |
| Classifier backlog > `PERP_QUEUE_MAXSIZE` | parser drops oldest tick, increments drop counter; never blocks; never raises |
| Exchange restart budget exhausted | that exchange's inner task parks in circuit-break; other exchange + main pipeline unaffected; automatic recovery after `PERP_CIRCUIT_BREAK_SEC` |
| Hostile symbol/ticker from exchange | Pydantic validator drops the tick (log + counter); classifier never sees it |

## 5. Testing

Every public surface gets a corresponding test. Test categories:

1. **Pure classifier tests** (no I/O): funding flip with/without magnitude gate, OI spike with warmup / baseline / ratio gate edge cases.
2. **Baseline store tests**: EWMA math for N samples, warmup gate, no-mutation contract, LRU eviction at `max_keys`, idle eviction by `evict_idle()`.
3. **Ticker-normalization tests** (`tests/perp/test_normalize.py`): table-driven. `BTCUSDT → BTC`, `1000PEPEUSDT → PEPE`, `DOGEUSD → DOGE`, `ETH-PERP → ETH`, malformed/hostile input (`"../etc/passwd"`, `"SYMBOL WITH SPACE"`, 33-char string) → dropped with counter incremented.
4. **Binance/Bybit parser tests**: real payload fixtures committed to `tests/fixtures/perp/`, assert `PerpTick` shape.
5. **WS client tests** using a hand-rolled async mock (aioresponses does NOT mock WebSockets): subscribe handshake, pings (Binance pong-on-ping, Bybit outbound `{"op":"ping"}`), reconnect-after-close with jitter; max one dedicated slow test marked `@pytest.mark.slow`.
6. **Watcher integration test**: feed synthetic `PerpTick` stream into classifier pipeline, assert DB row count, cooldown enforcement, batched-write flush behavior (flush-on-interval AND flush-on-batch-size).
7. **Backpressure test**: push `PERP_QUEUE_MAXSIZE + 100` ticks before the classifier task can consume, assert `dropped_ticks` counter == 100 AND the freshest tick is NOT dropped.
8. **Circuit-breaker test**: force `PERP_MAX_CONSECUTIVE_RESTARTS + 1` exceptions on one exchange's inner task, assert that exchange parks in circuit-break AND the other exchange continues AND the pipeline's main loop is unaffected AND recovery after `PERP_CIRCUIT_BREAK_SEC` (time-mocked). The supervisor MUST read its sleep duration via an injectable clock (default `asyncio.sleep`) so the test can fast-forward without real 3600s waits.
9. **DB tests**: insert/fetch/prune round-trip, batch insert, index presence, cross-ticker query correctness.
10. **`main.py` integration tests** mirroring `test_main_cryptopanic_integration.py` shape: enabled path, disabled path, enrichment fields populated, scoring signal fires only when both flags set AND `SCORER_MAX_RAW` bumped (monkeypatch), watcher-task-never-launched-when-disabled.
11. **Scorer tests**: signal fires on both funding_flip and oi_spike conditions **when monkeypatched `SCORER_MAX_RAW >= 203`**; does NOT fire when `PERP_SCORING_ENABLED=false`; does NOT fire when `_PERP_SCORING_DENOMINATOR_READY=False`; does NOT fire when `perp_last_anomaly_at` is None; fires correctly under co-occurrence counting.
12. **Flag-off provability snapshot** (explicit contract from Section 9 success criteria): fixed candidate-token corpus (fixture) that MUST include ≥ 1 token with `perp_last_anomaly_at` populated and a `perp_oi_spike_ratio` above threshold so the assertion actually bites (an all-None corpus makes the test vacuous). Run scorer with `PERP_ENABLED=true, PERP_SCORING_ENABLED=false` vs fully-disabled, assert `(raw_score, signals_fired)` tuple is byte-identical across both runs. This is the test that proves the shadow path cannot contaminate scoring.
13. **Regression**: full suite continues to pass. Target: `881 passed, 1 skipped` plus BL-054 additions.

## 6. Delivery milestones (map to plan tasks)

| # | Task | Files | Notes |
|---|------|-------|-------|
| 1 | Config knobs + schemas + ticker normalizer | `config.py`, `perp/schemas.py`, `perp/normalize.py`, `perp/__init__.py` | types + validators first |
| 2 | Classifier + baseline pure functions (with LRU/idle evict) | `perp/anomaly.py`, `perp/baseline.py` | TDD |
| 3 | Binance parser + WS client (pong-on-ping, jitter reconnect) | `perp/binance.py` | uses `aiohttp.ClientSession.ws_connect` |
| 4 | Bybit parser + WS client (op:ping, jitter reconnect) | `perp/bybit.py` | same shape; different keepalive |
| 5 | DB layer (single + batch insert, fetch, prune, indexes) | `db.py` | WAL already on |
| 6 | CandidateToken fields + enrichment helper | `models.py`, `perp/enrichment.py` | |
| 7 | Scorer Signal 14 with denominator-ready guard | `scorer.py` | SCORER_MAX_RAW unchanged; runtime guard enforces recalibration gate |
| 8 | Main pipeline wiring (task launch + stage 2.5 + prune) | `main.py` | background task + enrichment hook |
| 9 | Watcher supervisor + circuit-breaker + backpressure queue + batched DB flush + heartbeat shadow stats | `perp/watcher.py` | ties it all together |
| 10 | Flag-off snapshot test + docs / README stub + regression sweep | `tests/perp/test_flag_off_snapshot.py`, `README.md` | locks the contract |

Frequent-commit rhythm: one commit per task, per the plan template.

## 7. Rollout

- Ship with `PERP_ENABLED=false`, `PERP_SCORING_ENABLED=false`.
- Merge is allowed (research-only, no deployment impact).
- Operator flips `PERP_ENABLED=true` on VPS to start collecting data (zero scoring impact).
- **Shadow observability (required before flag flip).** Operator reads shadow data via two surfaces:
  1. Ad-hoc SQL: `SELECT exchange, COUNT(*), COUNT(DISTINCT ticker) FROM perp_anomalies WHERE observed_at > datetime('now','-1 day') GROUP BY exchange;`
  2. Heartbeat-log counter: watcher emits a structlog line every heartbeat interval (`perp_shadow_stats: binance_anomalies_24h=N bybit_anomalies_24h=M unique_tickers=K baseline_warm=X/Y queue_high_water=Z dropped_ticks_24h=W`). This makes "am I collecting?" answerable without a DB query.
- **Scoring-flag flip is a separate PR that cannot be bypassed** (enforced by the runtime guard in §3.9). That PR:
  1. Bumps `SCORER_MAX_RAW` from 183 → 203 (+10 for BL-053 cryptopanic, +10 for BL-054 perp).
  2. Bundles BOTH `CRYPTOPANIC_SCORING_ENABLED=true` and `PERP_SCORING_ENABLED=true` flips so the distribution-shift review is single-pass.
  3. Recalibrates co-occurrence thresholds + test fixtures.
  4. Ships after ≥ 7 days of shadow data confirms non-trivial signal coverage.

## 8. Open questions (auto-resolved)

| Q | Resolution |
|---|------------|
| OKX included? | No — deferred. Binance + Bybit cover the deepest alt-perp liquidity. |
| Curated symbol list default empty? | Yes — rely on Binance `!markPrice@arr@1s` for universe coverage; `PERP_SYMBOLS` is additive for targeted OI streams. |
| Block pipeline on watcher warmup? | No — warmup is purely internal to BaselineStore; pipeline is never blocked. |
| Shared DB session with main pipeline? | Yes — same `Database` instance passed in. SQLite WAL (already enabled) + batched anomaly writes (§3.5) + per-flush transactions keep the classifier hot-path off the main-pipeline writer. |
| What if both exchange flags go to `false`? | Supervisor logs a clear warning at startup and refuses to launch any inner task; enrichment becomes a no-op; pipeline scores unchanged. |
| Can the scoring flag be flipped independently of the SCORER_MAX_RAW bump? | No — the runtime guard in §3.9 enforces that. The design is deliberately tamper-resistant. |

## 9. Success criteria

- All 10 tasks land with tests, TDD-green.
- Full suite ≥ 881 passed (BL-053 baseline) + BL-054 additions, zero regressions.
- With `PERP_ENABLED=false`: no new tasks launched, no new DB writes, no scorer behavior change. Provable by: new tests asserting `asyncio.all_tasks()` contains no `perp-watcher` when disabled.
- With `PERP_ENABLED=true`, `PERP_SCORING_ENABLED=false`: anomalies land in DB, candidates are tagged, but `(raw_score, signals_fired)` is byte-identical to the disabled run on the same input corpus. Proven by the snapshot test in Section 5 item 12.
- With both flags on AND `SCORER_MAX_RAW >= 203`: `perp_anomaly` appears in `signals_fired` exactly when the documented conditions hold.
- With both flags on AND `SCORER_MAX_RAW < 203`: runtime guard refuses to fire; scorer output identical to shadow mode. Proven by Section 5 item 11.

---

**Ready for parallel review.** Reviewers: architecture/scope + security/ops.
