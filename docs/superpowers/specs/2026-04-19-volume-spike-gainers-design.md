# Volume Spike Detector + Top Gainers Tracker

**Date:** 2026-04-19
**Status:** Implemented

## Overview

Two new features that leverage existing CoinGecko `/coins/markets` data from the pipeline (zero extra API calls):

1. **Volume Spike Detector** -- Tracks volume history per coin and detects breakouts where current volume exceeds the 7-day average by a configurable ratio (default 5x).

2. **Top Gainers Tracker** -- Snapshots tokens with >20% 24h gains and compares them against our prior detections (narrative, pipeline, chains, and volume spikes) to measure lead time.

## Architecture

### Volume Spike Detector

- **Module:** `scout/spikes/` (detector.py, models.py)
- **DB Tables:** `volume_history_cg`, `volume_spikes`
- **Config:** `VOLUME_SPIKE_ENABLED`, `VOLUME_SPIKE_RATIO`, `VOLUME_SPIKE_MAX_MCAP`
- **Data Source:** `_cg_module.last_raw_markets` (already fetched each cycle)
- **Integration:** Runs in `run_cycle()` after `cache_prices`, before aggregation
- **Dashboard:** `/api/spikes/recent`, `/api/spikes/stats`

### Top Gainers Tracker

- **Module:** `scout/gainers/` (tracker.py)
- **DB Tables:** `gainers_snapshots`, `gainers_comparisons`
- **Config:** `GAINERS_TRACKER_ENABLED`, `GAINERS_MIN_CHANGE`, `GAINERS_MAX_MCAP`
- **Data Source:** `_cg_module.last_raw_markets` (already fetched each cycle)
- **Integration:** Snapshots stored in `run_cycle()`, comparisons computed during EVALUATE interval (every 6h)
- **Dashboard:** `/api/gainers/snapshots`, `/api/gainers/comparisons`, `/api/gainers/stats`

## Detection Logic

### Volume Spikes

```
For each coin in latest volume_history_cg:
  avg_vol = AVG(volume_24h) from last 7 days
  if current_volume / avg_vol > VOLUME_SPIKE_RATIO
     AND market_cap < VOLUME_SPIKE_MAX_MCAP
     AND market_cap > 0:
    -> Insert into volume_spikes (dedup by coin_id + date)
```

### Gainers Comparison

Same pattern as `trending/tracker.py`:
- For each token on the top gainers list in the last 24h
- Check predictions, candidates, signal_events, and volume_spikes tables
- Compute lead time in minutes
- Track hit rate (caught vs missed)

## Config Defaults

| Key | Default | Description |
|-----|---------|-------------|
| VOLUME_SPIKE_ENABLED | true | Kill switch |
| VOLUME_SPIKE_RATIO | 5.0 | Min ratio for spike detection |
| VOLUME_SPIKE_MAX_MCAP | 500M | Max market cap filter |
| GAINERS_TRACKER_ENABLED | true | Kill switch |
| GAINERS_MIN_CHANGE | 20.0 | Min 24h% to qualify as gainer |
| GAINERS_MAX_MCAP | 500M | Max market cap filter |
