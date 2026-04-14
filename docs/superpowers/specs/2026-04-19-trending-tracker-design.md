# Trending Snapshot Tracker Design

**Date:** 2026-04-19
**Status:** Implementing
**Goal:** Validate gecko-alpha's core promise -- did we catch tokens BEFORE they appeared on CoinGecko Trending?

## Problem

The pipeline detects early pump candidates via multiple signals (DexScreener, GeckoTerminal, CoinGecko markets, narrative rotation). But we have no way to measure: **did we see it first?**

CoinGecko's `/search/trending` endpoint is the industry benchmark for "this token is hot right now." If our system consistently identifies tokens *before* they trend on CoinGecko, that validates the entire approach.

## Architecture

### New module: `scout/trending/`

```
scout/trending/
  __init__.py
  tracker.py      # Snapshot + comparison logic
  models.py       # TrendingSnapshot, TrendingComparison, TrendingStats
```

### Data Flow

1. **Snapshot** (every 30 min, in narrative_agent_loop OBSERVE phase):
   - Fetch `/search/trending` via existing `_get_with_backoff` (shared rate limiter)
   - Store each coin in `trending_snapshots` table

2. **Compare** (every 6h, in narrative_agent_loop EVALUATE phase):
   - For each distinct coin that appeared on trending in last 24h
   - Check `predictions` table (narrative agent picks)
   - Check `candidates` table (pipeline candidates)
   - Check `signal_events` table (chain signal events)
   - Compute lead_minutes = trending_appeared_at - earliest_detection_at
   - Store results in `trending_comparisons` table

3. **Stats** (on-demand via API):
   - Total trending tokens tracked
   - Caught before trending (is_gap=0): count + percentage
   - Missed (is_gap=1): count
   - Average lead time for catches

### DB Schema

```sql
CREATE TABLE IF NOT EXISTS trending_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    market_cap_rank INTEGER,
    trending_score REAL,
    snapshot_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_trending_snap
    ON trending_snapshots(coin_id, snapshot_at);

CREATE TABLE IF NOT EXISTS trending_comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    appeared_on_trending_at TEXT NOT NULL,
    detected_by_narrative INTEGER DEFAULT 0,
    narrative_detected_at TEXT,
    narrative_lead_minutes REAL,
    detected_by_pipeline INTEGER DEFAULT 0,
    pipeline_detected_at TEXT,
    pipeline_lead_minutes REAL,
    detected_by_chains INTEGER DEFAULT 0,
    chains_detected_at TEXT,
    chains_lead_minutes REAL,
    is_gap INTEGER DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_trending_comp
    ON trending_comparisons(coin_id);
```

### Config Knobs

| Key | Default | Description |
|-----|---------|-------------|
| `TRENDING_SNAPSHOT_ENABLED` | `True` | Master kill switch |
| `TRENDING_COMPARISON_INTERVAL` | `21600` | How often to run comparison (6h) |

### Dashboard API

- `GET /api/trending/snapshots` -- recent trending tokens
- `GET /api/trending/stats` -- hit rate, lead time, misses
- `GET /api/trending/comparisons` -- detailed comparison table

### Main Loop Integration

- Snapshot: runs in `narrative_agent_loop` OBSERVE phase (every 30 min)
- Comparison: runs in `narrative_agent_loop` EVALUATE phase (every 6h)
- Both gated by `TRENDING_SNAPSHOT_ENABLED` config

## Success Criteria

- Snapshot data accumulates every 30 min
- Comparisons correctly identify early detections
- Dashboard shows hit rate, lead time, gaps
- Full test coverage for tracker logic
- No regression in existing tests
