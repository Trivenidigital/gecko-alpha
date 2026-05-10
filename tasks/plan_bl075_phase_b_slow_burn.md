**New primitives introduced:** `scout/spikes/detector.py::detect_slow_burn_7d` (sibling function to existing `detect_7d_momentum`), `slow_burn_candidates` DB table (with `also_in_momentum_7d` flag column for clean overlap analysis), migration `bl_slow_burn_v1` (schema_version 20260515), Settings `SLOW_BURN_ENABLED` + `SLOW_BURN_MIN_7D_CHANGE` + `SLOW_BURN_MAX_1H_CHANGE` + `SLOW_BURN_MAX_MCAP` + `SLOW_BURN_MIN_VOLUME` + `SLOW_BURN_DEDUP_DAYS` (R2 NIT: naming parallelism with existing `MOMENTUM_7D_*`), scout/main.py wiring (parallel to `detect_7d_momentum`).

# Plan — BL-075 Phase B: Slow-burn watcher (RIV-shape blind spot)

## Hermes-first analysis

Mandatory per CLAUDE.md §7b. Checked Hermes skill hub at `hermes-agent.nousresearch.com/docs/skills` (verified 2026-05-09 in BL-NEW-QUOTE-PAIR plan; 18 domains, no crypto-DEX-parsing or token-detection skills).

| Domain | Hermes skill found? | Decision |
|---|---|---|
| 7-day price-change watcher (CG `/coins/markets`) | None — same negative result as Phase A | Build from scratch — sibling to existing `detect_7d_momentum` |
| Slow-burn vs spike pattern classifier | None | Build from scratch — pure threshold logic (`7d > 50%` AND `1h < 5%`) |

awesome-hermes-agent ecosystem check: no community skill for slow-burn detection. Verdict: **build from scratch**.

## Drift-check (per CLAUDE.md §7a)

Verified 2026-05-10 against current tree (master HEAD `862a352`):

1. **Phase A status:** `mcap_null_with_price_count` counter exists in `scout/heartbeat.py:27` + wired into both ingestion paths at `scout/ingestion/coingecko.py:117,248` + logged in heartbeat output at `scout/heartbeat.py:85`. **Phase A is SHIPPED**, not RESEARCH-GATED as the backlog entry suggests. Backlog stale.

2. **Phase A decision-tree evaluation (6d telemetry from prod heartbeat):** latest sample 2026-05-10T01:46Z shows `mcap_null_with_price_count=42,039 / tokens_scanned=78,550 = 53.5%`. Per BL-075 decision tree: `> 5% → significant blind spot; Phase B is justified`. **Phase B is unblocked**.

3. **Existing `detect_7d_momentum` at `scout/spikes/detector.py:190`** — filter: `change_7d >= min_7d_change (default 100%) AND mcap > 0 AND mcap <= max_mcap AND volume >= min_volume_24h`. Wired in `scout/main.py:604-621`. Stores to `momentum_7d` table.

4. **Slow-burn shape vs existing momentum_7d:** existing detector requires `7d >= 100%` (concentrated runners) AND `mcap > 0` (silently rejects the 53.5% null-mcap cohort). Slow-burn needs `7d >= 50% (lower) AND 1h < 5% (NEW gate)` — the inverse-of-velocity_alerter pattern. Net-new behavior; cannot be expressed by tweaking `detect_7d_momentum` alone.

5. **`scout/early/`** directory — does NOT exist in tree (BL-075 backlog spec proposed it). Per CLAUDE.md "prefer editing existing files to creating new ones", placing slow-burn in `scout/spikes/detector.py` as a sibling function is more aligned with project conventions than creating a new top-level module. Decision documented in §"What's in scope".

Drift verdict: **Phase A shipped (close-out backlog), Phase B genuinely net-new but scoped to extend `scout/spikes/detector.py` rather than create `scout/early/slow_burn.py` as the spec proposed.**

## Why this matters

Per BL-075 backlog motivating evidence (2026-05-03): RIV (`riv-coin`) ran $2M → $200M mcap over 30 days — exactly the asymmetric move the system exists to surface — yet appeared in zero detection tables across 30d of polling. Phase A telemetry confirms the silent-rejection rate at 53.5% — over half of CoinGecko-scanned tokens have null/0 mcap with positive price, and every existing detector silently rejects them via `mcap > 0` filters.

The slow-burn watcher addresses both gaps:
1. **1h-low gate** distinguishes slow accumulation from concentrated pumps (the velocity_alerter / detect_7d_momentum cohort is short-window).
2. **mcap=0 tolerance** (with explicit logging instead of silent rejection) — captures the cohort that Phase A revealed is being dropped.

User trading goal alignment per memory `user_trading_goals.md`: "manual research, chain-agnostic, beat CoinGecko Highlights by minutes." A slow-burn watcher catches the multi-day moves CG Highlights rotation will surface at peak — surfacing them earlier when the mcap is still low.

## What's in scope

1. **New CG field on `CandidateToken`** — actually NOT needed. `detect_slow_burn_7d` operates on `raw_coins: list[dict]` directly (matching the existing `detect_7d_momentum` pattern), so no model change required. The CG response already contains `price_change_percentage_7d_in_currency` AND `price_change_percentage_1h_in_currency` (request param at `scout/ingestion/coingecko.py:78,211`).

2. **New function `scout/spikes/detector.py::detect_slow_burn_7d`** — sibling to `detect_7d_momentum`. Same shape:
   - Args: `db, raw_coins, min_7d_change=50.0, max_1h_change=5.0, max_mcap=500_000_000, min_volume_24h=100_000, dedup_days=7`.
   - Filter: `change_7d >= min_7d_change AND abs(change_1h) <= max_1h_change AND volume >= min_volume_24h` — R1 MUST-FIX: symmetric 1h gate. A 1h-down `-8%` token is a retrace/dump, not a slow-burn — should NOT fire. Filter rejects both directions of high 1h volatility, captures only low-volatility-1h-with-strong-7d shape.
   - **Critical: do NOT silently reject mcap=0.** Pass them through with `market_cap=NULL` in the row + log structured `slow_burn_mcap_unknown` event for observability. Without this, the slow-burn watcher inherits the same blind spot Phase A diagnosed (validated by RIV back-check 2026-05-10: RIV's mcap was NULL for ~900 of its first 947 CG data points; mcap-tolerant detector would have caught RIV in the null-mcap window).
   - **mcap-known-upgrade flag (R2 MUST-FIX known gap):** if a coin is detected with mcap=NULL at cycle N and later with mcap=$10M at cycle N+M (within dedup_days), the dedup query suppresses the second row. We accept this for v1 — slow_burn_candidates is a research table, not a real-time signal. Document in runbook + code comment. Phase B+1 enhancement: query CG once per dedup-window-suppressed-mcap-upgrade event to back-fill, OR add `last_seen_market_cap` column updated by detector. Out of scope.
   - Dedup: pre-INSERT SELECT `WHERE coin_id = ? AND date(detected_at) >= date('now', '-N days')` (matches `detect_7d_momentum` pattern at `scout/spikes/detector.py:225-229`). NO `UNIQUE(coin_id, detected_at)` constraint per R2 MUST-FIX — microsecond-precision `detected_at` makes the constraint vacuous + misleading.
   - **Cross-detector overlap flag (R1 MUST-FIX):** before INSERT, query `momentum_7d` for `coin_id` within ±3 days of `now`. Set `also_in_momentum_7d` boolean column accordingly. Enables clean D+14 overlap analysis without inflated joins.
   - Persist to new `slow_burn_candidates` table.
   - Return list of dict rows.

3. **New table `slow_burn_candidates`** — schema mirrors `momentum_7d` plus `change_1h` column + `also_in_momentum_7d` overlap flag (R1 MUST-FIX):
   ```sql
   CREATE TABLE IF NOT EXISTS slow_burn_candidates (
       id            INTEGER PRIMARY KEY AUTOINCREMENT,
       coin_id       TEXT    NOT NULL,
       symbol        TEXT    NOT NULL,
       name          TEXT,
       price_change_7d  REAL NOT NULL,
       price_change_1h  REAL NOT NULL,
       price_change_24h REAL,
       market_cap    REAL,                     -- NULL allowed; mcap-unknown cohort
       current_price REAL,
       volume_24h    REAL,
       also_in_momentum_7d INTEGER NOT NULL DEFAULT 0,  -- R1 overlap flag
       detected_at   TEXT    NOT NULL
       -- NO UNIQUE constraint; pre-INSERT dedup query is the guard (R2 MUST-FIX).
   );
   CREATE INDEX idx_slow_burn_detected ON slow_burn_candidates(detected_at);
   CREATE INDEX idx_slow_burn_coin     ON slow_burn_candidates(coin_id);
   ```

4. **Migration `_migrate_bl_slow_burn_v1`** — follow the canonical `BEGIN EXCLUSIVE / try-except-ROLLBACK / SCHEMA_DRIFT_DETECTED / post-assertion` pattern (matches `_migrate_high_peak_fade_columns_and_audit_table`). Schema version 20260515 (post BL-NEW-QUOTE-PAIR's 20260513 + BL-NEW-LIVE-HYBRID M1.5a's 20260514).

5. **New Settings** (`scout/config.py`) — R2 NIT: parallel naming with existing `MOMENTUM_7D_*`:
   - `SLOW_BURN_ENABLED: bool = True` — kill switch.
   - `SLOW_BURN_MIN_7D_CHANGE: float = 50.0` — was `MIN_7D_PCT`; renamed for parallelism with `MOMENTUM_7D_MIN_CHANGE`.
   - `SLOW_BURN_MAX_1H_CHANGE: float = 5.0` — was `MAX_1H_PCT`; gate is `abs(change_1h) <= this` (symmetric).
   - `SLOW_BURN_MAX_MCAP: float = 500_000_000` — matches MOMENTUM_7D_MAX_MCAP.
   - `SLOW_BURN_MIN_VOLUME: float = 100_000` — matches MOMENTUM_7D_MIN_VOLUME.
   - `SLOW_BURN_DEDUP_DAYS: int = 7`.

6. **Wiring in `scout/main.py`** — parallel to `detect_7d_momentum` block at lines 604-621:
   ```python
   if settings.SLOW_BURN_ENABLED and _raw_markets_combined:
       try:
           slow_burn = await detect_slow_burn_7d(
               db, _raw_markets_combined,
               min_7d_change=settings.SLOW_BURN_MIN_7D_CHANGE,
               max_1h_change=settings.SLOW_BURN_MAX_1H_CHANGE,
               max_mcap=settings.SLOW_BURN_MAX_MCAP,
               min_volume_24h=settings.SLOW_BURN_MIN_VOLUME,
               dedup_days=settings.SLOW_BURN_DEDUP_DAYS,
           )
           if slow_burn:
               logger.info(
                   "slow_burn_detected",
                   count=len(slow_burn),
                   tokens=[s["symbol"] for s in slow_burn],
                   mcap_unknown_count=sum(1 for s in slow_burn if not s.get("market_cap")),
                   also_in_momentum_count=sum(1 for s in slow_burn if s.get("also_in_momentum_7d")),
               )
       except Exception:
           logger.exception("slow_burn_error")
   ```

7. **Tests** in `tests/test_slow_burn_detector.py`:
   - Happy path: token with `7d=80%`, `1h=2%`, `mcap=$10M`, `vol=$200K` → fires.
   - Boundary 7d: 49.99% does NOT fire; 50.0% fires; 50.01% fires.
   - Boundary 1h: 4.99% fires; 5.0% fires; 5.01% does NOT fire (the inversion direction differs from 7d).
   - Velocity-shape rejection: token with `7d=80%, 1h=15%` does NOT fire (concentrated pump, not slow burn).
   - Volume floor: `vol=$50K < $100K` does not fire.
   - Mega-cap floor: `mcap=$1B > $500M` does not fire.
   - **mcap-unknown cohort fires** + emits `slow_burn_mcap_unknown` log event (the Phase A blind-spot fix; locks the regression test against silent re-rejection).
   - Dedup: same coin twice within 7d → only first fires.
   - Cross-day dedup: different `detected_at` dates within 7d → still dedup'd.
   - SLOW_BURN_ENABLED=False short-circuits (test wiring in main.py).
   - Migration: columns + indexes added, schema_version row written, idempotent rerun.

8. **Docs**:
   - `CLAUDE.md` — add slow_burn to the list of detection layers.
   - `docs/runbook_slow_burn_phase_b.md` — operator runbook with shadow-soak verification queries.
   - `tasks/todo.md` — D+14 shadow-soak end window.

## What's out of scope

- **No paper trade dispatch** (per BL-075 spec — research-only, like velocity_alerter).
- **No Telegram alerts** (research-only; data accumulates in `slow_burn_candidates` for operator review).
- **No scoring integration** — slow-burn is NOT a CandidateToken signal yet. Phase B+1 (graduation to signal type) is gated on 14d shadow-soak data.
- **No DexScreener mcap fallback for null-mcap tokens** — addressing the Phase A finding fully would require a DS lookup per null-mcap token. Out of Phase B scope; logged as deferred follow-up if shadow-soak shows the null-mcap cohort is worth saving.
- **No new top-level `scout/early/` module** — extending `scout/spikes/detector.py` is more aligned with existing conventions; the spec's `scout/early/slow_burn.py` proposal is rejected per drift-check §5.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Filter too loose → table fills with noise | Conservative thresholds: `7d ≥ 50%` (1.5x) AND `1h ≤ 5%`. CG returns ~10K tokens/poll; expect <100 rows/cycle. Dedup over 7d keeps daily flow <50/day. |
| Filter too tight → no detections in 14d soak | Boundary tests lock thresholds; if 14d soak produces <10 detections we widen. Acceptance criteria below specify this. |
| Mcap-unknown cohort dominates results | Acceptable for v1 — the whole point is to capture them. Operator triages via dashboard query. If >80% of detections are mcap-unknown, we add Phase B+1 DexScreener lookup. |
| Conflict with `detect_7d_momentum` data — same token in both tables | Acceptable. Different filter shapes catch different patterns. Both tables are research; operator joins as needed. |
| Migration `bl_slow_burn_v1` collides with existing schema_version | Verified 20260515 is unused (last is 20260514 from M1.5a per `scout/db.py`). Safe. |

## Soak + revert

- Shadow soak: 14 days post-deploy.
- Revert: `SLOW_BURN_ENABLED=False` env override (no code rollback; new table stays untouched).
- D+14 evaluation queries:
  ```sql
  -- Total detections
  SELECT COUNT(*), COUNT(DISTINCT coin_id) FROM slow_burn_candidates;

  -- Mcap-known vs mcap-unknown split
  SELECT
    SUM(CASE WHEN market_cap > 0 THEN 1 ELSE 0 END) AS known_mcap,
    SUM(CASE WHEN market_cap IS NULL OR market_cap = 0 THEN 1 ELSE 0 END) AS unknown_mcap
  FROM slow_burn_candidates;

  -- Hit-rate vs existing 7d_momentum (overlap)
  SELECT
    sb.coin_id,
    sb.detected_at,
    m7.detected_at AS m7_detected_at
  FROM slow_burn_candidates sb
  LEFT JOIN momentum_7d m7
    ON m7.coin_id = sb.coin_id
   AND date(m7.detected_at) BETWEEN date(sb.detected_at, '-3 days') AND date(sb.detected_at, '+3 days');

  -- Tokens that became 5x runners post-detection (manual eval — requires
  -- the operator to spot-check via CoinGecko)
  SELECT coin_id, symbol, detected_at, market_cap, current_price
  FROM slow_burn_candidates
  WHERE date(detected_at) >= date('now', '-14 days');
  ```

## Acceptance criteria (post R1 MUST-FIX revision — split volume from quality)

- All new tests pass (≥10 cases).
- Full regression suite passes.
- `black scout/ tests/` clean.
- Migration applied on prod without errors.
- **Pre-merge gate (R1 MUST-FIX, completed 2026-05-10):** RIV back-check via `CG /coins/riv-coin/market_chart?days=30` shows 13 days where `7d_change ≥ 50% AND day-over-day < 5%` simultaneously, with mcap NULL on 900+ of 947 data points. Confirms 50% threshold + mcap-tolerance both match the BL-075 motivating evidence. Threshold validated; no change needed.
- **D+14 volume gate:** ≥35 unique-coin detections (R1 MUST-FIX — n=10 was too low for binomial power; n=35 gives 80% power on a 20% true-positive-rate hypothesis). If <35 in 14d → widen `MIN_7D_CHANGE` to 40% and resoak 7 more days.
- **D+14 quality gate:** of the first 35 detected coins, ≥3 must show ≥2x price within 30d post-detection (manually verified via CoinGecko spot-check). If <3 → signal is no better than random for slow-burn detection; close as won't-fix-on-this-axis.
- **D+14 separability gate:** `also_in_momentum_7d` flag distribution should show <70% overlap with detect_7d_momentum (otherwise slow-burn is just a softer-threshold momentum_7d, not a distinct signal worth keeping separate).
- Operator final eval at D+14: review the three gates above; promote to signal type only if all three pass.

## Estimate

- Code + tests + migration + runbook: ~2-3 hours.
- Reviewer dispatch + fix cycles: ~1 hour.
- PR + reviewers + merge + deploy: ~30 min.
- Shadow soak: 14 days passive.

## Reviewer dispatch — plan stage (2 parallel)

- **R1 (statistical/data):** Are `7d ≥ 50%` and `1h ≤ 5%` the right thresholds? Defend numerically — at the existing CG market scan rate, what fraction of polls would have ≥1 detection? Will the 7d threshold of 50% (vs detect_7d_momentum's 100%) overlap excessively? Should we explicitly back-check RIV by reconstructing its likely 7d/1h trace?
- **R2 (code/structural):** Is extending `scout/spikes/detector.py` correct (vs the spec's `scout/early/slow_burn.py`)? Migration shape correct? Settings naming consistent with `MOMENTUM_7D_*`? Mcap-unknown cohort handling clear and surfaced via structured logging? `UNIQUE(coin_id, detected_at)` index correct for dedup queries?
