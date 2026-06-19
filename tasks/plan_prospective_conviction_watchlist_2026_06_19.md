# Prospective sub-$30M High-Conviction Watchlist — Implementation Plan

**New primitives introduced:** `conviction_watchlist_snapshots` table; builder
`scout/conviction/prospective.py`; `/api/conviction/prospective` endpoint; "Prospective
Watchlist" dashboard tab; snapshot-freshness watchdog. Reuses `cross_surface_conviction`
tier gates + the gainers tracker's surface→source lookbacks.

> **For agentic workers:** TDD task-by-task; steps use `- [ ]`.

**Goal:** Snapshot current not-yet-pumped CG coins scored by sustained (≥24h) cross-surface
early confirmation, persist as the prospective-precision event stream, and surface
tier=high + fresh-mcap<$30M in a read-only dashboard tab. Observe-only.

**Architecture:** Pipeline hourly builder unions the 8 surface sources (CG-`coin_id`-keyed,
exact-match only), excludes coins already on `gainers_snapshots`/`gainers_comparisons`,
computes per-surface detection age, enriches current mcap+staleness, writes a snapshot
batch. Dashboard reads the latest batch. A §12a watchdog alerts if snapshots go stale.

**Tech Stack:** Python asyncio, aiosqlite, structlog, Pydantic Settings, FastAPI, React+Vite,
pytest (asyncio auto). Spec: `tasks/design_prospective_conviction_watchlist_2026_06_19.md`.

## Global Constraints
- Canonical identity = CG `coin_id`; exact-match aggregation only, NO symbol merge (Fold 2).
- Exclude pumped coins by BOTH `gainers_snapshots` AND `gainers_comparisons` (Fold 1).
- §12a freshness watchdog ships in V1 (Fold 3).
- Tab = `tier=high AND market_cap NOT NULL AND mcap fresh AND market_cap<$30M`; snapshot the
  full `tier≥watch` denominator; null/stale mcap never rendered as small-cap (Fold 4).
- Observe-only: no TG trade alerts, no trades, no action labels. (The freshness watchdog
  alert is a system-health alert, allowed.)
- No hardcoded thresholds (Settings + `Field` bounds). Alerter calls: `parse_mode=None` +
  `source=`. `black` formatted. N-gate display (never a bare backtest rate).
- Windows: builder/db/scorer tests run locally (aiosqlite, no aiohttp); endpoint/watchdog-alert
  tests import aiohttp → CI/Linux; frontend dist build on CI/operator box.

## File Structure
- `scout/config.py` — +6 Settings flags.
- `scout/db.py` — `conviction_watchlist_snapshots` table + migration + methods
  (`insert_conviction_watchlist_snapshot`, `get_latest_conviction_watchlist`,
  `prune_conviction_watchlist_snapshots`, `latest_conviction_watchlist_snapshot_at`).
- `scout/conviction/prospective_scorer.py` (new) — pure: ages → early/fresh/tier.
- `scout/conviction/prospective.py` (new) — builder (universe, exclusion, aggregation, mcap, write).
- `scout/conviction/watchlist_watchdog.py` (new) — freshness check → health + §12b alert.
- `scout/main.py` — wire builder + watchdog into hourly maintenance; prune into the loop.
- `dashboard/api.py` — `/api/conviction/prospective`.
- `dashboard/health_status.py` — add snapshot freshness to the SLO map (#337).
- `dashboard/frontend/components/ProspectiveWatchlistTab.jsx` (new) + tab registration + dist.
- Tests per task (below).

---

### Task 1: Config flags
**Files:** Modify `scout/config.py` (after the `CONVICTION_*` block ~line 46 of that group);
Test `tests/test_prospective_conviction_config.py`

**Produces:** `CONVICTION_PROSPECTIVE_ENABLED:bool=True`,
`CONVICTION_WATCHLIST_MAX_MCAP:float=30_000_000`,
`CONVICTION_WATCHLIST_MCAP_MAX_AGE_MINUTES:int=1440`,
`CONVICTION_PROSPECTIVE_LOOKBACK_DAYS:int=14`,
`CONVICTION_WATCHLIST_SNAPSHOT_RETENTION_DAYS:int=90`,
`CONVICTION_WATCHLIST_SNAPSHOT_SLO_MINUTES:int=180`.

- [ ] Step 1 — failing test:
```python
def test_prospective_conviction_flag_defaults(settings_factory):
    s = settings_factory()
    assert s.CONVICTION_PROSPECTIVE_ENABLED is True
    assert s.CONVICTION_WATCHLIST_MAX_MCAP == 30_000_000
    assert s.CONVICTION_WATCHLIST_MCAP_MAX_AGE_MINUTES == 1440
    assert s.CONVICTION_PROSPECTIVE_LOOKBACK_DAYS == 14
    assert s.CONVICTION_WATCHLIST_SNAPSHOT_RETENTION_DAYS == 90
    assert s.CONVICTION_WATCHLIST_SNAPSHOT_SLO_MINUTES == 180

def test_prospective_conviction_bounds_reject_invalid(settings_factory):
    import pytest; from pydantic import ValidationError
    with pytest.raises(ValidationError): settings_factory(CONVICTION_WATCHLIST_MAX_MCAP=-1)
    with pytest.raises(ValidationError): settings_factory(CONVICTION_PROSPECTIVE_LOOKBACK_DAYS=0)
```
- [ ] Step 2 — run → FAIL. Step 3 — add the 6 fields with `Field(..., ge=/gt=)` bounds
  (`MAX_MCAP ge=0`, `MCAP_MAX_AGE_MINUTES ge=0`, `LOOKBACK_DAYS ge=1 le=120`,
  `RETENTION_DAYS ge=1`, `SLO_MINUTES ge=1`). Step 4 — PASS. Step 5 — commit
  `feat(conviction): prospective watchlist config flags`.

---

### Task 2: DB table + methods
**Files:** Modify `scout/db.py`; Test `tests/test_prospective_conviction_db.py`

**Produces:**
- table `conviction_watchlist_snapshots(id, snapshot_at TEXT, coin_id TEXT, symbol TEXT,
  name TEXT, early_count INT, fresh_count INT, tier TEXT, contributing_surfaces TEXT(JSON),
  market_cap REAL NULL, mcap_age_minutes REAL NULL, first_detection_ages TEXT(JSON),
  created_at TEXT)`; indexes `(snapshot_at)`, `(snapshot_at, tier)`. Created in
  `_create_tables` + an idempotent migration method registered in `initialize()`.
- `async insert_conviction_watchlist_snapshot(rows: list[dict], snapshot_at: str) -> int`
- `async latest_conviction_watchlist_snapshot_at() -> str | None`
- `async get_latest_conviction_watchlist() -> list[dict]` (rows of the max snapshot_at)
- `async prune_conviction_watchlist_snapshots(*, keep_days:int) -> int`

- [ ] Step 1 — failing tests (tmp_path Database):
```python
from scout.db import Database
async def _db(tmp_path):
    db = Database(str(tmp_path/"c.db")); await db.initialize(); return db

async def test_insert_and_latest_roundtrip(tmp_path):
    db = await _db(tmp_path)
    assert await db.latest_conviction_watchlist_snapshot_at() is None
    rows = [{"coin_id":"pepe","symbol":"PEPE","name":"Pepe","early_count":4,"fresh_count":1,
             "tier":"high","contributing_surfaces":["chains","spikes","momentum","velocity"],
             "market_cap":12_000_000.0,"mcap_age_minutes":30.0,
             "first_detection_ages":{"chains":2000}}]
    n = await db.insert_conviction_watchlist_snapshot(rows, "2026-06-19T00:00:00+00:00")
    assert n == 1
    latest = await db.get_latest_conviction_watchlist()
    assert latest[0]["coin_id"]=="pepe" and latest[0]["tier"]=="high"
    assert latest[0]["contributing_surfaces"]==["chains","spikes","momentum","velocity"]
    assert latest[0]["market_cap"]==12_000_000.0

async def test_latest_returns_only_newest_batch(tmp_path):
    db = await _db(tmp_path)
    await db.insert_conviction_watchlist_snapshot([{"coin_id":"a","symbol":"A","name":"A",
        "early_count":2,"fresh_count":0,"tier":"watch","contributing_surfaces":[],
        "market_cap":None,"mcap_age_minutes":None,"first_detection_ages":{}}], "2026-06-19T00:00:00+00:00")
    await db.insert_conviction_watchlist_snapshot([{"coin_id":"b","symbol":"B","name":"B",
        "early_count":4,"fresh_count":0,"tier":"high","contributing_surfaces":[],
        "market_cap":None,"mcap_age_minutes":None,"first_detection_ages":{}}], "2026-06-19T01:00:00+00:00")
    latest = await db.get_latest_conviction_watchlist()
    assert [r["coin_id"] for r in latest]==["b"]

async def test_prune_by_retention(tmp_path):
    db = await _db(tmp_path)
    await db.insert_conviction_watchlist_snapshot([{"coin_id":"old","symbol":"O","name":"O",
        "early_count":2,"fresh_count":0,"tier":"watch","contributing_surfaces":[],
        "market_cap":None,"mcap_age_minutes":None,"first_detection_ages":{}}], "2026-01-01T00:00:00+00:00")
    deleted = await db.prune_conviction_watchlist_snapshots(keep_days=30)
    assert deleted == 1
```
- [ ] Step 2 — FAIL. Step 3 — implement table (in `_create_tables` list) + a
  `_migrate_conviction_watchlist_snapshots` registered in `initialize()` (mirror an existing
  idempotent migration; CREATE TABLE IF NOT EXISTS + CREATE INDEX after). Methods serialize
  `contributing_surfaces`/`first_detection_ages` via `json.dumps`, deserialize on read; use
  `self._txn_lock` for the insert; `INSERT` many rows with the shared `snapshot_at`. Step 4 —
  PASS. Step 5 — commit `feat(conviction): watchlist snapshot table + methods`.

---

### Task 3: Pure prospective scorer
**Files:** Create `scout/conviction/prospective_scorer.py`; Test `tests/test_prospective_scorer.py`

**Produces:** `score_prospective(first_detection_ages: dict[str,float|None], settings) ->
ProspectiveResult(early_count, fresh_count, tier, contributing: tuple[str,...])`.
A surface is SUSTAINED when its age ≥ `CONVICTION_EARLY_LEAD_MINUTES` (counts toward
early_count + tier via the existing `_tier`); FRESH when 0 ≤ age < that (fresh_count only).
None/negative age → ignored.

- [ ] Step 1 — failing tests: 0/2/4/8 sustained → tier low/watch/high; age exactly at 1440
  inclusive (sustained); age 100 → fresh only, not in tier; None age ignored; contributing in
  SURFACE order. Step 2 — FAIL. Step 3 — implement (reuse `_tier` + `SURFACE_LEAD_COLUMNS`
  ordering from `cross_surface.py`; equal weights). Step 4 — PASS. Step 5 — commit
  `feat(conviction): pure prospective scorer (sustained vs fresh)`.

---

### Task 4: Builder
**Files:** Create `scout/conviction/prospective.py`; Test `tests/test_prospective_builder.py`

**Consumes:** Task 2 db methods, Task 3 `score_prospective`, Settings.
**Produces:** `async build_prospective_watchlist(db, settings, *, now=None) -> dict` (run
summary: `{rows_written, high_tier, sub30m_high_fresh, per_surface_contrib, truncated}`).

Algorithm (all CG-`coin_id`-keyed, exact match — Fold 2):
1. `excluded` = set of coin_id in `gainers_snapshots` ∪ `gainers_comparisons` (Fold 1).
2. `cutoff` = now − LOOKBACK_DAYS. For each surface, `SELECT coin_id-or-key, MIN(detect_time)`
   grouped by key, where detect_time ≥ cutoff:
   - acceleration/momentum/slow_burn/velocity → their CG-slug tables (coin_id, MIN(detected_at)).
   - narrative → `predictions` (coin_id, MIN(predicted_at)).
   - pipeline → `candidates WHERE chain='coingecko'` (contract_address as coin_id, MIN(first_seen_at)).
   - chains → `signal_events` (token_id as coin_id, MIN(created_at)) — exact token_id only.
   Build `ages[coin_id][surface] = (now - MIN_time) minutes`.
3. universe = keys(ages) − excluded.
4. For each coin: `score_prospective(ages[coin])`; keep `tier≥watch`. Enrich symbol/name
   (`lookup_symbol_name_by_coin_id`) + mcap (`price_cache.market_cap`, `mcap_age_minutes` =
   now − price_cache.updated_at).
5. Cap rows at a generous bound (e.g. 2000) with `truncated` flag.
6. `insert_conviction_watchlist_snapshot(rows, now_iso)`.
7. `logger.info("conviction_prospective_snapshot_written", **summary)`.

- [ ] Step 1 — failing tests (tmp_path db; seed source rows at controlled timestamps):
  (a) a coin with 4 surfaces aged ≥24h → high, snapshotted; (b) a coin in `gainers_snapshots`
  but NOT comparisons → EXCLUDED (Fold 1); (c) a `chain='base'` candidate sharing a symbol with
  a CG coin → does NOT add to that coin's early_count (Fold 2); (d) a signal_events row whose
  token_id is a SYMBOL (not the coin_id) → not counted; (e) mcap enrich + `mcap_age_minutes`
  from price_cache; (f) tier=watch coin (2 surfaces) IS snapshotted (full denominator).
  Step 2 — FAIL. Step 3 — implement. Step 4 — PASS. Step 5 — commit
  `feat(conviction): prospective watchlist builder`.

---

### Task 5: Freshness watchdog (§12a)
**Files:** Create `scout/conviction/watchlist_watchdog.py`; modify `dashboard/health_status.py`;
Test `tests/test_watchlist_watchdog.py`

**Produces:** `async check_watchlist_freshness(db, session, settings, logger, *, now=None) ->
str` returning `ok|degraded|down`; on stale (latest snapshot age > SLO) logs WARNING
`conviction_watchlist_snapshot_stale` and, if enabled, sends a §12b operator alert
(`parse_mode=None`, `source="conviction_watchlist_watchdog"`, dispatched/delivered logs,
lazy alerter import). `dashboard/health_status.py` SLO map gains a `conviction_watchlist`
entry so `/api/system/health` reports it (#337 enum).

- [ ] Step 1 — failing tests: fresh snapshot → ok, no alert; age > SLO → down + alert
  dispatched+delivered (parse_mode None); no snapshot ever → unknown/down, alert once
  (in-memory dedup, reset helper). Step 2 — FAIL. Step 3 — implement (mirror
  `sqlite_maintenance` alert discipline). Step 4 — PASS. Step 5 — commit
  `feat(conviction): watchlist freshness watchdog (§12a/§12b)`.

---

### Task 6: Wire into pipeline hourly maintenance
**Files:** Modify `scout/main.py` (`_run_hourly_maintenance`); Test extends
`tests/test_hourly_maintenance.py`

- [ ] Step 1 — failing test: with `CONVICTION_PROSPECTIVE_ENABLED=True`, `_run_hourly_maintenance`
  awaits `build_prospective_watchlist` + `prune_conviction_watchlist_snapshots` + the watchdog;
  with all disabled, none are called. (Mirror the existing prune-call assertions; use AsyncMock.)
- [ ] Step 2 — FAIL. Step 3 — add a guarded block (try/except, can't crash the cycle): build →
  watchdog → add prune to the prune loop with `CONVICTION_WATCHLIST_SNAPSHOT_RETENTION_DAYS`.
  Step 4 — PASS (CI/Linux; main imports aiohttp). Step 5 — commit
  `feat(conviction): wire prospective builder + watchdog into hourly loop`.

---

### Task 7: Endpoint
**Files:** Modify `dashboard/api.py` (+ `dashboard/models.py` if a response_model is added);
Test `tests/test_prospective_conviction_endpoint.py`

**Produces:** `GET /api/conviction/prospective?min_tier=high&max_mcap=30000000&limit=50` →
`{meta:{read_only, not_trade_advice, observe_only, prospective, calibration:
"prospective_unvalidated", snapshot_at, snapshot_age_minutes, total_in_batch, returned,
mcap_max_age_minutes}, rows:[...], mcap_unknown:[...]}`. Reads latest batch; filters
`tier≥min_tier` and (for `rows`) `market_cap NOT NULL AND mcap_age_minutes<=max_age AND
market_cap<max_mcap`; null/stale-mcap high-tier coins go in `mcap_unknown` (Fold 4).

- [ ] Step 1 — failing tests (TestClient + seeded snapshot): shape/meta; min_tier filter;
  max_mcap filter; stale mcap → mcap_unknown not rows; empty/missing snapshot → empty +
  snapshot_at null. Step 2 — FAIL. Step 3 — implement. Step 4 — PASS. Step 5 — commit
  `feat(conviction): /api/conviction/prospective endpoint`.

---

### Task 8: Frontend tab + dist
**Files:** Create `dashboard/frontend/components/ProspectiveWatchlistTab.jsx`; register the tab
in the dashboard shell (mirror the existing Conviction tab registration); rebuild dist
(`npm --prefix dashboard/frontend run build:codex`) and COMMIT `dist/` per
[[feedback_vite_dist_index_html_commit_discipline]]; Test
`tests/test_dashboard_frontend_layout.py` (extend) + a contract test.

Mirror the existing Conviction tab/table component + `TokenLink`. Columns: symbol/name, mcap,
early_count (sustained ≥24h), contributing surfaces, emerging (fresh_count), oldest-surface
age, snapshot freshness. Banner: "Observe-only · prospective precision UNVALIDATED · not trade
advice." N-gate `INSUFFICIENT_DATA` until enough rows. Copy firewall: no "act now"/advice
terms (extend the existing firewall test).

- [ ] Step 1 — failing frontend layout/contract test (tab present, observe-only banner,
  emerging≠high). Step 2 — FAIL. Step 3 — implement component + register + build dist. Step 4 —
  PASS + `git diff --check` clean + committed dist. Step 5 — commit
  `feat(conviction): prospective watchlist dashboard tab`.

---

## Verification
```bash
uv run pytest tests/test_prospective_conviction_config.py tests/test_prospective_conviction_db.py \
  tests/test_prospective_scorer.py tests/test_prospective_builder.py -q          # local (no aiohttp)
uv run pytest --tb=short -q   # full suite (CI/Linux; watchdog/endpoint/main import aiohttp)
npm --prefix dashboard/frontend run build:codex                                   # CI/operator box
```

## Self-review (writing-plans)
- **Spec coverage:** identity policy (T4), dual-gainers exclusion (T4), §12a watchdog (T5),
  mcap rules (T7), full-denominator snapshot (T2/T4), observe-only (all), N-gate (T8),
  config (T1), tab (T8). All spec sections mapped.
- **Placeholders:** none — each task has concrete tests + algorithm; T8 mirrors named existing
  components (read them at impl time for exact JSX, per the codebase pattern).
- **Type consistency:** snapshot row dict keys identical across T2/T4/T7; `score_prospective`
  return fields consistent T3↔T4.

## Risks / review gates
- **Identity under-count (Fold 2):** prospective early_count is a conservative lower bound;
  the ≥4 gate may surface few/zero high-tier coins initially — that's correct (measure, don't
  assume). Surface `total_in_batch` + per-surface contrib so a near-empty watchlist is
  diagnosable, not silent.
- **Builder cost:** the union+per-coin enrich runs hourly; bounded by LOOKBACK_DAYS + row cap.
- Codex gates before merge: structural (sources reached, exclusion + identity correct),
  failure-mode (freshness watchdog visible, builder isolation, mcap-staleness honesty),
  UI/data-contract (endpoint↔tab shape, observe-only copy firewall, N-gate).

## Review section (fill after implementation)
- _Diff summary / test results / residual risks:_
