**New primitives introduced:** one new module `scout/ingestion/held_position_prices.py`, one new shell watchdog `scripts/held-position-price-watchdog.sh`, one new Settings field `HELD_POSITION_PRICE_REFRESH_INTERVAL_CYCLES` (default `1`), one new Settings field `HELD_POSITION_PRICE_REFRESH_ENABLED` (default `True`), and a minimal enhancement to existing `Database.cache_prices()` writer (INSERT OR REPLACE → INSERT ... ON CONFLICT DO UPDATE with `COALESCE` on `price_change_7d` to preserve existing 7d-change values when caller has none). NO new DB tables. NO new write paths beyond the enhanced `cache_prices()` writer.

# Plan: Held-position `price_cache` staleness fix (Alt A — dedicated refresh lane)

**Decisions locked by operator 2026-05-12:**
1. Alt A (dedicated held-position refresh lane) — watchdog co-shipped, not deferred
2. Cadence = 1 cycle (env-configurable via `HELD_POSITION_PRICE_REFRESH_INTERVAL_CYCLES`)
3. Deploy 2026-05-14 (post BL-NEW-AUTOSUSPEND-FIX 2026-05-13 closure; clean cohort boundary at `opened_at < 2026-05-14`)
4. Watchdog stale-threshold = 30 min (5 consecutive misses = alert; reduces noise vs 15-min while keeping P2 monitoring acceptable)
5. DEX-token-only follow-up deferred — filed as BL-NEW-DEX-PRICE-COVERAGE in backlog.md

Soak-interaction verification (operator-requested pre-2026-05-14-commit check, 2026-05-12):
- BL-NEW-QUOTE-PAIR (ends 2026-05-16) — scorer-side; not held-position-price-path
- BL-075 Phase B slow_burn (ends 2026-05-24) — upstream detector; pre-signal; not affected
- BL-067 conviction_lock (ends 2026-05-18) — HIGHEST sensitivity (trail_pct fire timing); mitigated by 2026-05-14 cohort boundary
- BL-NEW-LOW-PEAK-LOCK P2 (ends 2026-05-25) — HIGHEST sensitivity; same mitigation
- Recent commits all read-only against `price_cache`; no concurrent writer work

## Coverage caveat (structural — file-level acknowledged)

> Alt A's held-position lane uses `/simple/price` (CoinGecko), so it covers tokens with CG IDs. DEX-only-discovered tokens (contract-addr-shaped, no CG listing) are not covered by this fix. The triage heuristic at `scripts/triage_refresh_held_token_prices_20260512.py` (lines 37-49, `is_cg_coin_id`) skips obvious contract addresses; the held-position lane uses the same heuristic. Current cohort is 0% in that shape, so no immediate impact. Tracked as follow-up: **BL-NEW-DEX-PRICE-COVERAGE** (backlog.md). Promote out of deferred state when contract-addr-shaped tokens begin accumulating in the held cohort.

## Context

Per `tasks/findings_open_position_price_freshness_2026_05_12.md`: 49 of 150 open paper_trades had `price_cache.updated_at > 24h` on 2026-05-12T12:47Z; 65/150 had > 1h. The trailing-stop evaluator's 1h hard-skip at `scout/trading/evaluator.py:319` produces log-but-no-fire behavior for tokens > 1h stale, and trusts prices < 1h stale without per-decision guards — meaning the 1-24h band is silently making exit decisions on stale data. Triage 2026-05-12T12:58Z showed 25.8% material-drift rate (17/66 refreshed positions had |Δ%| > 10%). Worst cases: RIV +52%, goblin-trump −41%, TRUTH +21%.

This is the second strong instance of §12c-narrow (health-claim-vs-output-truth-for-specific-subset); first is perp_anomalies empty-since-deploy from the 2026-05-11 silent-failure audit.

## Approach: Alt A — dedicated held-position refresh lane

### Files

**New: `scout/ingestion/held_position_prices.py`** (~120 LOC)

`async def fetch_held_position_prices(session, settings, db) -> list[dict]` — queries `paper_trades WHERE status='open'` for unique token_ids, filters to CG-format ids (heuristic from triage script), batches a single `/simple/price?ids=...&vs_currencies=usd&include_market_cap=true&include_24hr_change=true` call via `coingecko_limiter.acquire()`, converts response to `/coins/markets`-shaped raw-coin dicts compatible with `Database.cache_prices()`.

Module-level cycle counter for cadence throttling; refresh fires when `counter % HELD_POSITION_PRICE_REFRESH_INTERVAL_CYCLES == 0`.

Emits `held_position_refresh_summary` structured log per cycle with `refreshed_count`, `material_drift_count` (|Δ%| > 10%), `largest_drift_pct`, `skipped_contract_addr_count`. Telemetry shape matches triage-script logging so post-deploy reconciliation is direct.

**Modified: `scout/main.py`** (~10 LOC)

Add `fetch_held_position_prices(session, settings, db)` to the parallel `asyncio.gather()` block at lines 479-488. Merge its return value into `all_raw` at line 507 before `db.cache_prices(all_raw)`.

**Modified: `scout/config.py`** (2 new fields)

```python
HELD_POSITION_PRICE_REFRESH_ENABLED: bool = True
HELD_POSITION_PRICE_REFRESH_INTERVAL_CYCLES: int = 1
```

**Modified: `scout/db.py:cache_prices`** (~5 LOC SQL enhancement)

INSERT OR REPLACE → INSERT ... ON CONFLICT DO UPDATE with `COALESCE(excluded.price_change_7d, price_cache.price_change_7d)` to preserve existing 7d when caller has no 7d data. No-op for existing callers (markets/trending always have 7d); preserves 7d for held-position caller. Same writer, slightly enhanced SQL.

**New: `tests/test_held_position_prices.py`** (~150 LOC)

Coverage: held-set extraction, CG-id heuristic filtering, batched /simple/price mocking via aioresponses, raw-coin-shape conversion, cadence-counter throttling, empty-cohort short-circuit, disabled-flag short-circuit, 429 handling via the existing limiter.

**New: `scripts/held-position-price-watchdog.sh`** (~80 LOC)

Shell + curl-direct Telegram pattern (matches `scripts/gecko-backup-watchdog.sh`). Runs every 5 min (systemd timer or cron). Queries `paper_trades JOIN price_cache` for held-positions with `(updated_at IS NULL OR age > 30 min)`. Alerts via curl-direct TG when count > 0 for 3 consecutive cycles (avoids transient-CG-blip spam). Curl-direct chosen over `scout.alerter.send_telegram_message` per documented choice in backup-watchdog (alerter requires aiohttp.ClientSession, swallows errors silently).

### Cross-cutting concerns

**Rate-limit posture:** 1 CG request per cycle at default cadence. Baseline 3-8 req/min of 25/min budget; held-position adds 1 req/min. Comfortable headroom; backpressure handle via `HELD_POSITION_PRICE_REFRESH_INTERVAL_CYCLES=5` or `=10` if pressure spikes.

**AALIEN sub-case:** Solved as free side-effect — `/simple/price` + INSERT ON CONFLICT DO UPDATE creates rows for never-cached tokens on first refresh.

**Telemetry:** Structured logs match triage-script JSON shape. 7-day post-deploy reconciliation will show material-drift distribution drop substantially from 25.8% baseline.

**Soak isolation:** Deploy 2026-05-14; soak attribution at BL-067 / BL-NEW-LOW-PEAK-LOCK P2 evaluation dates uses `opened_at < 2026-05-14` (stale-cache era) vs `>= 2026-05-14` (fresh-cache era) cohort split.

## Verification

**Pre-deploy:**
1. `uv run pytest tests/test_held_position_prices.py -v` — 8+ tests pass
2. `uv run python -m scout.main --dry-run --cycles 3` against test DB containing simulated open trades — verify `held_position_refresh_summary` log entries appear

**Post-deploy on VPS (2026-05-14):**
1. T+10min: `journalctl -u gecko-pipeline -g held_position_refresh_summary --since '10 min ago' | head -5` should show ≥5 refresh records
2. T+1hr: query `SELECT COUNT(*) FROM paper_trades pt LEFT JOIN price_cache pc ON pt.token_id = pc.coin_id WHERE pt.status='open' AND (pc.updated_at IS NULL OR (julianday('now') - julianday(pc.updated_at)) * 24 > 0.5)` should return 0
3. T+15min after watchdog timer starts: simulate stale row (`UPDATE price_cache SET updated_at = datetime('now','-1 hour') WHERE coin_id = 'bitcoin'`), wait 3×5min=15min for 3-consecutive-cycles threshold, confirm TG alert fires
4. T+7day: re-run audit equivalent to 2026-05-12 cohort query — expect stale > 1h count to be 0

**Soak attribution at BL-067 end (2026-05-18) and BL-NEW-LOW-PEAK-LOCK P2 end (2026-05-25):**
- Split closed trades into `opened_at < 2026-05-14` and `>= 2026-05-14` cohorts
- Evaluate trail-stop P&L separately per cohort
- If both cohorts show the same conviction-lock edge → strategy robust to price-freshness
- If post-fix cohort shows different edge → price-freshness was confounding pre-fix measurement

## Revert path

`HELD_POSITION_PRICE_REFRESH_ENABLED=False` in `.env` + service restart. Held-position lane stops firing; existing ingestion-lane writes continue as before. Watchdog can be revert-disabled by stopping the systemd timer.

## What is NOT in this change

- DexScreener-by-address fallback for pure-DEX-only tokens (filed as BL-NEW-DEX-PRICE-COVERAGE)
- Generic table-freshness daemon (each watchdog stays bespoke for now)
- Schema change to `price_cache` (no new columns)
- Changes to evaluator behavior (still reads cache exactly as today; no new staleness guards)
- §12c promotion to global CLAUDE.md (deferred to dedicated session per anti-tail-end discipline)
