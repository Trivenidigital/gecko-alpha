# Early Detection Layer — LunarCrush Integration (Phase 1)

**Date:** 2026-04-09
**Status:** Approved
**Goal:** Detect tokens before they appear on CoinGecko Highlights (Trending Coins + Top Gainers) for manual research decisions.
**Target lead time:** 1-2 hours (even minutes is valuable).
**Budget:** $24/mo (LunarCrush Individual plan).

---

## 1. Architecture Overview

The early detection layer runs **parallel** to the existing pipeline and is fully independent. It can be enabled/disabled via config without affecting the current ingestion → scoring → alert flow.

```
Existing Pipeline (unchanged)
├── CoinGecko/DexScreener/GeckoTerminal → scorer → MiroFish → gate → alert

Early Detection Layer (new, parallel)
├── LunarCrush Poller (every 5 min)
│   ├── Fetch coin social metrics (Galaxy Score, social volume, mentions)
│   └── Detect spikes vs 7-day baseline
├── CoinGecko Trending Snapshotter (every 30 min)
│   └── Record current state of /search/trending
├── Comparison Engine
│   └── Match early signals against trending snapshots
│   └── Compute lead time when a flagged token appears on trending
└── Dashboard Panel
    ├── Live: tokens with rising social signals not yet trending
    ├── Scorecard: hit rate, avg lead time, miss rate
    └── History: timestamped predictions vs actual trending appearances
```

Both loops run inside `scout/main.py` via `asyncio.gather()` alongside the existing pipeline loop.

---

## 2. New Modules

| File | Purpose |
|------|---------|
| `scout/early/__init__.py` | Package init |
| `scout/early/lunarcrush.py` | LunarCrush API client — rate-limited, async |
| `scout/early/tracker.py` | Spike detection logic + trending comparison engine |
| `scout/early/models.py` | `EarlySignal` and `TrendingSnapshot` Pydantic models |
| `scout/db.py` (extend) | New tables: `early_signals`, `trending_snapshots` |
| `scout/main.py` (extend) | Add early detection loop to `asyncio.gather()` |
| `scout/config.py` (extend) | New config knobs |
| `dashboard/api.py` (extend) | New API endpoints for early detection data |
| `dashboard/frontend/components/EarlyDetection.jsx` | New dashboard panel |

### No changes to existing files:
- `scout/scorer.py` — untouched
- `scout/alerter.py` — untouched
- `scout/ingestion/` — untouched
- `scout/gate.py` — untouched
- Existing dashboard panels — untouched

---

## 3. LunarCrush API Integration

### Authentication
- API key passed via `LUNARCRUSH_API_KEY` env var
- Header: `Authorization: Bearer {api_key}`
- Base URL: `https://lunarcrush.com/api4/public`

### Endpoints Used

**Primary: `GET /coins/list/v2`**
Returns all tracked coins with social metrics. We extract:
- `galaxy_score` (0-100) — composite social health score
- `social_volume` — total social posts/mentions in window
- `social_mentions` — mention count
- `sentiment` — positive/negative ratio
- `market_cap`, `price`, `percent_change_24h`
- `symbol`, `name`, `id`

**Secondary: `GET /coins/{coin_id}/time-series/v2`**
Historical social data for baseline calculation (7-day average). Called once per coin when first detected, then cached.

### Rate Limiting
- LunarCrush Individual plan: enforce a conservative 10 req/min soft limit (adjust after observing actual limits)
- Implementation: same pattern as existing `scout/ingestion/coingecko.py` — track call timestamps, enforce limit, exponential backoff on 429
- Since we poll every 5 min and need ~2 calls per cycle, rate limiting is unlikely to be an issue

### Polling Strategy
- Every `LUNARCRUSH_POLL_INTERVAL` seconds (default: 300 = 5 min)
- Fetch full coin list, filter to coins with social_volume > 0
- Compare current social_volume against stored 7-day baseline
- Flag coins where spike_ratio > `SOCIAL_VOLUME_SPIKE_RATIO` (default: 2.0)

---

## 4. Spike Detection Logic

A token is flagged as an early signal when ANY of these conditions are met:

| Condition | Description | Config Key |
|-----------|-------------|------------|
| Social volume spike | `current_social_volume / baseline_7d_avg > 2.0` | `SOCIAL_VOLUME_SPIKE_RATIO` |
| Galaxy Score jump | `current_galaxy_score - previous_galaxy_score > 10` (within 1 hour) | `GALAXY_SCORE_JUMP_THRESHOLD` |
| Mention acceleration | `current_mentions / previous_mentions > 3.0` (30-min window) | `MENTION_ACCEL_THRESHOLD` |

### Baseline Management
- On first poll: store all coin social_volume values as initial baseline
- After 24 hours of polling: compute rolling 7-day average from stored data
- Until 7 days of data: use available history as baseline (minimum 24h before flagging)
- Baseline stored in `early_signals` table, updated each poll cycle

### Deduplication
- A token is only flagged once per spike event
- After flagging, suppress re-flagging for `EARLY_SIGNAL_COOLDOWN` seconds (default: 3600 = 1 hour)
- If social volume drops below 1.5x baseline, cooldown resets (spike ended, new spike can be detected)

---

## 5. CoinGecko Trending Snapshotter

Reuses existing `scout/ingestion/coingecko.py` `fetch_trending()` function. No new API calls needed — just store the results.

- Every `TRENDING_SNAPSHOT_INTERVAL` seconds (default: 1800 = 30 min)
- Call existing `fetch_trending()` 
- Store each coin's `id`, `name`, `symbol`, `rank` with timestamp in `trending_snapshots` table
- Used by comparison engine to determine when a token "appeared on trending"

---

## 6. Comparison Engine

Runs after each trending snapshot. Matching is done by **symbol** (uppercase normalized), since CoinGecko and LunarCrush use different internal coin IDs but share ticker symbols.

For each token in the snapshot:

1. Check if it exists in `early_signals` with `appeared_on_trending_at IS NULL`
2. If found: update `appeared_on_trending_at = now()`, compute `lead_time_minutes = appeared_on_trending_at - detected_at`
3. This is a **hit** — we detected it before CoinGecko showed it

For tokens in `early_signals` older than 4 hours with `appeared_on_trending_at IS NULL`:
- Mark as `expired = True` — this is a **miss** (we flagged it, it never trended)

For tokens in trending snapshot NOT in `early_signals`:
- This is a **gap** — CoinGecko caught it, we didn't. Log for analysis.

### Metrics Computed
- **Hit rate:** `hits / (hits + misses)` — % of our signals that actually trended
- **Coverage:** `hits / (hits + gaps)` — % of trending tokens we caught early
- **Avg lead time:** mean of `lead_time_minutes` for all hits
- **False positive rate:** `misses / (hits + misses)`

---

## 7. Database Schema

### Table: `early_signals`

```sql
CREATE TABLE IF NOT EXISTS early_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin_id TEXT NOT NULL,              -- LunarCrush coin ID
    coin_name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    galaxy_score REAL,
    social_volume REAL,
    social_volume_baseline REAL,        -- 7-day average at time of detection
    spike_ratio REAL,                   -- social_volume / baseline
    spike_type TEXT NOT NULL,           -- 'social_volume' | 'galaxy_score' | 'mention_accel'
    detected_at TEXT NOT NULL,          -- ISO timestamp
    appeared_on_trending_at TEXT,       -- NULL until matched, then ISO timestamp
    lead_time_minutes REAL,            -- computed on match
    expired INTEGER DEFAULT 0,          -- 1 if >4hrs without trending
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_early_signals_symbol ON early_signals(symbol);
CREATE INDEX idx_early_signals_detected ON early_signals(detected_at);
CREATE INDEX idx_early_signals_pending ON early_signals(appeared_on_trending_at) WHERE appeared_on_trending_at IS NULL;
```

### Table: `trending_snapshots`

```sql
CREATE TABLE IF NOT EXISTS trending_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_at TEXT NOT NULL,           -- ISO timestamp
    coin_id TEXT NOT NULL,              -- CoinGecko coin ID
    coin_name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    rank INTEGER,                       -- position in trending list
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_trending_snapshots_at ON trending_snapshots(snapshot_at);
CREATE INDEX idx_trending_snapshots_symbol ON trending_snapshots(symbol);
```

### Table: `social_baselines`

```sql
CREATE TABLE IF NOT EXISTS social_baselines (
    symbol TEXT PRIMARY KEY,
    avg_social_volume REAL NOT NULL,
    avg_galaxy_score REAL NOT NULL,
    sample_count INTEGER NOT NULL,       -- number of data points in average
    last_updated TEXT NOT NULL
);
```

---

## 8. Configuration

New entries in `scout/config.py` Settings class and `.env.example`:

```python
# Early Detection — LunarCrush
LUNARCRUSH_API_KEY: str = ""                    # Empty = early detection disabled
LUNARCRUSH_POLL_INTERVAL: int = 300             # 5 minutes
SOCIAL_VOLUME_SPIKE_RATIO: float = 2.0          # 2x baseline = spike
GALAXY_SCORE_JUMP_THRESHOLD: float = 10.0       # point increase triggers flag
MENTION_ACCEL_THRESHOLD: float = 3.0            # 3x mentions in 30 min
EARLY_SIGNAL_COOLDOWN: int = 3600               # 1 hour suppress re-flag
TRENDING_SNAPSHOT_INTERVAL: int = 1800           # 30 minutes
EARLY_SIGNAL_EXPIRY_HOURS: int = 4              # mark as miss after 4 hours
```

Early detection is **disabled by default** when `LUNARCRUSH_API_KEY` is empty. The existing pipeline is completely unaffected.

---

## 9. Dashboard API Endpoints

New endpoints in `dashboard/api.py`:

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/early/signals` | Active early signals (not yet expired/matched) |
| GET | `/api/early/signals/history` | All signals with match status, paginated |
| GET | `/api/early/metrics` | Hit rate, coverage, avg lead time, false positive rate |
| GET | `/api/early/trending` | Latest trending snapshot |
| GET | `/api/early/gaps` | Tokens that trended but we missed |

---

## 10. Dashboard Frontend

New "Early Detection" tab in the dashboard with these components:

### 10a. Metrics Cards (top row)
- **Hit Rate** — large % with trend arrow
- **Avg Lead Time** — minutes, with min/max range
- **Coverage** — % of trending we caught
- **Active Signals** — count of currently flagged tokens

### 10b. Live Signals Table
Tokens currently flagged, not yet on CoinGecko trending:
- Symbol, Name, Galaxy Score, Social Volume, Spike Ratio, Spike Type, Detected At, Time Since Detection
- Sortable by spike_ratio (highest first)
- Row color: green if <1hr old, yellow if 1-2hr, red if >2hr (going stale)

### 10c. Timeline View
For each matched signal (hit):
- Horizontal bar showing: `[Detected] ----lead time---- [Appeared on Trending]`
- Visual representation of lead time per token

### 10d. Recent History Table
Last 50 signals with columns:
- Symbol, Detected At, Trending At (or "Expired" / "Pending"), Lead Time, Spike Type, Spike Ratio

### 10e. Gaps Table
Tokens that appeared on CoinGecko trending that we did NOT flag:
- Symbol, Name, Appeared At, Notes
- Useful for tuning thresholds

---

## 11. Main Loop Integration

In `scout/main.py`, the early detection loop runs as a separate async task alongside the existing pipeline:

```python
async def main():
    settings = Settings()
    
    async with aiohttp.ClientSession() as session:
        tasks = [pipeline_loop(session, settings)]
        
        # Only start early detection if API key is configured
        if settings.LUNARCRUSH_API_KEY:
            tasks.append(early_detection_loop(session, settings))
        
        await asyncio.gather(*tasks)
```

The `early_detection_loop` handles:
1. LunarCrush polling on its own interval
2. Trending snapshots on its own interval  
3. Comparison after each snapshot
4. All with independent error handling — a LunarCrush failure never affects the main pipeline

---

## 12. Error Handling

- **LunarCrush API down:** Log warning, skip cycle, retry next interval. Never crash the main pipeline.
- **Rate limited (429):** Exponential backoff (5s, 10s, 20s, max 60s). Same pattern as existing CoinGecko client.
- **Invalid API key:** Log error on first call, disable early detection for remainder of session (don't spam failed auth).
- **DB write failure:** Log error, continue polling. Data loss is acceptable for shadow mode.
- **CoinGecko trending fetch fails:** Skip snapshot, try again next interval. Use existing error handling from `coingecko.py`.

---

## 13. Testing Strategy

| Test File | Coverage |
|-----------|----------|
| `tests/test_lunarcrush.py` | API client: auth, rate limiting, response parsing, error handling |
| `tests/test_tracker.py` | Spike detection: threshold logic, baseline management, cooldown, dedup |
| `tests/test_early_comparison.py` | Comparison engine: hit/miss/gap classification, lead time calculation, metrics |
| `tests/test_early_db.py` | DB operations: table creation, insert, query, index usage |
| `tests/test_early_dashboard.py` | Dashboard API endpoints: response format, filtering, pagination |

Mock strategy: `aioresponses` for LunarCrush HTTP mocks (same as existing tests). `tmp_path` for DB fixtures.

---

## 14. Out of Scope (Phase 1)

- **Alerting on early signals** — shadow mode only, no Telegram alerts for early detections (yet)
- **Scoring integration** — early signals do NOT feed into the existing scorer
- **Automated trading** — no execution engine
- **Other social sources** — Santiment, Nansen deferred to Phase 2/3
- **Top Gainers prediction** — Phase 1 focuses on Trending Coins; Top Gainers requires price momentum detection which is a different signal set
