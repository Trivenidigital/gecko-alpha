**New primitives introduced:** Six new Settings fields (`VOLUME_SPIKES_RETENTION_DAYS`, `MOMENTUM_7D_RETENTION_DAYS`, `TRENDING_SNAPSHOTS_RETENTION_DAYS`, `LEARN_LOGS_RETENTION_DAYS`, `CHAIN_MATCHES_RETENTION_DAYS`, `HOLDER_SNAPSHOTS_RETENTION_DAYS`), six new `Database.prune_*` methods, hourly wiring inside the existing `_run_hourly_maintenance` helper in `scout/main.py`, deletion of the now-empty `_run_extra_table_prune` helper in `scout/narrative/agent.py` (or list reduction to zero), structured log events `{table}_pruned` and `{table}_prune_failed` for each of the six tables.

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

## Per-table reader-window analysis

Each table has at least one reader. Retention must NOT truncate analytically-meaningful history. Keeping current hardcoded values as Settings defaults preserves behavior; operator can override per-table via `.env`.

| Table | Current retention | Defended reader | Reader semantic | Risk if lower? |
|---|---|---|---|---|
| `volume_spikes` | 30d (`detected_at`) | `gainers/tracker.py:212` `MIN(detected_at)` for lead-time analysis | First-spike timestamp per coin_id | Lower retention silently shortens "first-spike" timestamp, garbling lead-time computation |
| `momentum_7d` | 30d (`detected_at`) | `spikes/detector.py:300` `'-7 days'` | 7d rolling count | 7d retention sufficient for spikes/detector; 30d is wider than needed but preserves historical analytics |
| `trending_snapshots` | 7d (`snapshot_at`) | `trading/engine.py:39` `MIN(snapshot_at)` | First-trending timestamp per coin_id | Lower retention truncates first-trending timestamp. 7d is already a tight window — defensive to keep |
| `learn_logs` | 90d (`created_at`) | `narrative/learner.py:375` `ORDER BY created_at DESC LIMIT 7` | Last 7 daily reflections | LIMIT-N read; 90d is far over-provisioned but defensive for replay/debug |
| `chain_matches` | 30d (`completed_at`) | `backtest.py:161` `WHERE completed_at > ?` | Time-bounded backtest queries | Lower retention truncates backtest history; 30d aligns with paper-trade horizon |
| `holder_snapshots` | 14d (`scanned_at`) | `db.py:4198` `ORDER BY scanned_at DESC LIMIT 1` | Latest holder count per contract | LIMIT-1 read; 14d is over-provisioned (only needs the latest row) but cheap |

**Cross-field validator decision:** unlike PR #136's `_validate_retention_covers_secondwave_window`, there is no single load-bearing downstream window for these 6 tables. Each table's "right" retention is independent. **No new model_validator in this PR** — per-table operator override via `.env` is the right knob shape. Document the reader-windows in code-adjacent comments so operators don't lower a retention below its reader-window unknowingly.

---

## File map

- **Create:** none (extending existing files)
- **Modify:**
  - `scout/config.py` — add 6 Settings fields next to `SCORE_HISTORY_RETENTION_DAYS` block
  - `scout/db.py` — add 6 prune methods next to `prune_score_history` / `prune_volume_snapshots` (~line 4815)
  - `scout/main.py` — extend `_run_hourly_maintenance` body with 6 new prune calls (after the score/volume block)
  - `scout/narrative/agent.py` — delete `_run_extra_table_prune` helper entirely; replace its call site at the narrative daily-learn loop with a single comment noting the migration
- **Test:**
  - `tests/test_config.py` — 6 default tests + 6 env-override tests (or batched parametrize)
  - `tests/test_db.py` — 12 prune method tests (2 per table: keeps_recent + empty_returns_zero)
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
    # Hourly prune via main._run_hourly_maintenance. Defaults preserve existing
    # in-tree behavior from scout/narrative/agent.py:86-93 (pre-cycle-2). Per-table
    # reader-window notes in tasks/plan_narrative_prune_scope_expansion.md. Lower
    # at your own risk — see reader-window table.
    VOLUME_SPIKES_RETENTION_DAYS: int = 30
    MOMENTUM_7D_RETENTION_DAYS: int = 30
    TRENDING_SNAPSHOTS_RETENTION_DAYS: int = 7
    LEARN_LOGS_RETENTION_DAYS: int = 90
    CHAIN_MATCHES_RETENTION_DAYS: int = 30
    HOLDER_SNAPSHOTS_RETENTION_DAYS: int = 14
```

- [ ] **Step 1.4: Run → PASS**
- [ ] **Step 1.5: Commit** — `feat(config): 6 narrative-owned-table retention fields`

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

**Indexes question:** the existing tables already have indexes on the relevant timestamp columns? Verify via `PRAGMA index_list` for each table OR via the `_create_tables` block (db.py ~lines 350-400 and beyond). If any of the 6 lacks a `scanned_at`/`detected_at`/etc. index, follow the same migration pattern from PR #136 (split per-table, `PRAGMA busy_timeout=90000`). **Engineer: confirm before merging.**

**§9a runtime check before deploy:** SSH srilu, `sqlite3 scout.db "SELECT name, MIN(detected_at), COUNT(*) FROM volume_spikes;"` etc. for all 6 tables to confirm pruning expectations. Skip if redundant with what cycle 1 already verified.

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
- 12 prune method tests (2 per table × 6 tables)
- 6 hourly-maintenance integration tests (one per new prune call)
- 1 narrative-agent-helper-deleted regression test
- Full regression must pass on srilu (Windows OPENSSL workaround per memory)

Total: ~31 new tests.

---

## Deployment verification (operator-gated)

1. `journalctl -u gecko-pipeline --since "10 minutes ago" | grep -E "_pruned|_prune_failed"` — should see info-when-rows>0, silent-when-zero for each table
2. Spot-check via `sqlite3 scout.db "SELECT COUNT(*) FROM <table> WHERE <col> < datetime('now', '-N days')"` for any table; expect 0
3. Revert path: per-table `.env` `<TABLE>_RETENTION_DAYS=365`; full rollback = revert PR

---

## Out of scope

- New indexes on the 6 tables — most have at least the existing schema-indexes; PR-stage `EXPLAIN QUERY PLAN` check determines if any need a new `idx_*_<col>` migration (defer to design phase or fold).
- Cross-field validator like PR #136's `_validate_retention_covers_secondwave_window` — no single load-bearing downstream window applies to all 6 tables. Per-table reader-window risk documented in code comments instead.
- Curl-direct alert on prune failure — already filed as `BL-NEW-SCORE-VOLUME-PRUNE-ALERT` from PR #136; this PR's prunes will benefit when that lands.
- VPS deployment — operator-gated per user "do not deploy until PR is reviewed by me" rule.
