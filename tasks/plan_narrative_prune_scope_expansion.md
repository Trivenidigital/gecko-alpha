**New primitives introduced:** Six new Settings fields (`VOLUME_SPIKES_RETENTION_DAYS`, `MOMENTUM_7D_RETENTION_DAYS`, `TRENDING_SNAPSHOTS_RETENTION_DAYS`, `LEARN_LOGS_RETENTION_DAYS`, `CHAIN_MATCHES_RETENTION_DAYS`, `HOLDER_SNAPSHOTS_RETENTION_DAYS`), a new Pydantic `@model_validator(mode='after')` enforcing 30d floor on backtest-CLI-consumed tables (`TRENDING_SNAPSHOTS`/`CHAIN_MATCHES`/`VOLUME_SPIKES`), six new `Database.prune_*` methods, five new index migrations via cycle 1's `_migrate_scanned_at_index` helper extended (`idx_volume_spikes_detected_at`, `idx_momentum_7d_detected_at`, `idx_trending_snapshots_snapshot_at`, `idx_learn_logs_created_at`, `idx_holder_snapshots_scanned_at`), hourly wiring inside `_run_hourly_maintenance`, deletion of the now-empty `_run_extra_table_prune` helper in `scout/narrative/agent.py`, structured log events `{table}_pruned` and `{table}_prune_failed` for each table.

# Plan: BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION — parameterize + decouple remaining 6 narrative-owned prunes

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend PR #136's hardening pattern to the remaining 6 tables in `scout/narrative/agent.py:86-93` (`volume_spikes`, `momentum_7d`, `trending_snapshots`, `learn_logs`, `chain_matches`, `holder_snapshots`). Same defect class — hardcoded retention values violating "no hardcoded thresholds" rule; coupling to narrative daily-learn loop creating silent-failure surface; no per-table telemetry.

**Architecture:** 6× the score/volume pattern. Each table gets its own Settings field, prune method on `Database`, and hourly-loop call site. Once all 6 are migrated out, `_run_extra_table_prune` becomes empty and is deleted.

**Tech Stack:** aiosqlite, Pydantic v2 Settings, structlog, pytest-asyncio.

---

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| SQLite retention / pruning primitives | None (Hermes skill hub 2026-05-16 search via WebFetch DevOps category) | Build in-tree. |
| Multi-table data-lifecycle management | None — Hermes covers external API/agent orchestration, not project-internal DB maintenance | Build in-tree. |

**awesome-hermes-agent ecosystem check:** repo 404 on 2026-05-16. **Verdict:** mirror PR #136 (`tasks/design_score_volume_pruning_harden.md`) custom-code path.

---

## Drift check (post-fetch)

- **Step 0** (memory `feedback_drift_check_after_pull.md` from cycle 1): `git fetch && git log --oneline -10 origin/master` → top is PR #136 merge (`00abaa7`) + PR #135 merge (`42b8d01`); nothing newer. Clean.
- `grep prune_volume_spikes scout/` → no in-tree method exists. Same for momentum_7d / trending_snapshots / learn_logs / chain_matches / holder_snapshots. All 6 are still pruned only via the narrative daily-loop helper.
- `scout/narrative/agent.py:86-93` lists exactly the 6 tables this plan targets — verified after cycle 1's score/volume extraction.

**Backlog reference:** `BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION` filed in `backlog.md` from cycle 1 PR #136. Decision-by: 6 weeks (lower urgency than score/volume; slower write rates).

---

## Per-table reader-window analysis (post-V8 + V9 plan-review fold)

Each table has multiple readers across the codebase. V8 reviewer surfaced backtest-CLI consumers (default `--days=30`) that the plan's original draft missed. Defaults below preserve in-tree narrative-loop hardcoded values WHERE the readers stay below them, and BUMP UP where backtest CLI readers exceed them.

| Table | Old hardcoded | New default | Longest-windowed reader | Why this default? |
|---|---|---|---|---|
| `volume_spikes` | 30d (`detected_at`) | **45d** | `scripts/backtest_conviction_lock.py:298,886` `--days` default 30 | Backtest CLI default 30d; 15d headroom prevents boundary-coincidence silent truncation when operator runs `--days 30` |
| `momentum_7d` | 30d (`detected_at`) | **30d** (unchanged) | `scout/spikes/detector.py:300,488` `-7 days` | All readers ≤7d; 30d generous; no backtest reader observed |
| `trending_snapshots` | 7d (`snapshot_at`) | **30d** | `scripts/backtest_conviction_lock.py:894` `--days` default 30 | **V8 MUST-FIX:** 7d default silently truncated backtest cohort at the CLI default. Plan's original "7d defensive" was wrong — bumped to match backtest expectation |
| `learn_logs` | 90d (`created_at`) | **90d** (unchanged) | `scout/narrative/learner.py:375` `ORDER BY created_at DESC LIMIT 7` + `dashboard/db.py:380` LIMIT-? | LIMIT-N reads; 90d over-provisioned, kept defensive |
| `chain_matches` | 30d (`completed_at`) | **45d** | `scout/backtest.py:161` `--days` default 30 + `scripts/backtest_v1_signal_stacking.py:237,279` `-30 days` literal | **V8 MUST-FIX:** 30d at backtest CLI default coincides exactly; 15d headroom |
| `holder_snapshots` | 14d (`scanned_at`) | **14d** (unchanged) | `scout/db.py:4198` `ORDER BY scanned_at DESC LIMIT 1` (only reader) | LIMIT-1 read; per memory `findings_silent_failure_audit_2026_05_11.md §2.5` writer is dormant (BL-020 never wired) — irrelevant until activated |

### Extended reader inventory (V8-surfaced; engineer must NOT lower below these)

- `volume_spikes`: also `scout/losers/tracker.py:207` (`MIN` lead-time), `scout/spikes/detector.py:131,325,345,488` (≤7d windows), `scout/briefing/collector.py:363` (-N hours), `dashboard/db.py:1455` (±24h)
- `trending_snapshots`: `scout/trending/tracker.py:200,484` (-24h, -N hours), `scout/trading/engine.py:39` (`MIN` pre-filtered upstream), `scout/trading/signals.py:418` (-5 minutes)
- `chain_matches`: `scout/chains/tracker.py:478,721,1019` (cooldown 12h, stuck-row 48h, 12h), `scout/chains/patterns.py:270` **no time bound** (all-time aggregate — pruning caps "all-time" hit-rate sample; document but not load-bearing), `scout/trading/signals.py:782` (-5 minutes), `scout/briefing/collector.py:378` (-N hours), `dashboard/db.py:444` (LIMIT-?)
- `momentum_7d`: `scout/spikes/detector.py:226,280,295,300,304,488` (all ≤7d), `dashboard/db.py:1466` (±24h)

### Cross-field validator decision (REVISED post-V8)

Add `_validate_backtest_cli_retention_floor` model_validator enforcing 30d floor on `TRENDING_SNAPSHOTS_RETENTION_DAYS` / `CHAIN_MATCHES_RETENTION_DAYS` / `VOLUME_SPIKES_RETENTION_DAYS`. Backtest CLI defaults (`--days 30`) are effectively the read-window for analytical consumers; retention below 30d on any of these three silently truncates the cohort.

```python
@model_validator(mode="after")
def _validate_backtest_cli_retention_floor(self) -> "Settings":
    """V8 plan-review fold: backtest CLI tools default --days=30 against
    trending_snapshots / chain_matches / volume_spikes. Retention below 30
    silently truncates the backtest cohort at the CLI default."""
    backtest_floor = 30
    for field_name in (
        "TRENDING_SNAPSHOTS_RETENTION_DAYS",
        "CHAIN_MATCHES_RETENTION_DAYS",
        "VOLUME_SPIKES_RETENTION_DAYS",
    ):
        value = getattr(self, field_name)
        if value < backtest_floor:
            raise ValueError(
                f"{field_name}={value} must be >= {backtest_floor} to cover "
                f"backtest CLI default --days=30. Lower retention silently "
                f"truncates backtest cohorts."
            )
    return self
```

The other 3 tables (`momentum_7d` / `learn_logs` / `holder_snapshots`) have no backtest-CLI consumer requiring a floor; per-table operator override via `.env` remains the knob.

---

## File map

- **Create:** none (extending existing files)
- **Modify:**
  - `scout/config.py` — add 6 Settings fields next to `SCORE_HISTORY_RETENTION_DAYS` block + `_validate_backtest_cli_retention_floor` model_validator
  - `scout/db.py` — add 6 prune methods next to `prune_score_history` / `prune_volume_snapshots` (~line 4815); add 5 new `_migrate_<table>_<col>_idx_v1` migrations using cycle 1's `_migrate_scanned_at_index` helper at db.py:3677 (skip `chain_matches` per V9 NICE-TO-HAVE — slow growth, EXPLAIN-gate at PR-stage)
  - `scout/main.py` — extend `_run_hourly_maintenance` body with 6 new prune calls
  - `scout/narrative/agent.py` — delete `_run_extra_table_prune` helper entirely; replace call site with a single comment noting the migration
- **Test:**
  - `tests/test_config.py` — 6 default tests + env-override + 1 validator-raises test
  - `tests/test_db.py` — 12 prune method tests (2 per table) + 5 EXPLAIN-uses-index tests (one per new index)
  - `tests/test_hourly_maintenance.py` — extend with 6 assert-prune-called tests
  - `tests/test_narrative_agent_prune.py` — DELETE or update to assert helper is gone

---

## Tasks

### Task 1: 6 Settings fields

**Files:** `scout/config.py` (~line 250 next to existing SCORE_HISTORY block); `tests/test_config.py` (append)

- [ ] **Step 1.1: Write the failing tests**

```python
def test_narrative_table_retention_defaults():
    s = Settings(_env_file=None, TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
    assert s.VOLUME_SPIKES_RETENTION_DAYS == 30
    assert s.MOMENTUM_7D_RETENTION_DAYS == 30
    assert s.TRENDING_SNAPSHOTS_RETENTION_DAYS == 7
    assert s.LEARN_LOGS_RETENTION_DAYS == 90
    assert s.CHAIN_MATCHES_RETENTION_DAYS == 30
    assert s.HOLDER_SNAPSHOTS_RETENTION_DAYS == 14


def test_narrative_table_retention_env_override():
    s = Settings(_env_file=None, TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k",
                 VOLUME_SPIKES_RETENTION_DAYS=60)
    assert s.VOLUME_SPIKES_RETENTION_DAYS == 60
```

- [ ] **Step 1.2: Run → FAIL**

```
uv run pytest tests/test_config.py -k "narrative_table_retention" -v
```

- [ ] **Step 1.3: Add the 6 fields in `scout/config.py`** (after `VOLUME_SNAPSHOTS_RETENTION_DAYS`):

```python
    # -------- Narrative-owned table retention (BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION) --------
    # Hourly prune via main._run_hourly_maintenance. Defaults adjusted per V8
    # plan-review fold to cover backtest CLI default --days=30 + 15d headroom
    # for trending/chain/volume (backtest_conviction_lock.py / backtest.py).
    # Per-table reader-window analysis in tasks/plan_narrative_prune_scope_expansion.md.
    VOLUME_SPIKES_RETENTION_DAYS: int = 45  # was 30 hardcoded; covers backtest --days=30 + headroom
    MOMENTUM_7D_RETENTION_DAYS: int = 30
    TRENDING_SNAPSHOTS_RETENTION_DAYS: int = 30  # was 7 hardcoded; backtest CLI default 30 silently truncated
    LEARN_LOGS_RETENTION_DAYS: int = 90
    CHAIN_MATCHES_RETENTION_DAYS: int = 45  # was 30 hardcoded; covers backtest --days=30 + headroom
    HOLDER_SNAPSHOTS_RETENTION_DAYS: int = 14
```

- [ ] **Step 1.4: Add the model_validator after the fields**

```python
    @model_validator(mode="after")
    def _validate_backtest_cli_retention_floor(self) -> "Settings":
        """V8 plan-review fold: backtest CLI tools default --days=30 against
        trending_snapshots / chain_matches / volume_spikes. Retention below 30
        silently truncates the backtest cohort at the CLI default."""
        backtest_floor = 30
        for field_name in (
            "TRENDING_SNAPSHOTS_RETENTION_DAYS",
            "CHAIN_MATCHES_RETENTION_DAYS",
            "VOLUME_SPIKES_RETENTION_DAYS",
        ):
            value = getattr(self, field_name)
            if value < backtest_floor:
                raise ValueError(
                    f"{field_name}={value} must be >= {backtest_floor} to cover "
                    f"backtest CLI default --days=30. Lower retention silently "
                    f"truncates backtest cohorts."
                )
        return self
```

Also add a test:

```python
def test_backtest_cli_retention_floor_validator():
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="must be >= 30 to cover backtest CLI"):
        Settings(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k",
            TRENDING_SNAPSHOTS_RETENTION_DAYS=14,
        )
```

- [ ] **Step 1.5: Run → PASS**
- [ ] **Step 1.6: Commit** — `feat(config): 6 narrative-owned-table retention fields + backtest-CLI-floor validator`

---

### Task 2: 6 Database.prune_* methods

**Files:** `scout/db.py` (after `prune_volume_snapshots`); `tests/test_db.py` (append)

Each method mirrors `prune_score_history` exactly except for the table + timestamp-column name. The 6 tables don't share a column name (`detected_at` / `snapshot_at` / `created_at` / `completed_at` / `scanned_at`), so each method needs the column hardcoded.

- [ ] **Step 2.1: Write 12 failing tests** (2 per table; mirror score-side pattern):

```python
async def test_prune_volume_spikes_keeps_recent(db):
    now = datetime.now(timezone.utc)
    await db._conn.execute(
        "INSERT INTO volume_spikes (coin_id, symbol, detected_at) VALUES (?, ?, ?)",
        ("c-recent", "RECENT", (now - timedelta(days=5)).isoformat()),
    )
    await db._conn.execute(
        "INSERT INTO volume_spikes (coin_id, symbol, detected_at) VALUES (?, ?, ?)",
        ("c-old", "OLD", (now - timedelta(days=45)).isoformat()),
    )
    await db._conn.commit()
    deleted = await db.prune_volume_spikes(keep_days=30)
    assert deleted == 1


async def test_prune_volume_spikes_empty_returns_zero(db):
    assert await db.prune_volume_spikes(keep_days=30) == 0
```

…repeat for `momentum_7d` / `trending_snapshots` / `learn_logs` / `chain_matches` / `holder_snapshots` with each table's actual schema.

**NOTE for engineer:** read each table's `CREATE TABLE` in `scout/db.py` first to get the actual column list (some require additional NOT NULL columns at insert time). Check before writing the seed INSERT in each test.

- [ ] **Step 2.2: Run all 12 → FAIL**

- [ ] **Step 2.3: Implement 6 methods in `scout/db.py`**:

```python
    async def prune_volume_spikes(self, *, keep_days: int) -> int:
        """Delete volume_spikes rows older than ``keep_days``. Returns rowcount."""
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM volume_spikes WHERE detected_at <= ?",
            (cutoff,),
        )
        await self._conn.commit()
        return cur.rowcount or 0

    async def prune_momentum_7d(self, *, keep_days: int) -> int:
        """Delete momentum_7d rows older than ``keep_days``. Returns rowcount."""
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM momentum_7d WHERE detected_at <= ?",
            (cutoff,),
        )
        await self._conn.commit()
        return cur.rowcount or 0

    async def prune_trending_snapshots(self, *, keep_days: int) -> int:
        """Delete trending_snapshots rows older than ``keep_days``. Returns rowcount."""
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM trending_snapshots WHERE snapshot_at <= ?",
            (cutoff,),
        )
        await self._conn.commit()
        return cur.rowcount or 0

    async def prune_learn_logs(self, *, keep_days: int) -> int:
        """Delete learn_logs rows older than ``keep_days``. Returns rowcount."""
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM learn_logs WHERE created_at <= ?",
            (cutoff,),
        )
        await self._conn.commit()
        return cur.rowcount or 0

    async def prune_chain_matches(self, *, keep_days: int) -> int:
        """Delete chain_matches rows older than ``keep_days``. Returns rowcount."""
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM chain_matches WHERE completed_at <= ?",
            (cutoff,),
        )
        await self._conn.commit()
        return cur.rowcount or 0

    async def prune_holder_snapshots(self, *, keep_days: int) -> int:
        """Delete holder_snapshots rows older than ``keep_days``. Returns rowcount."""
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM holder_snapshots WHERE scanned_at <= ?",
            (cutoff,),
        )
        await self._conn.commit()
        return cur.rowcount or 0
```

- [ ] **Step 2.4: Run → PASS**

- [ ] **Step 2.5: Commit** — `feat(db): 6 prune methods for narrative-owned tables`

---

### Task 2.5: Index migrations for 5 tables (V9 plan-review fold)

V9 reviewer confirmed all 6 tables have leading-column-mismatch indexes (or zero for `learn_logs`) — same defect class as cycle 1's `score_history`/`volume_snapshots`. The plan originally deferred this to PR-stage; V9 pulled it into design phase to avoid a re-cycle.

Reuse cycle 1's helper at `scout/db.py:3677` (`_migrate_scanned_at_index`). It already accepts `table` / `index_name` / `migration_name` kwargs — extends cleanly. Add 5 new `_migrate_*` wrappers + register in `initialize()` after `_migrate_volume_snapshots_scanned_at_index`.

Tables + new indexes:

| Table | Index column | New index name | Migration name | Severity |
|---|---|---|---|---|
| `volume_spikes` | `detected_at` | `idx_volume_spikes_detected_at` | `volume_spikes_detected_at_idx_v1` | MUST (V9 SHOULD-FIX) |
| `momentum_7d` | `detected_at` | `idx_momentum_7d_detected_at` | `momentum_7d_detected_at_idx_v1` | MUST (V9 SHOULD-FIX) |
| `trending_snapshots` | `snapshot_at` | `idx_trending_snapshots_snapshot_at` | `trending_snapshots_snapshot_at_idx_v1` | MUST (V9 MUST-FIX) |
| `learn_logs` | `created_at` | `idx_learn_logs_created_at` | `learn_logs_created_at_idx_v1` | MUST (V9 MUST-FIX) |
| `holder_snapshots` | `scanned_at` | `idx_holder_snapshots_scanned_at` | `holder_snapshots_scanned_at_idx_v1` | MUST (V9 MUST-FIX) |
| `chain_matches` | (skipped) | — | — | V9 NICE-TO-HAVE — slow growth, EXPLAIN-gate at PR-stage. Note this in PR description. |

**Important:** the cycle 1 helper at `db.py:3677` hardcodes `ON {table}(scanned_at)` in line 3690. NEEDS extension to accept the column name as a parameter — currently only handles `scanned_at`. The 5 new tables have 3 different columns (`detected_at`, `snapshot_at`, `created_at`). Refactor needed:

```python
async def _migrate_scanned_at_index(
    self, *, table: str, column: str = "scanned_at", index_name: str, migration_name: str
) -> None:
```

…and update line 3690:

```python
            await conn.execute(
                f"CREATE INDEX IF NOT EXISTS {index_name} ON {table}({column})"
            )
```

The 2 existing callers (score_history / volume_snapshots) use the default `column="scanned_at"` — backward compatible.

- [ ] **Step 2.5.1: Write 5 failing EXPLAIN tests** (mirror cycle 1's pattern):

```python
@pytest.mark.parametrize("table,col,index", [
    ("volume_spikes", "detected_at", "idx_volume_spikes_detected_at"),
    ("momentum_7d", "detected_at", "idx_momentum_7d_detected_at"),
    ("trending_snapshots", "snapshot_at", "idx_trending_snapshots_snapshot_at"),
    ("learn_logs", "created_at", "idx_learn_logs_created_at"),
    ("holder_snapshots", "scanned_at", "idx_holder_snapshots_scanned_at"),
])
async def test_prune_uses_new_idx(db, table, col, index):
    cur = await db._conn.execute(
        f"EXPLAIN QUERY PLAN DELETE FROM {table} WHERE {col} <= ?",
        ("2026-01-01T00:00:00+00:00",),
    )
    plan = await cur.fetchall()
    plan_str = " ".join(str(row[3]) for row in plan)
    assert index in plan_str, f"{index} not used: {plan_str}"
```

- [ ] **Step 2.5.2: Run → FAIL** (5 tests)

- [ ] **Step 2.5.3: Refactor `_migrate_scanned_at_index` to accept `column` kwarg** (+ verify cycle 1's score/volume callers still pass)

- [ ] **Step 2.5.4: Add 5 new `_migrate_*` wrappers in `scout/db.py`** (after the volume_snapshots one):

```python
    async def _migrate_volume_spikes_detected_at_index(self) -> None:
        await self._migrate_scanned_at_index(
            table="volume_spikes", column="detected_at",
            index_name="idx_volume_spikes_detected_at",
            migration_name="volume_spikes_detected_at_idx_v1",
        )

    async def _migrate_momentum_7d_detected_at_index(self) -> None:
        await self._migrate_scanned_at_index(
            table="momentum_7d", column="detected_at",
            index_name="idx_momentum_7d_detected_at",
            migration_name="momentum_7d_detected_at_idx_v1",
        )

    async def _migrate_trending_snapshots_snapshot_at_index(self) -> None:
        await self._migrate_scanned_at_index(
            table="trending_snapshots", column="snapshot_at",
            index_name="idx_trending_snapshots_snapshot_at",
            migration_name="trending_snapshots_snapshot_at_idx_v1",
        )

    async def _migrate_learn_logs_created_at_index(self) -> None:
        await self._migrate_scanned_at_index(
            table="learn_logs", column="created_at",
            index_name="idx_learn_logs_created_at",
            migration_name="learn_logs_created_at_idx_v1",
        )

    async def _migrate_holder_snapshots_scanned_at_index(self) -> None:
        await self._migrate_scanned_at_index(
            table="holder_snapshots", column="scanned_at",
            index_name="idx_holder_snapshots_scanned_at",
            migration_name="holder_snapshots_scanned_at_idx_v1",
        )
```

- [ ] **Step 2.5.5: Register 5 calls in `initialize()` after `_migrate_volume_snapshots_scanned_at_index`** (around line 110):

```python
        await self._migrate_volume_spikes_detected_at_index()
        await self._migrate_momentum_7d_detected_at_index()
        await self._migrate_trending_snapshots_snapshot_at_index()
        await self._migrate_learn_logs_created_at_index()
        await self._migrate_holder_snapshots_scanned_at_index()
```

- [ ] **Step 2.5.6: Run → PASS** (all 5 EXPLAIN tests pass)

- [ ] **Step 2.5.7: Commit** — `feat(db): 5 index migrations for narrative-owned-table prune coverage`

**Deploy cost note:** 5 indexes × ~30-60s each = up to ~5 min total migration time at startup. Per the V9 reviewer's row-count estimates, most of these tables are smaller than cycle 1's 6M-row tables (volume_spikes / momentum_7d / trending_snapshots write ~daily, not hourly), so actual cost likely 5-15s per index. SSH-check row counts before deploy to forecast accurately.

**§9a runtime check before deploy:** SSH srilu, `sqlite3 scout.db "SELECT COUNT(*) FROM volume_spikes;"` (and the other 4) to forecast migration duration.

---

### Task 3: Wire 6 prune calls into `_run_hourly_maintenance`

**Files:** `scout/main.py` (after the score/volume block inside `_run_hourly_maintenance`); `tests/test_hourly_maintenance.py` (append)

- [ ] **Step 3.1: Write 6 failing integration tests**

```python
async def test_run_hourly_maintenance_calls_volume_spikes_prune(tmp_path):
    settings = _make_settings(tmp_path)
    db = _make_db_mock()
    db.prune_volume_spikes = AsyncMock(return_value=0)
    session = MagicMock()
    logger = MagicMock()
    await _run_hourly_maintenance(db, session, settings, logger)
    db.prune_volume_spikes.assert_awaited_once_with(
        keep_days=settings.VOLUME_SPIKES_RETENTION_DAYS
    )
```

…repeat for the other 5 tables. Update `_make_db_mock` to include the 6 new prune methods returning `AsyncMock(return_value=0)`.

- [ ] **Step 3.2: Run → FAIL**

- [ ] **Step 3.3: Wire the 6 prune calls in `_run_hourly_maintenance`** (after the volume_snapshots block):

```python
    # BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION: 6 narrative-owned tables
    # parameterized + decoupled from narrative daily loop.
    for prune_name, retention_attr in [
        ("prune_volume_spikes", "VOLUME_SPIKES_RETENTION_DAYS"),
        ("prune_momentum_7d", "MOMENTUM_7D_RETENTION_DAYS"),
        ("prune_trending_snapshots", "TRENDING_SNAPSHOTS_RETENTION_DAYS"),
        ("prune_learn_logs", "LEARN_LOGS_RETENTION_DAYS"),
        ("prune_chain_matches", "CHAIN_MATCHES_RETENTION_DAYS"),
        ("prune_holder_snapshots", "HOLDER_SNAPSHOTS_RETENTION_DAYS"),
    ]:
        try:
            keep_days = getattr(settings, retention_attr)
            rows = await getattr(db, prune_name)(keep_days=keep_days)
            if rows:
                logger.info(
                    f"{prune_name}_done", rows_deleted=rows, keep_days=keep_days
                )
        except Exception:
            logger.exception(f"{prune_name}_failed")
```

**Engineer judgement call:** the loop above is DRYer but slightly less greppable than 6 explicit blocks. PR #136 used explicit blocks for score/volume. Pick whichever — both are acceptable; the loop is ~12 LOC, the explicit form is ~36 LOC. Recommend loop for code density.

- [ ] **Step 3.4: Run → PASS**

- [ ] **Step 3.5: Commit** — `feat(main): hourly prune of 6 narrative-owned tables via Settings`

---

### Task 4: Delete `_run_extra_table_prune` helper

**Files:** `scout/narrative/agent.py` (delete helper + update call site); `tests/test_narrative_agent_prune.py` (delete or update)

After Task 3 lands, `_run_extra_table_prune` has zero entries left in its list — it's dead code. Delete it.

- [ ] **Step 4.1: Update `tests/test_narrative_agent_prune.py`**

Two options:
- DELETE the test file (cleanest if no other narrative agent tests live there)
- REPLACE its content with a regression test asserting the symbol is gone:

```python
def test_run_extra_table_prune_helper_is_removed():
    """BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION: all 6 tables migrated to
    scout.main._run_hourly_maintenance. Helper should not be re-introduced."""
    import scout.narrative.agent as narrative_agent
    assert not hasattr(narrative_agent, "_run_extra_table_prune"), (
        "Helper was migrated out; reintroducing it suggests a regression "
        "from the parameterize+decouple pattern."
    )
```

Engineer pick.

- [ ] **Step 4.2: Delete `_run_extra_table_prune` from `scout/narrative/agent.py`**

Delete lines 68-103 (the helper definition).

- [ ] **Step 4.3: Update the call site in `narrative_agent_loop`**

Find the `await _run_extra_table_prune(db)` call (was around line 679 pre-extraction; now around the daily-learn-complete block) and replace with:

```python
                    # BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION (2026-05-16):
                    # all narrative-owned table prunes are now run hourly from
                    # scout.main._run_hourly_maintenance via Settings retention.
                    # No daily-loop prune remains here.
```

- [ ] **Step 4.4: Run regression** — full test suite to confirm no caller broke

```
uv run pytest --tb=short -q  # on srilu (Windows OPENSSL workaround)
```

- [ ] **Step 4.5: Commit** — `refactor(narrative): delete _run_extra_table_prune (migration complete)`

---

### Task 5: Update backlog status + close BL-NEW

**Files:** `backlog.md`

- [ ] **Step 5.1:** Edit `BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION` entry in `backlog.md`. Change status to:

```markdown
**Status:** SHIPPED 2026-05-XX — PR #<num> (`<commit>`). All 6 tables (volume_spikes / momentum_7d / trending_snapshots / learn_logs / chain_matches / holder_snapshots) now parameterized via Settings + hourly-pruned via `scout.main._run_hourly_maintenance`. The narrative daily-loop `_run_extra_table_prune` helper deleted (was at `scout/narrative/agent.py:68-103` post-PR-#136).
```

- [ ] **Step 5.2: Commit** — `docs(backlog): close BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION`

---

## Test plan summary

- 6 config default tests + 6 env-override tests (12 total, may batch via parametrize)
- 1 backtest-CLI-floor validator test
- 12 prune method tests (2 per table × 6 tables)
- 5 EXPLAIN-uses-index tests (one per new index migration)
- 6 hourly-maintenance integration tests (one per new prune call)
- 1 narrative-agent-helper-deleted regression test
- Full regression must pass on srilu (Windows OPENSSL workaround per memory)

Total: ~37 new tests.

---

## Deployment verification (operator-gated)

1. `journalctl -u gecko-pipeline --since "10 minutes ago" | grep -E "_pruned|_prune_failed"` — should see info-when-rows>0, silent-when-zero for each table
2. Spot-check via `sqlite3 scout.db "SELECT COUNT(*) FROM <table> WHERE <col> < datetime('now', '-N days')"` for any table; expect 0
3. Revert path: per-table `.env` `<TABLE>_RETENTION_DAYS=365`; full rollback = revert PR

---

## Out of scope

- New index on `chain_matches.completed_at` — V9 NICE-TO-HAVE; slow-growth table tolerates table scan. PR-stage EXPLAIN check; promote to migration if EXPLAIN shows SCAN with row count > a few hundred.
- Curl-direct alert on prune failure — already filed as `BL-NEW-SCORE-VOLUME-PRUNE-ALERT` from PR #136; this PR's prunes will benefit when that lands.
- VPS deployment — operator-gated per user "do not deploy until PR is reviewed by me" rule.
- Activating `holder_snapshots` writer (BL-020) — out of scope; the retention setting applies for when the writer eventually fires.

**Post-V8/V9 fold changes from original plan:**
- Cross-field validator ADDED (`_validate_backtest_cli_retention_floor`) — backtest CLI tools' `--days=30` default is effectively a load-bearing downstream window for trending/chain/volume
- 5 index migrations PULLED IN to design/build phase (was deferred to PR-stage)
- Defaults BUMPED for trending_snapshots (7→30), chain_matches (30→45), volume_spikes (30→45)
- Test count: 31 → 37
