# Paper-Trading Feedback Loop — Design Spec

**Date:** 2026-04-18 (revised after two parallel review rounds)
**Status:** Implementation-ready — ready for user approval
**Supersedes:** n/a (new initiative)
**PR target:** Sprint 1 — Feedback Loop (PR #29)
**Sprint:** Paper-Trading Intelligence — Sprint 1

---

## 1. Goal

Convert the paper-trading pipeline from a passive logger into a closed learning loop. Today, every signal fires into `paper_trades` but we have no per-combo stats, no sense of how early we were vs CoinGecko Highlights, no visibility into winners we *missed*, and no mechanism for underperformers to self-demote. Sprint 1 closes all six gaps so that next quarter's signal quality is a function of last quarter's evidence.

Concrete outcome: by week 2, the user receives a Sunday digest ranking every signal-combo by 30d win rate, with chronic losers automatically gated out of new trades, plus a weekly list of +50% CG winners we failed to open a paper trade on.

---

## 2. The six gaps (scope)

| # | Gap | Today | After Sprint 1 |
|---|---|---|---|
| G1 | Multi-signal combos | `signal_data.signals` stored as JSON but never aggregated | Materialized `combo_performance(combo_key, window)` table. Most combos will be single-signal (see §3 D13); treat combo = effective suppression unit. |
| G2 | Rolling windows | Daily digest shows lifetime-to-date stats only | 7d + 30d rolling stats per combo |
| G3 | Weekly digest | None | Sunday 09:00 digest with leaderboard, missed-winners, lead-time, suppression log |
| G4 | Lead-time metric | None | One column on `paper_trades`: `lead_time_vs_trending_min` + tri-state status column |
| G5 | Missed-winner audit | None | On-demand query: CG +50% 24h winners where no paper trade opened ±30min, tiered, pipeline-gap aware |
| G6 | Auto-suppression | None | Entry-gate: if `trades>=20 AND 30d_wr<30%` → deny open_trade for 14d, then 5-trade parole |

**Explicitly out of scope:**
- Social signal integration (LunarCrush etc — research concluded structurally unfit)
- Triple-combo stats (Sprint 2 if pairs prove useful)
- Lead-time vs first +5% price move (descoped — requires new `price_history_cg` table; Sprint 2)
- Per-dispatcher signal enrichment to produce multi-signal combos in `trade_gainers` / `trade_trending` / etc (Sprint 2)
- Live execution (paper-only stays paper-only)
- Dashboard UI for the loop (CLI + Telegram sufficient for Sprint 1)
- Backfilling historical lead-times (lazy: populate going forward only)

---

## 3. Key decisions (locked from brainstorm + post-review revisions)

| # | Decision | Rationale |
|---|---|---|
| D1 | **Hybrid persistence (Approach 3)** — materialize only `combo_performance`; everything else on-demand | Suppression is the only hot-path read; weekly digest tolerates a 2s SQL scan |
| D2 | **Pair-level combos only** for Sprint 1 | Pair cardinality is already small; triples blow up with low samples each |
| D3 | **Min 10 trades** for leaderboard visibility; **min 20 trades** for suppression gate | 10 to rank, 20 is the floor where WR<30% is statistically meaningful |
| D4 | **Suppression rule: `trades>=20 AND 30d_wr<30%`** | 30% matches random-walk floor for 2:1 TP:SL; below is actively harmful |
| D5 | **Parole: 14 days locked, then 5-trade re-test** | 14d covers most regime shifts; 5 trades lets combo re-qualify without flooding |
| D6 | **Missed-winner tiers: partial_miss (50–200%), major_miss (200–1000%), disaster_miss (>1000%)** | Severity tiers focus attention on PnL-moving cases |
| D7 | **Missed-winner filters: `market_cap >= $5M` AND `market_cap_rank <= 1500`** | Excludes micro-cap junk the signals correctly ignore; only audits winners we could reasonably have caught |
| D8 | **Lead-time reference: CG-trending-appearance only** (Sprint 1) | Primary reference matches user's stated goal ("beat CG Highlights by minutes"). First +5% deferred to Sprint 2 — requires price history infrastructure. |
| D9 | **Windows: 7d + 30d** | 7d catches regime shifts; 30d is suppression reference |
| D10 | **Cadence: daily digest unchanged, add weekly Sunday 09:00** | Daily stays operational; weekly does analytical lifting |
| D11 | **Denormalize `signal_combo` onto `paper_trades`** | `json_extract` slow at aggregate scale; hot-path reads justify the column |
| D12 | **Deploy without feature flag** | Fully additive; cold-start (no `combo_performance` row) defaults to allow |
| D13 | **Accept single-signal combos as the norm** | Only `trade_first_signals` populates `signal_data.signals` in current codebase. For 6 of 7 dispatchers, `combo_key == signal_type`. Suppression is effectively per-signal-type for Sprint 1. Enriching other dispatchers is Sprint 2 scope after data shows which signal types underperform. |
| D14 | **Schedule via elapsed-time pattern** (same as `last_summary_date`) | Weekly digest uses `last_weekly_digest_date` + `weekday() == 6 AND hour == 9` check inside `_pipeline_loop`. No new scheduler infrastructure. |
| D15 | **Refresh cadence: nightly-only** (Flow B dropped per review) | Removing fire-and-forget after `close_trade` eliminates concurrent-write race with `should_open`. Nightly refresh at 03:00 is sufficient — suppression decisions lag by <24h which is acceptable given 14d parole window. |
| D16 | **Parole decrement uses `BEGIN IMMEDIATE` transaction + SELECT/UPDATE** | `aiosqlite` does not surface rows from `UPDATE ... RETURNING` reliably. `BEGIN IMMEDIATE` acquires a write lock before SELECT, preventing SELECT/UPDATE interleave across coroutines sharing the connection. Matches codebase patterns. |
| D17 | **Fail-open with loud escalation on DB error** | `should_open` returns allow on DB error (preserve alerting), but counter-based Telegram alert fires after N failures/hour. Uses `last_alerted_ts` sentinel (not deque.clear) so sustained bursts keep alerting. |
| D18 | **Per-column schema migration + `schema_version` + post-migration assertion, wrapped in `BEGIN EXCLUSIVE`** | Prevents silent drift + partial-apply risk. `schema_version` row only commits if all DDL succeeded. |
| D19 | **All DB access uses `db._conn.execute(...)` + `cursor.fetchone()` pattern** | No new wrapper methods on `Database`. Matches existing `signals.py`, `digest.py`, `evaluator.py` conventions. |
| D20 | **`signal_combo` computed once in `signals.py`, passed to `open_trade` as kwarg** | Single derivation site; avoids drift if `build_combo_key` logic evolves. `engine.open_trade` treats it as opaque input. |
| D21 | **Percentiles computed in Python, not SQL** | SQLite has no PERCENTILE_CONT. Analytics fetches all `lead_time_vs_trending_min` rows for a signal_type (status='ok'), sorts in Python, slices p25/p50/p75. Cheap at digest scale (~thousand rows/week). |

---

## 4. Data model

### 4.1 New table: `combo_performance`

```sql
CREATE TABLE combo_performance (
    combo_key TEXT NOT NULL,                   -- e.g. "first_signal+momentum_ratio" or "volume_spike"
    window TEXT NOT NULL,                      -- '7d' | '30d'
    trades INTEGER NOT NULL,
    wins INTEGER NOT NULL,
    losses INTEGER NOT NULL,
    total_pnl_usd REAL NOT NULL,
    avg_pnl_pct REAL NOT NULL,
    win_rate_pct REAL NOT NULL,
    suppressed INTEGER NOT NULL DEFAULT 0,     -- 0 | 1
    suppressed_at TEXT,                         -- set when gate trips
    parole_at TEXT,                             -- suppressed_at + 14d
    parole_trades_remaining INTEGER,            -- 5 on parole entry, decrements per allowed trade
    refresh_failures INTEGER NOT NULL DEFAULT 0, -- consecutive nightly refresh failures
    last_refreshed TEXT NOT NULL,
    PRIMARY KEY (combo_key, window)
);
-- No additional index needed — PK covers all hot-path reads.
-- Cold-path scans (suppression log, chronic-failure scan) tolerate full-table cost.
```

### 4.2 New columns on `paper_trades`

```sql
ALTER TABLE paper_trades ADD COLUMN signal_combo TEXT;
ALTER TABLE paper_trades ADD COLUMN lead_time_vs_trending_min REAL;
ALTER TABLE paper_trades ADD COLUMN lead_time_vs_trending_status TEXT;  -- 'ok' | 'no_reference' | 'error'
CREATE INDEX idx_paper_trades_combo_opened ON paper_trades(signal_combo, opened_at);
CREATE INDEX idx_paper_trades_token_opened ON paper_trades(token_id, opened_at);  -- missed-winner LEFT JOIN
CREATE INDEX IF NOT EXISTS idx_trending_snapshots_coin_id ON trending_snapshots(coin_id);  -- hot-path lead-time lookup
```

### 4.3 New table: `schema_version`

```sql
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    description TEXT
);
-- Seeded with current implicit version on first run.
```

### 4.4 Combo key derivation

```python
def build_combo_key(signal_type: str, signals: list[str] | None) -> str:
    """
    Build combo_key from signal_type + up to 1 extra signal.
    Pair cap enforced: signal_type always included; if >1 extra signal provided,
    keep alphabetically-first and log the rest for Sprint 2 analysis.
    """
    parts = {signal_type}
    truncated: list[str] = []
    if signals:
        extras = sorted(s for s in signals if s != signal_type)
        if extras:
            parts.add(extras[0])
            truncated = extras[1:]
    if truncated:
        log.info("combo_key_truncated_signals",
                 signal_type=signal_type, kept=extras[0], dropped=truncated)
    return "+".join(sorted(parts))
```

Examples:
- `signal_type="first_signal"`, `signals=["momentum_ratio", "vol_acceleration"]` → `"first_signal+momentum_ratio"` (logs `dropped=["vol_acceleration"]`)
- `signal_type="volume_spike"`, no signals → `"volume_spike"`
- `signal_type="narrative_prediction"`, `signals=None` → `"narrative_prediction"`

### 4.5 Lead-time semantics (trending-only for Sprint 1)

Computed in a helper called by `engine.open_trade`, before insert. Uses `db._conn.execute` per D19:

```python
async def _compute_lead_time_vs_trending(
    db: Database, token_id: str, now: datetime
) -> tuple[float | None, str]:
    """Returns (lead_time_min, status). status in {'ok', 'no_reference', 'error'}."""
    try:
        cursor = await db._conn.execute(
            "SELECT MIN(snapshot_at) FROM trending_snapshots WHERE coin_id = ?",
            (token_id,),
        )
        row = await cursor.fetchone()
        crossed_at = row[0] if row else None
        if crossed_at is None:
            return (None, "no_reference")
        delta_min = (now - datetime.fromisoformat(crossed_at)).total_seconds() / 60
        # Positive: we opened AFTER the coin trended (we were late)
        # Negative: we opened BEFORE it trended (we beat CG Highlights)
        return (delta_min, "ok")
    except Exception as e:
        log.error("lead_time_compute_error", err=str(e), err_id="LEAD_TIME_CALC",
                  token_id=token_id)
        return (None, "error")
```

Reading convention:
- `status='ok'`, `min < 0`: we beat trending by |min| minutes (**goal state**)
- `status='ok'`, `min > 0`: we were late by `min` minutes
- `status='no_reference'`: coin never appeared in `trending_snapshots` (legitimate — not all trades come from trending coins)
- `status='error'`: computation failed — excluded from analytics aggregations

Analytics explicitly filters `WHERE lead_time_vs_trending_status = 'ok'` when computing medians.

---

## 5. Module structure

```
scout/trading/
  analytics.py          [NEW]  On-demand stat queries + audit gap detection
  suppression.py        [NEW]  Entry-gate + fallback counter + atomic parole decrement
  combo_refresh.py      [NEW]  Nightly refresh + chronic-failure tracking
  weekly_digest.py      [NEW]  Sunday digest builder + sender
  signals.py            [MOD]  should_open check before each open_trade
  engine.py             [MOD]  open_trade computes signal_combo + lead_time + status
  digest.py             [—]    Daily digest unchanged
  models.py             [—]    PaperTrade unchanged (columns added via migration)
scout/
  db.py                 [MOD]  Migration logic per-column + schema_version
  main.py               [MOD]  Add nightly refresh (03:00) + weekly digest (Sun 09:00) scheduling
```

### 5.1 `analytics.py`

All functions use `db._conn.execute(...)` + `cursor.fetchall()` per D19.

```python
async def combo_leaderboard(db, window: str, min_trades: int = 10) -> list[dict]:
    """
    Top/bottom combos by WR. Deterministic tie-break:
    ORDER BY win_rate_pct DESC, trades DESC, combo_key ASC.
    Only includes combos with trades >= min_trades.
    """

async def audit_missed_winners(db, start: datetime, end: datetime) -> dict:
    """
    Returns {
      "tiers": {"partial_miss": [...], "major_miss": [...], "disaster_miss": [...]},
      "uncovered_window": [...],   # winners whose crossed_at fell in pipeline gap
      "denominator": {
          "winners_total": N,           # rows from winners CTE (post mcap/rank filter)
          "winners_caught": M,          # had a paper_trades row within ±30min
          "winners_filtered_by_mcap": K1,   # rows excluded by mcap filter
          "winners_filtered_by_rank": K2,   # rows excluded by rank filter
          "pipeline_gap_hours": H,
      },
    }
    """

async def lead_time_breakdown(db, window: str) -> dict[str, dict]:
    """
    Per-signal-type lead-time stats. Percentiles computed in Python per D21.
    Query: SELECT signal_type, lead_time_vs_trending_min, lead_time_vs_trending_status
           FROM paper_trades WHERE opened_at >= ? (window boundary)
    Then in Python, group by signal_type:
      values_ok = sorted(row.lead_time for row in group if row.status == 'ok')
      median = values_ok[len(values_ok)//2] if values_ok else None
      p25, p75 = sliced at 25%/75% positions
    Returns {signal_type: {median_min, p25_min, p75_min,
                           count_ok, count_no_reference, count_error}}.
    """

async def suppression_log(db, start: datetime, end: datetime) -> list[dict]:
    """
    Derived from combo_performance.suppressed_at timestamps. No new table.
    Query: SELECT combo_key, suppressed_at, parole_at, parole_trades_remaining
           FROM combo_performance WHERE suppressed_at BETWEEN ? AND ?.
    """

async def detect_pipeline_gaps(db, start: datetime, end: datetime,
                                max_gap_minutes: int = 60) -> list[tuple[str, str]]:
    """
    Returns list of (gap_start, gap_end) ISO strings where gainers_snapshots
    has no row for > max_gap_minutes. Algorithm: SELECT snapshot_at ordered,
    compute deltas between consecutive rows, return ranges where delta > threshold.
    """
```

### 5.2 `suppression.py`

Uses `BEGIN IMMEDIATE` transaction for parole decrement per D16 (aiosqlite's `RETURNING` is unreliable). Fail-open with `last_alerted_ts` dedup per D17.

```python
# Module-level — assumes single-event-loop process (standard for gecko-alpha pipeline)
_FALLBACK_WINDOW_SEC = 3600
_FALLBACK_ALERT_THRESHOLD = 5
_FALLBACK_ALERT_COOLDOWN_SEC = 900   # re-alert at most every 15 min during sustained failure
_fallback_timestamps: deque[float] = deque()
_last_alerted_ts: float = 0.0


async def should_open(db: Database, combo_key: str) -> tuple[bool, str]:
    """
    Returns (allow, reason). Reason is for structured logging.
    Fail-open on DB error with counter-based Telegram escalation.
    """
    try:
        cursor = await db._conn.execute(
            "SELECT suppressed, parole_at, parole_trades_remaining "
            "FROM combo_performance WHERE combo_key = ? AND window = '30d'",
            (combo_key,),
        )
        row = await cursor.fetchone()
    except Exception as e:
        await _record_fallback(combo_key, str(e))
        return (True, "db_error_fallback_allow")

    if row is None:
        return (True, "cold_start")
    suppressed, parole_at, parole_remaining = row
    if not suppressed:
        return (True, "ok")
    if parole_at is None or datetime.fromisoformat(parole_at) > datetime.now(tz=UTC):
        return (False, "suppressed")

    # Parole open: atomic decrement via BEGIN IMMEDIATE (per D16).
    # BEGIN IMMEDIATE acquires the write lock up-front, so a concurrent should_open
    # call on the same connection will await the COMMIT before its own SELECT.
    try:
        await db._conn.execute("BEGIN IMMEDIATE")
        cur = await db._conn.execute(
            "SELECT parole_trades_remaining FROM combo_performance "
            "WHERE combo_key = ? AND window = '30d'",
            (combo_key,),
        )
        reread = await cur.fetchone()
        remaining = reread[0] if reread else 0
        if remaining <= 0:
            await db._conn.execute("COMMIT")
            return (False, "parole_exhausted")
        await db._conn.execute(
            "UPDATE combo_performance SET parole_trades_remaining = ? "
            "WHERE combo_key = ? AND window = '30d'",
            (remaining - 1, combo_key),
        )
        await db._conn.commit()
        return (True, "parole_retest")
    except Exception as e:
        try:
            await db._conn.execute("ROLLBACK")
        except Exception:
            pass
        await _record_fallback(combo_key, f"parole_decrement: {e}")
        return (True, "db_error_fallback_allow")


async def _record_fallback(combo_key: str, err: str) -> None:
    """Log + increment fail-open counter; alert at threshold w/ cooldown to dedup."""
    global _last_alerted_ts
    log.error("suppression_db_error",
              combo_key=combo_key, err=err, err_id="SUPP_DB_FAIL")
    now_ts = time.monotonic()
    _fallback_timestamps.append(now_ts)
    while _fallback_timestamps and now_ts - _fallback_timestamps[0] > _FALLBACK_WINDOW_SEC:
        _fallback_timestamps.popleft()
    # Fire Telegram at threshold, then obey cooldown so sustained bursts keep alerting
    # (but no more than once per cooldown period). Previous `.clear()` approach caused
    # silent gaps during sustained DB degradation.
    if (len(_fallback_timestamps) >= _FALLBACK_ALERT_THRESHOLD
            and now_ts - _last_alerted_ts >= _FALLBACK_ALERT_COOLDOWN_SEC):
        _last_alerted_ts = now_ts
        await alerter.send_telegram_message(
            f"⚠ Suppression fail-open fired {len(_fallback_timestamps)}x in last hour. "
            f"DB may be degraded — combos are currently ungated."
        )
```

### 5.3 `combo_refresh.py`

```python
async def refresh_combo(db: Database, combo_key: str) -> bool:
    """Recompute 7d + 30d rows for one combo; apply suppression rule. Returns success."""

async def refresh_all(db: Database) -> dict:
    """
    Nightly: recompute all combos seen in last 30d. Apply suppression rule.
    Returns {"refreshed": N, "failed": M, "chronic_failures": [combo_keys]}.
    Emits structured log `combo_refresh_chronic_failure` for any chronic failure.
    """
```

**Rollup SQL** (per window; parameterized `days` = 7 or 30). Win = `pnl_usd > 0`.
Only closed trades count (status != 'open'). Single statement per window:

```sql
SELECT
    COUNT(*)                                        AS trades,
    SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END)    AS wins,
    SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END)   AS losses,
    COALESCE(SUM(pnl_usd), 0)                       AS total_pnl_usd,
    COALESCE(AVG(pnl_pct), 0)                       AS avg_pnl_pct
FROM paper_trades
WHERE signal_combo = :combo_key
  AND status != 'open'
  AND closed_at >= datetime('now', :window_bound);  -- '-7 days' or '-30 days'
```

`win_rate_pct` = `100.0 * wins / trades` (or 0 if trades = 0). Python post-computes this to avoid divide-by-zero in SQL.

**Suppression rule inside `refresh_combo` (30d row only):**
- `trades >= 20 AND wr_30d < 30` AND `suppressed = 0` → set `suppressed = 1, suppressed_at = now, parole_at = now + 14d, parole_trades_remaining = 5`
- `suppressed = 1 AND parole_trades_remaining = 0 AND wr_30d >= 30` → clear suppression (set `suppressed = 0, suppressed_at = NULL, parole_at = NULL, parole_trades_remaining = NULL`)
- `suppressed = 1 AND parole_trades_remaining = 0 AND wr_30d < 30` → re-suppress (reset `suppressed_at = now, parole_at = now + 14d, parole_trades_remaining = 5`)
- On failure: increment `refresh_failures`; on success: reset to 0
- Always write `last_refreshed = now`

**Note on re-suppression race:** The `parole_trades_remaining = 0` → `= 5` reset happens in a single UPDATE inside `refresh_combo`. A concurrent `should_open` for the same combo would see `remaining > 0` momentarily after the reset, but parole_at is also freshly pushed 14d out, so the concurrent `should_open` evaluates `parole_at > now` and returns `(False, "suppressed")` before reaching the decrement path. No race.

### 5.4 `weekly_digest.py`

```python
async def build_weekly_digest(db, end_date: date) -> str | None:
    """Build Sunday digest. Returns None if no activity last 7d (caller must NOT send)."""

async def send_weekly_digest(db, settings) -> None:
    """
    Orchestrator: build + send via alerter. Called from main.py scheduling block.
    On error: send minimal fallback message with correlation ID (not silent).
    """
```

Digest sections, in order:
1. Header + week range
2. **Combo leaderboard** — top 5 + bottom 5 (30d WR, min 10 trades)
3. **Missed winners** — denominator line ("N missed out of M qualifying winners") + tiered breakdown + pipeline-gap annotation if any
4. **Lead-time medians** — by signal_type, with ok/no_reference/error counts
5. **Suppression log** — combos suppressed/paroled/cleared this week
6. **Fallback counters** — if any `db_error_fallback_allow` fired in last 7d, show count
7. **Chronic refresh failures** — combos that failed refresh ≥3 nights

**Sample rendered output (reference for implementer):**

```
Weekly Feedback — 2026-04-12 to 2026-04-18

[Combo leaderboard — 30d, min 10 trades]
Top 5:
  gainers_early             62.5%  WR  (24 trades, +$142.80)
  first_signal+momentum     58.3%  WR  (12 trades, +$74.10)
  trending_catch            54.2%  WR  (24 trades, +$88.40)
  narrative_prediction      52.4%  WR  (21 trades, +$61.30)
  volume_spike              50.0%  WR  (18 trades, +$34.90)
Bottom 5:
  losers_contrarian         22.2%  WR  (27 trades, -$312.50)  [SUPPRESSED 2026-04-14]
  chain_completed           31.8%  WR  (22 trades, -$45.20)
  ...

[Missed winners — last 7d]
12 missed out of 34 qualifying winners (mcap ≥ $5M, rank ≤ 1500)
  disaster_miss (>1000%): 1
    PEPE2026   +2340%  crossed 2026-04-15 14:22
  major_miss (200–1000%): 3
    WIF        +412%   crossed 2026-04-16 09:05
    BONK       +287%   crossed 2026-04-17 22:40
    ...
  partial_miss (50–200%): 8
    ...
  ⚠ pipeline gap 2026-04-14 03:12 to 05:41 — 2 winners in uncovered_window excluded

[Lead-time — 30d, signal_type medians, 'ok' only]
  first_signal     median -12.4 min  (ok=34, no_ref=5, err=0)    ← beat trending
  gainers_early    median  +8.1 min  (ok=22, no_ref=2, err=0)    ← late
  trending_catch   median  +0.0 min  (ok=24, no_ref=0, err=0)    ← coincident (expected)
  volume_spike     median -34.2 min  (ok=18, no_ref=1, err=0)    ← beat trending

[Suppression log — this week]
  losers_contrarian  SUPPRESSED 2026-04-14 — WR 22.2% (27 trades), parole until 2026-04-28
  chain_completed    PAROLE     2026-04-17 — 2/5 retest trades used

[Fallback counters]
  Suppression fail-opens: 0

[Chronic refresh failures]
  None
```

Empty-state messages per section when nothing to show. If activity is zero for the entire week, `build_weekly_digest` returns None and the orchestrator skips Telegram entirely.

### 5.5 `signals.py` changes

Every `trade_*` function that calls `engine.open_trade`:

1. Compute `combo_key = build_combo_key(signal_type, signals)` once (single derivation site per D20)
2. Call `allow, reason = await suppression.should_open(db, combo_key)`
3. If `not allow`: emit `log.info("signal_suppressed", combo_key=combo_key, reason=reason, coin_id=...)` and skip the trade
4. Otherwise: call `engine.open_trade(..., signal_combo=combo_key)` — passing the already-computed key as a new keyword argument

For dispatchers without multi-signal data (all except `trade_first_signals`), `signals=None` → `combo_key == signal_type` per D13.

### 5.6 `engine.py` changes

`open_trade` gains two responsibilities: accept the pre-computed `signal_combo` kwarg, and populate lead-time columns.

**New signature (additive kwargs — existing callers remain compatible if they pass `signal_combo`):**

```python
async def open_trade(
    self,
    *,
    token_id: str,
    symbol: str,
    name: str,
    chain: str,
    signal_type: str,
    signal_data: dict,
    entry_price: float | None,
    signal_combo: str,           # NEW — computed by signals.py, opaque to engine
    # ... existing kwargs unchanged
) -> PaperTrade | None:
    ...
    now = datetime.now(tz=UTC)
    lead_time, lead_status = await _compute_lead_time_vs_trending(self.db, token_id, now)
    # ... existing logic ...
    # INSERT paper_trades now includes three additional columns:
    #   signal_combo, lead_time_vs_trending_min, lead_time_vs_trending_status
```

All existing signals.py dispatchers must be updated to pass `signal_combo`. The kwarg is required (not defaulted) so a missing call site is a test failure, not a silent NULL.

### 5.7 `db.py` migration logic

Wrapped in `BEGIN EXCLUSIVE` transaction per D18 — `schema_version` row commits only if all DDL succeeded.

```python
async def _migrate_feedback_loop_schema(conn):
    """Per-column additive migration. Idempotent. Asserts post-state. Atomic."""
    try:
        await conn.execute("BEGIN EXCLUSIVE")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL,
                description TEXT
            )
        """)
        await conn.execute("""CREATE TABLE IF NOT EXISTS combo_performance ( ... )""")

        expected_cols = {
            "signal_combo": "TEXT",
            "lead_time_vs_trending_min": "REAL",
            "lead_time_vs_trending_status": "TEXT",
        }
        cur = await conn.execute("PRAGMA table_info(paper_trades)")
        existing = {row[1] for row in await cur.fetchall()}  # row[1] is name
        for col, coltype in expected_cols.items():
            if col in existing:
                log.info("schema_migration_column_action", col=col, action="skip_exists")
            else:
                await conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {col} {coltype}")
                log.info("schema_migration_column_action", col=col, action="added")

        # Indexes (idempotent)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_trades_combo_opened "
                          "ON paper_trades(signal_combo, opened_at)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_trades_token_opened "
                          "ON paper_trades(token_id, opened_at)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_trending_snapshots_coin_id "
                          "ON trending_snapshots(coin_id)")

        # Post-migration assertion
        cur = await conn.execute("PRAGMA table_info(paper_trades)")
        final = {row[1] for row in await cur.fetchall()}
        missing = set(expected_cols) - final
        if missing:
            raise RuntimeError(f"Schema migration incomplete: missing {missing}")

        # Only record version if everything above succeeded
        await conn.execute(
            "INSERT OR IGNORE INTO schema_version VALUES (?, ?, ?)",
            (20260418, datetime.now(tz=UTC).isoformat(), "feedback_loop_v1"),
        )
        await conn.commit()
    except Exception:
        await conn.execute("ROLLBACK")
        log.error("SCHEMA_DRIFT_DETECTED")
        raise
```

---

## 6. Flows

### Flow A — Open trade (hot path)

```
signals.trade_* called
  → build combo_key
  → suppression.should_open(db, combo_key)
      → cold_start | ok | parole_retest  → continue
      → suppressed | parole_exhausted    → log 'signal_suppressed', return
  → engine.open_trade
      → compute signal_combo + lead_time_vs_trending_min + lead_time_vs_trending_status
      → INSERT paper_trades
```

### Flow B — Close trade (unchanged)

No refresh hook. Close_trade only updates row; combo stats get refreshed at nightly sweep. (Dropped fire-and-forget refresh per review D15.)

### Flow C — Nightly sweep (03:00 local)

```
main.py schedule tick detects weekday hour == 3 AND last_combo_refresh_date != today
  → combo_refresh.refresh_all(db)
      → SELECT DISTINCT signal_combo FROM paper_trades WHERE opened_at >= now - 30d
      → for each combo: refresh_combo()
          → on failure: increment refresh_failures
          → on success: reset refresh_failures to 0
      → collect chronic_failures (refresh_failures >= 3)
  → log 'combo_refresh_summary'
  → set last_combo_refresh_date = today
```

### Flow D — Weekly digest (Sunday 09:00 local)

```
main.py schedule tick detects weekday == 6 AND hour == 9 AND last_weekly_digest_date != today
  → weekly_digest.send_weekly_digest(db, settings)
      → build_weekly_digest(db, today)
          → combo_leaderboard('30d') + audit_missed_winners(last 7d) +
            lead_time_breakdown('30d') + suppression_log(last 7d) +
            fallback counter reading + chronic refresh failure list
      → if None: log 'weekly_digest_empty' (no trades last 7d), skip send
      → else: alerter.send_telegram_message (split if >4096 chars)
      → on error: send fallback "Weekly digest failed: <class> [ref=wd-YYYYMMDD-xxxx]"
  → set last_weekly_digest_date = today
```

---

## 7. Missed-winner audit query (rewritten as LEFT JOIN)

```sql
WITH winners AS (
  SELECT coin_id,
         MIN(symbol) AS symbol,
         MIN(name)   AS name,
         MIN(snapshot_at) AS crossed_at,
         MAX(price_change_24h) AS peak_change,
         MAX(market_cap) AS mcap,
         MIN(market_cap_rank) AS best_rank
  FROM gainers_snapshots
  WHERE snapshot_at BETWEEN :start AND :end
    AND price_change_24h >= 50
  GROUP BY coin_id
  HAVING mcap >= 5000000 AND best_rank <= 1500
)
SELECT w.coin_id, w.symbol, w.name, w.crossed_at, w.peak_change, w.mcap, w.best_rank,
       CASE
         WHEN w.peak_change >= 1000 THEN 'disaster_miss'
         WHEN w.peak_change >= 200  THEN 'major_miss'
         ELSE 'partial_miss'
       END AS tier
FROM winners w
LEFT JOIN paper_trades pt
       ON pt.token_id = w.coin_id
      AND pt.opened_at BETWEEN datetime(w.crossed_at, '-30 minutes')
                           AND datetime(w.crossed_at, '+30 minutes')
WHERE pt.id IS NULL;
```

The `LEFT JOIN ... WHERE pt.id IS NULL` pattern correctly references `w.crossed_at` in the join predicate (standard SQLite). Replaces the invalid CTE-NOT-IN-datetime form from the first draft.

**Pipeline-gap handling:** `audit_missed_winners` separately calls `detect_pipeline_gaps(db, start, end)`. Any winner whose `crossed_at` falls inside a detected gap is moved from the tier bucket to an `uncovered_window` bucket in the return dict, and the digest annotates the section accordingly.

**Denominator reporting:** `audit_missed_winners` always returns counts: `winners_total`, `winners_caught` (opened ±30min), `winners_filtered_by_mcap`, `winners_filtered_by_rank`, `winners_missed`, `pipeline_gap_hours`. Weekly digest always shows at least: `"N missed out of M qualifying winners"`. If `M == 0`, emits `audit_query_empty_warning` event.

---

## 8. Settings

```python
# scout/config.py additions
FEEDBACK_SUPPRESSION_MIN_TRADES: int = 20
FEEDBACK_SUPPRESSION_WR_THRESHOLD_PCT: float = 30.0
FEEDBACK_PAROLE_DAYS: int = 14
FEEDBACK_PAROLE_RETEST_TRADES: int = 5
FEEDBACK_MIN_LEADERBOARD_TRADES: int = 10
FEEDBACK_MISSED_WINNER_MIN_PCT: float = 50.0
FEEDBACK_MISSED_WINNER_MIN_MCAP: float = 5_000_000
FEEDBACK_MISSED_WINNER_MAX_RANK: int = 1500
FEEDBACK_MISSED_WINNER_WINDOW_MIN: int = 30
FEEDBACK_PIPELINE_GAP_THRESHOLD_MIN: int = 60         # gap >1h = uncovered
FEEDBACK_WEEKLY_DIGEST_WEEKDAY: int = 6               # 6 = Sunday
FEEDBACK_WEEKLY_DIGEST_HOUR: int = 9                  # 09:00 local
FEEDBACK_COMBO_REFRESH_HOUR: int = 3                  # 03:00 local nightly
FEEDBACK_FALLBACK_ALERT_THRESHOLD: int = 5            # fail-opens/hour before Telegram alert
FEEDBACK_CHRONIC_FAILURE_THRESHOLD: int = 3           # consecutive nightly failures
```

---

## 9. Error handling (revised post-review)

- **`suppression.should_open` DB error** — log `suppression_db_error` with `err_id="SUPP_DB_FAIL"`; return allow; increment fallback counter. Telegram alert fires once per hour if ≥5 failures in the window.
- **`combo_refresh.refresh_combo` single-combo failure** — log + increment `refresh_failures` column. `refresh_all` continues. Chronic failures (≥3 consecutive nights) surface in weekly digest under §5.4 section 7.
- **Lead-time computation error** — log `lead_time_compute_error` with `err_id="LEAD_TIME_CALC"`; store `status='error'` + NULL value. Analytics filters out `status='error'` rows from medians.
- **`weekly_digest` failure** — log + send Telegram fallback with correlation ID: `"Weekly digest failed: <class> [ref=wd-YYYYMMDD-xxxx]. Check logs."`. Never silent.
- **Missed-winner audit with empty denominator** — emit `audit_query_empty_warning` event if zero qualifying winners in window (distinguishes "caught everything" from "query broken" from "no data").
- **Schema migration partial failure** — post-migration assertion raises `RuntimeError`, crashing startup loudly. Pipeline does not proceed on drift.

---

## 10. Testing strategy

TDD order. All tests use `tmp_path` aiosqlite fixtures + existing `conftest.py` factories.

| File | Coverage |
|---|---|
| `tests/test_trading_combo_key.py` | `build_combo_key`: single signal, pair, triple→pair truncation with logged drops, empty signals, sorted output, signal_type always included |
| `tests/test_trading_suppression.py` | `should_open`: cold start, not-suppressed, suppressed pre-parole, parole boundary (`parole_at == now`), parole allow+decrement, parole exhausted, **concurrent decrement test** (see below), DB error fallback, fallback counter triggers Telegram at threshold with 15min cooldown (subsequent fallbacks in cooldown do NOT re-alert; after cooldown expires they DO re-alert), partial DB state (30d row exists but 7d missing), parole reset path after re-suppression |
| `tests/test_trading_combo_refresh.py` | 7d/30d rollup math; suppression boundary tests: `trades=20 AND wr=30.0` (does NOT trigger), `trades=20 AND wr=29.99` (triggers), `trades=19 AND wr=0` (does NOT trigger); parole auto-clear on WR recovery; re-suppression on recovery failure resets `suppressed_at` (not original); idempotency (calling refresh twice while suppressed doesn't double-update); zero-trade combo handling; refresh_failures increments on failure, resets on success; chronic failure detected at threshold |
| `tests/test_trading_analytics.py` | `combo_leaderboard` min_trades filter; missed-winner tier boundaries at `peak=50` (partial), `199.99` (partial), `200` (major), `999.99` (major), `1000` (disaster); catch window boundaries: opened at `crossed_at - 30min` (caught), `crossed_at - 31min` (missed), `crossed_at + 30min` (caught), `crossed_at + 31min` (missed); mcap filter boundary `4_999_999` (excluded) vs `5_000_000` (included); multi-snapshot same coin uses `MIN(crossed_at)` for catch window; lead-time breakdown filters `status='ok'` only; lead-time counts include `count_no_reference` and `count_error`; `detect_pipeline_gaps` correctly identifies >60min gap; missed-winner with pipeline-gap moves to `uncovered_window` bucket; empty denominator emits warning |
| `tests/test_trading_weekly_digest.py` | message structure all sections; Telegram length split at 4096 preserves line integrity (no mid-number truncation); empty-state returns None + `send_weekly_digest` does NOT call Telegram in that case; fallback includes correlation ID on error; fallback counter section only renders when nonzero |
| `tests/test_trading_engine_leadtime.py` | `open_trade` populates `signal_combo`; negative `lead_time_vs_trending_min` when we beat trending (status='ok'); positive when late (status='ok'); NULL + status='no_reference' when coin never trended; NULL + status='error' on computation exception (still inserts row — does not block trade) |
| `tests/test_trading_signals_integration.py` *(NEW)* | End-to-end: for each `trade_*` dispatcher, verify a suppressed combo results in zero `engine.open_trade` calls + a `signal_suppressed` structured log. Regression check: existing non-suppressed path still opens trades correctly. |
| `tests/test_trading_db_migration.py` *(NEW)* | Per-column migration: fresh DB (all columns added), already-migrated DB (all skipped), partial DB (only some columns present — adds missing ones, not re-add existing), post-migration assertion raises on missing column, `schema_version` row inserted |

**Concurrent decrement test spec (what "concurrent" means in asyncio):**

`asyncio.gather(should_open(db, "bad_combo"), should_open(db, "bad_combo"))` where `bad_combo` is pre-seeded with `suppressed=1, parole_at=now-1h, parole_trades_remaining=1`. Even though the event loop is single-threaded, the two coroutines interleave at `await` points inside `should_open`. Under D16 (`BEGIN IMMEDIATE`), the first coroutine to reach the transaction acquires the write lock; the second blocks until COMMIT, re-reads the row, and sees `remaining=0`. Assertion: exactly one result is `(True, "parole_retest")` and the other is `(False, "parole_exhausted")`. Without `BEGIN IMMEDIATE`, both coroutines would read `remaining=1`, both would UPDATE, and the test would fail — proving the lock is doing the work.

Regression gate: existing `tests/test_trading_*.py` must not regress. Daily digest numbers in particular must be byte-identical (new columns are additive, don't affect existing queries).

---

## 11. Deployment plan

- Branch: `feat/paper-trading-feedback-loop`
- PR: `#29 — feat(trading): feedback loop with combo stats, suppression, weekly digest, missed-winner audit`
- Dev: pytest full suite green (expect ~60 new tests)
- VPS: pull, restart `gecko-pipeline.service`. No env var changes needed for defaults.
- First 24h: monitor for schema migration success, post-migration assertion passes, first nightly refresh at 03:00 runs cleanly
- Day 2 onwards: no suppression fires yet (no combo has 20 trades in 7d). Combo rows populate.
- Day 7: first Sunday weekly digest — validate it renders with all sections
- Day 14+: first suppressions may fire as combos accumulate 20+ trades

**Rollback:**
- Suppression misbehaves: `UPDATE combo_performance SET suppressed = 0` (one SQL statement, instant — does not require redeploy)
- Weekly digest errors: cron failure doesn't affect pipeline; triage offline
- Schema migration assertion fails at startup: pipeline crashes loudly — easier to detect than silent drift

---

## 12. Success criteria (revised — testable or observable)

**Automated (test suite):**
- All 8 test files pass
- `refresh_all` completes in <5s with seeded fixture of 1000 trades across 50 combos (pytest-benchmark or `time.monotonic` assertion)
- Daily digest output byte-identical vs main on a seeded fixture (regression proof)

**Production-observable (week 1):**
- Weekly digest delivers Sunday 09:00 with all sections (or explicit empty-state message per section)
- Schema migration log events (`schema_migration_column_action`) show per-column actions
- Denominator line in missed-winner section: if `winners_total == 0`, `audit_query_empty_warning` event present in logs

**Production-observable (week 4+):**
- At least one combo surfaces in bottom-5 leaderboard with <50% WR (proof the rollup works — regardless of whether it hits suppression threshold)
- If no combo has yet been suppressed: either signals are strong (good) or threshold should be revisited (signal to act on)

---

## 13. Non-goals

- Predicting *which* combos will be good (just measure what is)
- Live execution changes (still research-only + paper)
- Social/on-chain integration (separate tracks)
- Auto-tuning thresholds (Sprint 2+)
- Dashboard UI (CLI + Telegram suffice)
- Backfilling lead-time for historical trades (forward-only)

---

## 14. Deferred to Sprint 2

- Lead-time vs first +5% price move (requires `price_history_cg` table)
- Enriching `trade_gainers` / `trade_trending` / other dispatchers to carry upstream signals (enables real pair-combos instead of single-signal-only)
- Triple-combo stats
- Dashboard UI
- Auto-threshold tuning based on observed WR distribution
