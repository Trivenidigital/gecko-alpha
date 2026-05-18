**New primitives introduced:** Extends `scout/ingestion/held_position_prices.py` with: (1) `stale_open_count` + `stale_open_pct` gauge fields in the existing `held_position_refresh_summary` structlog event, (2) per-token `held_position_token_persistently_stale` WARN-level structured log when a held token has been cache-stale ≥ `HELD_POSITION_STALE_WARN_HOURS` (default 24 to align with gauge threshold), (3) module-level `_get_cached_price_ages()` SQL helper (direct query of `price_cache.updated_at`; avoids touching `db.py`), (4) `_reset_warned_today_for_tests()` mirror of existing `_reset_cycle_counter_for_tests()` (test isolation), (5) 1 new `Settings` key `HELD_POSITION_STALE_WARN_HOURS`. **Task 4 (`/coins/{id}` fallback) descoped from this PR** pending empirical verification — see post-reviewer scope-cut below. No DB schema changes. No new alert paths. No `db.py` changes.

# BL-NEW-HELD-POSITION-REFRESH-RATE-GAP Implementation Plan (v2 — post-2-reviewer fold)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans.

**Goal:** Address the 14% silent miss-rate (21/148 open paper_trades with `price_cache` rows > 24h stale) surfaced by 2026-05-18 audit. Trailing-stop / peak-fade evaluators can't fire correctly on stale prices. Ship **visibility-first** (gauge + per-token WARN) so operator sees the gap in journalctl; defer the `/coins/{id}` fallback until empirically verified that it recovers data.

**Architecture:** Extend existing `scout/ingestion/held_position_prices.py` (240 LOC; shipped 2026-05-12 via PR #112). Diagnosis below confirms root cause is **stale-source behavior** (the 21 stale tokens are CG-lane-EXCLUSIVE — 0/21 appear in gainers_snapshots or trending_snapshots over last 24h despite 4617+645 total entries; held-position lane is their sole refresher; CG `/simple/price` apparently returns no data for them).

**Tech Stack:** Python 3.11, aiohttp, structlog, aiosqlite. pytest-asyncio + aioresponses (project convention per existing test file). No new deps.

## v2 fold summary (post-2-reviewer fold)

| Finding | Severity | Resolution |
|---|---|---|
| R1 #1 / R2 #1 CRITICAL — `existing[tid]["updated_at"]` KeyError (`get_cached_prices` doesn't return `updated_at`) | CRITICAL | New helper `_get_cached_price_ages(db, coin_ids) -> dict[str, datetime]` doing direct `SELECT coin_id, updated_at FROM price_cache WHERE coin_id IN (...)`. Avoids touching `db.py`. |
| R2 #2 CRITICAL — `parse_iso(...)` is not a real symbol | CRITICAL | Use `datetime.fromisoformat(...)` from stdlib; add `from datetime import datetime, timezone` to module imports. |
| R1 #1 CRITICAL (separate) — "Stale-source" diagnosis insufficiently evidenced | CRITICAL | **Empirically verified post-review**: 0/21 stale tokens appear in `gainers_snapshots` (4617 entries 24h) or `trending_snapshots` (645 entries 24h). They are CG-lane-EXCLUSIVE — the held-position lane is their sole refresher. Other hypotheses (rate-limit-truncation, ordering, filtering, failed writes) don't fit. |
| R1 #4 IMPORTANT — `/coins/{id}` fallback efficacy unverified | IMPORTANT | **TASK 4 DESCOPED from this PR.** Direct curl of `/coins/pythia` returned HTTP 429 (srilu hit free-tier rate limit). Cannot verify fallback would recover the stale tokens. Defer to evidence-gated follow-up `BL-NEW-HELD-POSITION-FALLBACK-COINS-ENDPOINT` (re-eval when CG rate-limit subsides + we can manual-curl 5 of the 21 stale ids). Shipping visibility-first lets operator confirm the per-token list + ship fallback only if proven useful. |
| R1 #3 / R1 #6 IMPORTANT — `existing` widened to `held_ids`; threshold-alert for §12a | IMPORTANT | (a) `_get_cached_price_ages` queries on `held_ids` (not just `raw_coins`). (b) Defer the auto-alert as `BL-NEW-HELD-POSITION-STALE-COUNT-ALERT` evidence-gated follow-up — minimal-blast-radius ship today (visibility gauge); add alert when baseline measured + threshold proven. |
| R2 #3 IMPORTANT — Sequential 21-call fallback hammers CG rate-limit | IMPORTANT | Moot since Task 4 descoped. If/when re-scoped: cap `MAX_FALLBACK_PER_CYCLE=5` + `coingecko_limiter.is_backing_off()` check before each call. |
| R2 #4 IMPORTANT — `_warned_today` lacks test-reset helper | IMPORTANT | Add `_reset_warned_today_for_tests()` + autouse fixture. |
| R2 #16 IMPORTANT — `stale_open_count` outside try/except risks log crash | IMPORTANT | Wrap stale-count + per-token-warn computation in their own try/except; default `stale_open_count=None` on failure with `logger.exception("held_position_stale_count_failed")`. Existing summary log still emits cleanly. |
| R1 #5 IMPORTANT — `HELD_POSITION_STALE_WARN_HOURS=48` misses 24-48h bucket | IMPORTANT | Lower default to 24 (aligns with `stale_open_count` threshold so both surfaces share semantics). |
| R1 #2 IMPORTANT — `/coins/{id}` returns market_data shape | N/A | Task 4 descoped. |
| R2 #6 MINOR — Tests use `aioresponses` pattern | MINOR | Confirmed; tests will follow existing pattern at `tests/test_held_position_prices.py:12`. |
| R1 #8 MINOR — In-memory `_warned_today` survives restarts | MINOR | Acceptable per memory `feedback_in_memory_telemetry_persistence.md`; pipeline restart cadence is weekly-deploy-driven (low). |

**Hermes-first analysis:**

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Per-token CG price refresh for project SQLite cache | None (awesome-hermes-agent: `x-twitter-scraper` + `mercury` + `hermes-blockchain-oracle`; none write to project SQLite) | Build in-tree extension |
| Operator-visible stale-cache metric | None | structlog gauge field on existing event (additive) |

awesome-hermes-agent reachable per cycle-12 PR #152 fold; no relevant primitive applies.

**Drift-check:** worktree HEAD = `cdeb31f` = origin/master (zero divergence). Grep for `held_position_refresh|fetch_held_position_prices` returns 4 files (module + caller + Settings + tests). No parallel session.

## Confirmed root-cause diagnosis

| Hypothesis | Empirical fit | Verdict |
|---|---|---|
| Refresh interval too long | 127/148 fresh ≤24h at `INTERVAL_CYCLES=1` | NO |
| LIFO/FIFO ordering starvation | Lane uses `SELECT DISTINCT`; no ordering | NO |
| Rate-limiter contention | Would affect ALL tokens; 85% are fresh | NO |
| Token filtering | Stale tokens ARE CG-shaped | NO |
| Failed writes | Would be random, not same-21 across days | NO |
| **Stale-source (CG `/simple/price` returns empty for these tokens)** | **0/21 in gainers_snapshots OR trending_snapshots over last 24h. These tokens are CG-lane-EXCLUSIVE; held-position lane is their sole refresher; CG no longer returns data for them.** | **YES — empirically confirmed** |

## Files to create / modify

### Create
- `tasks/findings_held_position_refresh_rate_gap_2026_05_18.md` — empirical diagnosis + post-deploy soak plan + Task 4 deferral rationale

### Modify
- `scout/ingestion/held_position_prices.py` — additive: gauge field, per-token warn (with dedup), Settings reads, `_get_cached_price_ages` helper, `_reset_warned_today_for_tests`
- `scout/config.py` — 1 new Settings: `HELD_POSITION_STALE_WARN_HOURS: int = 24` + validator
- `tests/test_held_position_prices.py` — add 5 tests (gauge presence/accuracy, warn-emission, dedup, settings smoke, error-path isolation)
- `backlog.md` — flip status PROPOSED → PR-OPEN/SCRIPT-READY per cycle-13 PR #156 wording convention; on merge → SHIPPED + merge SHA. File 2 new follow-ups: `BL-NEW-HELD-POSITION-FALLBACK-COINS-ENDPOINT` (evidence-gated; verify `/coins/{id}` actually recovers tokens) + `BL-NEW-HELD-POSITION-STALE-COUNT-ALERT` (threshold-driven TG alert; ship after baseline measured)
- `tasks/todo.md` — Active Work entry

### Do NOT modify
- `scout/db.py` — no schema or signature changes (`_get_cached_price_ages` lives in held_position_prices.py)
- `scout/main.py` — caller wiring unchanged
- `.env` on srilu — no operator config flips

## Task decomposition

### Task 1: `_get_cached_price_ages` helper (direct SQL on `price_cache.updated_at`)

**Files:**
- Modify: `scout/ingestion/held_position_prices.py` — add helper
- Test: `tests/test_held_position_prices.py` — add test for the helper

- [ ] **Step 1: Write failing test**

```python
@pytest.mark.asyncio
async def test_get_cached_price_ages_returns_aware_datetimes(tmp_path):
    db = Database(str(tmp_path / "scout.db"))
    await db.connect()
    # Seed 2 price_cache rows with different ages
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) VALUES (?, ?, ?)",
        ("fresh-coin", 1.0, "2026-05-18T00:00:00+00:00"),
    )
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) VALUES (?, ?, ?)",
        ("stale-coin", 2.0, "2026-05-10T00:00:00+00:00"),
    )
    await db._conn.commit()
    from scout.ingestion.held_position_prices import _get_cached_price_ages
    ages = await _get_cached_price_ages(db, ["fresh-coin", "stale-coin", "missing-coin"])
    assert "fresh-coin" in ages
    assert "stale-coin" in ages
    assert "missing-coin" not in ages
    assert ages["fresh-coin"].tzinfo is not None
    await db.close()
```

- [ ] **Step 2: Run → FAIL** (ImportError)

- [ ] **Step 3: Implement (~15 lines)**

```python
# scout/ingestion/held_position_prices.py
from datetime import datetime, timezone

async def _get_cached_price_ages(
    db: "Database", coin_ids: list[str]
) -> dict[str, datetime]:
    """Direct query of price_cache.updated_at for the given coin_ids.

    Returns tz-aware datetimes. Coins absent from the cache are absent
    from the returned dict (caller treats missing as "needs refresh").

    Avoids touching scout/db.py (the existing `Database.get_cached_prices`
    helper doesn't return updated_at).
    """
    if db._conn is None or not coin_ids:
        return {}
    placeholders = ",".join("?" * len(coin_ids))
    sql = f"SELECT coin_id, updated_at FROM price_cache WHERE coin_id IN ({placeholders})"
    cur = await db._conn.execute(sql, coin_ids)
    rows = await cur.fetchall()
    return {
        r[0]: datetime.fromisoformat(r[1])
        for r in rows
        if r[1] is not None
    }
```

- [ ] **Step 4: Run → PASS**

- [ ] **Step 5: Commit**

### Task 2: `stale_open_count` + `stale_open_pct` gauge in existing log

**Files:**
- Modify: `scout/ingestion/held_position_prices.py` — extend `held_position_refresh_summary` event
- Test: `tests/test_held_position_prices.py` — assert new fields present + accurate

- [ ] **Step 1: Write failing test using aioresponses + seeded cache rows**
- [ ] **Step 2: Implement (~20 lines)** in `fetch_held_position_prices` (after `held_ids` is fetched and before the existing summary log):

```python
stale_open_count = None
stale_open_pct = None
try:
    ages_for_held = await _get_cached_price_ages(db, held_ids)
    now_utc = datetime.now(timezone.utc)
    stale_threshold_hours = 24
    stale_count = 0
    for tid in held_ids:
        age = ages_for_held.get(tid)
        if age is None:
            stale_count += 1
            continue
        if (now_utc - age).total_seconds() / 3600 > stale_threshold_hours:
            stale_count += 1
    stale_open_count = stale_count
    if held_ids:
        stale_open_pct = round(100.0 * stale_count / len(held_ids), 1)
except Exception:
    logger.exception("held_position_stale_count_failed")
```

Then in the summary log, add `stale_open_count=stale_open_count, stale_open_pct=stale_open_pct,`.

- [ ] **Step 3: Run → PASS**
- [ ] **Step 4: Commit**

### Task 3: Per-token persistent-stale WARN + 24h dedup + `_reset_warned_today_for_tests`

**Files:**
- Modify: `scout/ingestion/held_position_prices.py` — add WARN + dedup
- Test: `tests/test_held_position_prices.py` — assert WARN emits once per token per 24h

- [ ] **Step 1: Write 2 failing tests** (`test_persistently_stale_token_emits_warn` + `test_warn_dedup_within_24h`)
- [ ] **Step 2: Implement (~25 lines)**

```python
# scout/ingestion/held_position_prices.py (module-level)
_warned_today: dict[str, datetime] = {}

def _reset_warned_today_for_tests() -> None:
    """Test-only helper mirroring _reset_cycle_counter_for_tests."""
    global _warned_today
    _warned_today = {}

# Inside fetch_held_position_prices, after `ages_for_held` is computed:
try:
    now_utc = datetime.now(timezone.utc)
    threshold_hours = settings.HELD_POSITION_STALE_WARN_HOURS
    dedup_cutoff = now_utc - timedelta(hours=24)
    for tid in held_ids:
        age = ages_for_held.get(tid)
        if age is None:
            cache_age_hours = float("inf")
        else:
            cache_age_hours = (now_utc - age).total_seconds() / 3600
        if cache_age_hours < threshold_hours:
            continue
        last_warn = _warned_today.get(tid)
        if last_warn is not None and last_warn > dedup_cutoff:
            continue
        logger.warning(
            "held_position_token_persistently_stale",
            token_id=tid,
            cache_age_hours=round(cache_age_hours, 1) if cache_age_hours != float("inf") else None,
            cache_last=age.isoformat() if age is not None else None,
            warn_threshold_hours=threshold_hours,
        )
        _warned_today[tid] = now_utc
except Exception:
    logger.exception("held_position_persistent_stale_warn_failed")
```

- [ ] **Step 3: Run → 2 PASS**
- [ ] **Step 4: Commit**

### Task 4: Settings: `HELD_POSITION_STALE_WARN_HOURS`

**Files:**
- Modify: `scout/config.py` — add 1 key + validator
- Test: `tests/test_held_position_prices.py` — settings smoke test

- [ ] **Step 1: Write failing test**

```python
def test_held_position_stale_warn_hours_default():
    from scout.config import Settings
    s = Settings()
    assert s.HELD_POSITION_STALE_WARN_HOURS == 24
```

- [ ] **Step 2: Add to `scout/config.py` after `HELD_POSITION_PRICE_REFRESH_INTERVAL_CYCLES` at L110:**

```python
# BL-NEW-HELD-POSITION-REFRESH-RATE-GAP (cycle 13):
# Per-token persistent-stale WARN threshold; ≥ this many hours of cache
# staleness on an open paper_trade emits one WARN/24h to journalctl.
# Default 24 aligns with the stale_open_count gauge threshold (single
# semantic across both surfaces).
HELD_POSITION_STALE_WARN_HOURS: int = 24
```

Add field_validator:

```python
@field_validator("HELD_POSITION_STALE_WARN_HOURS")
@classmethod
def _validate_held_position_stale_warn_hours(cls, v: int) -> int:
    if v < 1:
        raise ValueError(f"HELD_POSITION_STALE_WARN_HOURS must be >= 1; got={v}")
    return v
```

- [ ] **Step 3: Run → PASS**
- [ ] **Step 4: Commit**

### Task 5: Findings doc + soak plan

**Files:**
- Create: `tasks/findings_held_position_refresh_rate_gap_2026_05_18.md`

- [ ] **Step 1: Write** — sections: (a) empirical diagnosis (CG-lane-EXCLUSIVE evidence; 0/21 in other surfaces); (b) root-cause verdict (stale-source); (c) per-token current stale list; (d) Task 4 deferral rationale (`/coins/{id}` not verifiable today due to 429); (e) post-deploy soak plan (grep journalctl for `held_position_token_persistently_stale`; verify per-token list overlaps the 21 known; promote `BL-NEW-HELD-POSITION-FALLBACK-COINS-ENDPOINT` to PROPOSED once CG-curl confirms `/coins/{id}` returns data for 1+ stale token); (f) alternate-diagnosis fallback (if >25% per-token turnover post-soak, re-investigate rate-limit-truncation per R1 #11).

- [ ] **Step 2: Commit**

### Task 6: Backlog + todo + 2 follow-ups

- [ ] backlog.md: PROPOSED → PR-OPEN/SCRIPT-READY 2026-05-18
- [ ] File `BL-NEW-HELD-POSITION-FALLBACK-COINS-ENDPOINT` (evidence-gated; ship after manual-curl confirms `/coins/{id}` recovery for ≥1 of the 21 stale tokens)
- [ ] File `BL-NEW-HELD-POSITION-STALE-COUNT-ALERT` (threshold-driven TG alert on `stale_open_count > max(5, 0.05 * held_total)` for ≥3 consecutive cycles; ship after baseline measured)
- [ ] tasks/todo.md: Active Work entry
- [ ] Commit

### Task 7: PR + 3 reviewers + fold

## Self-review checklist

- [ ] All CRITICAL findings from 2 plan reviewers folded (KeyError, parse_iso, diagnosis-evidence)
- [ ] All IMPORTANT findings folded OR explicitly descoped with evidence-gated follow-ups
- [ ] No live config flips; no `.env` change; no DB schema change
- [ ] CLAUDE.md plan-doc gate satisfied
- [ ] Hermes-first table + drift-check evidence present

## Out of scope (deliberate, with evidence-gated follow-ups)

- `/coins/{id}` fallback path (Task 4 originally) — descoped pending empirical CG-rate-limit clearance + manual-curl verification of 1+ stale token. Tracked as `BL-NEW-HELD-POSITION-FALLBACK-COINS-ENDPOINT`.
- Threshold-driven TG alert on `stale_open_count` — tracked as `BL-NEW-HELD-POSITION-STALE-COUNT-ALERT` (baseline-first; alert after operator chooses threshold).
- Auto-retire CG-delisted signals (large scope; operator-decision-per-token).
- DEX-side fallback (BL-NEW-DEX-PRICE-COVERAGE remains dormant per 2026-05-18 audit).

## Execution handoff

Proceeding to inline build per CLAUDE.md §10 (design fold consolidated into plan v2's code-block specs; separate design doc would duplicate). Build → PR → 3 reviewers.
