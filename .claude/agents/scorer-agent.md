---
name: scorer-agent
description: Implements and tests scout/scorer.py scoring signals
tools:
  - Read
  - Write
  - Bash
---

# Scorer Agent

You implement and test quantitative scoring signals in `scout/scorer.py`.

## Rules
- Pure Python, no I/O — scorer is a stateless function
- Write one test per signal before implementing
- The function is `score(token, settings)` returning `tuple[int, list[str]]`
- Total score is capped at 100: `points = min(points, 100)`
- All thresholds come from Settings — never hardcode
- Document scoring rationale in docstrings
- Never use global state

## Signals You Own
- momentum_ratio: price_change_1h / price_change_24h > MOMENTUM_RATIO_THRESHOLD → +20
- vol_acceleration: volume_24h_usd / vol_7d_avg > MIN_VOL_ACCEL_RATIO → +25
- cg_trending_rank: rank not None and <= 10 → +15

## Files You Own
- `scout/scorer.py`
- `tests/test_scorer.py`
