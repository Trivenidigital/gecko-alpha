**New primitives introduced:** `SCORE_HISTORY_RETENTION_DAYS` Settings field, `VOLUME_SNAPSHOTS_RETENTION_DAYS` Settings field, `Database.prune_score_history(*, keep_days)` method, `Database.prune_volume_snapshots(*, keep_days)` method, structured log events `score_history_pruned` / `volume_snapshots_pruned` / `extra_prune_table_error`.

# Plan: Harden score_history + volume_snapshots pruning

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close BL-NEW-SCORE-HISTORY-PRUNING + BL-NEW-VOLUME-SNAPSHOTS-PRUNING per §7a residual-gap rule. Existing 14-day pruning at `scout/narrative/agent.py:680-699` works, but has 4 defects (hardcoded threshold, narrative-loop coupling, silent except, no telemetry). Harden the existing implementation rather than build new.

**Architecture:** Mirror the in-tree pattern used by `prune_old_candidates` / `prune_perp_anomalies` / `prune_cryptopanic_posts` (methods on `Database`, called hourly from `scout/main.py`, retention via Settings). Move score_history + volume_snapshots out of narrative's daily loop; leave the other 6 tables (which are not in scope of these two backlog items) in narrative but fix the systemic `except Exception: pass` so they fail loudly.

**Tech Stack:** aiosqlite, Pydantic Settings, structlog, pytest-asyncio.

---

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| SQLite table pruning / retention | None — Hermes skill hub (689 skills as of 2026-05-16) lists no internal-DB-maintenance skills. Generic data-retention is project-internal. | Build in-tree. |
| Structured logging / observability for prune passes | None — Hermes Observability/MLOps skills cover external telemetry surfaces, not async-aiosqlite call-site logging. | Build in-tree. |
| Pydantic Settings field conventions | N/A — project already uses Pydantic Settings (`scout/config.py`). | Extend existing. |

**awesome-hermes-agent ecosystem check:** repo returned 404 on 2026-05-16; previous probes confirmed no overlap with internal-DB or async-Python-pruning. **One-sentence verdict:** project-internal hardening; no Hermes capability replaces this path.

---

## Drift check (post-audit)

- Existing implementation at `scout/narrative/agent.py:680-699` — 14-day pruning for 8 tables including score_history + volume_snapshots, runs inside narrative daily-learn loop.
- Existing pattern for other prunes — `Database.prune_old_candidates`, `Database.prune_perp_anomalies`, `Database.prune_cryptopanic_posts` (methods on Database class, hourly call from `scout/main.py:1702-1751`).
- Audit reference: `tasks/findings_backlog_drift_audit_2026_05_16.md` — BL-NEW-SCORE-HISTORY-PRUNING and BL-NEW-VOLUME-SNAPSHOTS-PRUNING categorized as Drift-partial.
- Backlog status reconciled in PR #135 (`docs/backlog-reconcile-2026-05-16` branch).

**Residual gaps this plan closes:**
1. Hardcoded `14` retention — violates project CLAUDE.md "No hardcoded thresholds (must come from Settings / .env)"
2. Coupling to narrative daily-learn loop — disabling narrative silently disables pruning for these 2 tables
3. `except Exception: pass` at `agent.py:695-696` — Class 1 silent failure (CLAUDE.md §12a)
4. No row-count telemetry per pass

**Residual gaps this plan does NOT close (deferred follow-up):**
- The other 6 tables in `agent.py:681-690` (`volume_spikes`, `momentum_7d`, `trending_snapshots`, `learn_logs`, `chain_matches`, `holder_snapshots`) remain in the narrative daily loop with hardcoded thresholds. They benefit from the silent-except fix (Class 1 surface) but parameterize+decouple is deferred. File `BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION` for follow-up after this PR lands.

---

## File map

- **Create:** none (extending existing files)
- **Modify:**
  - `scout/config.py` — add 2 Settings fields
  - `scout/db.py` — add 2 prune methods next to existing `prune_perp_anomalies` / `prune_cryptopanic_posts` (lines ~4692-4764)
  - `scout/main.py` — add 2 prune calls inside hourly loop (~line 1736, after `prune_perp_anomalies`)
  - `scout/narrative/agent.py` — remove score_history + volume_snapshots from list at line 687-689; replace `except Exception: pass` at line 695-696 with structured log
- **Test:**
  - `tests/test_db.py` — add prune method tests (mirror existing `test_prune_perp_anomalies` / `test_prune_cryptopanic_posts` if present, else add fresh)
  - `tests/test_config.py` — add tests for the 2 new settings defaults + env override
  - `tests/test_main.py` OR `tests/test_pruning_integration.py` — add test that main.py hourly loop calls both new prune methods

---

## Tasks

### Task 1: Add Settings fields

**Files:**
- Modify: `scout/config.py` (next to `CRYPTOPANIC_RETENTION_DAYS` ~line 288 OR `PERP_ANOMALY_RETENTION_DAYS` ~line 595 — pick whichever placement the existing file convention suggests)

- [ ] **Step 1.1: Write the failing test**

```python
# tests/test_config.py
def test_score_history_retention_default():
    from scout.config import Settings
    s = Settings(_env_file=None)
    assert s.SCORE_HISTORY_RETENTION_DAYS == 14

def test_volume_snapshots_retention_default():
    from scout.config import Settings
    s = Settings(_env_file=None)
    assert s.VOLUME_SNAPSHOTS_RETENTION_DAYS == 14

def test_score_history_retention_env_override(monkeypatch):
    monkeypatch.setenv("SCORE_HISTORY_RETENTION_DAYS", "7")
    from scout.config import Settings
    s = Settings()
    assert s.SCORE_HISTORY_RETENTION_DAYS == 7
```

- [ ] **Step 1.2: Run to verify failure**

```
uv run pytest tests/test_config.py::test_score_history_retention_default tests/test_config.py::test_volume_snapshots_retention_default tests/test_config.py::test_score_history_retention_env_override -v
```

Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'SCORE_HISTORY_RETENTION_DAYS'`.

- [ ] **Step 1.3: Add the Settings fields**

In `scout/config.py`, in the appropriate section (likely after `CRYPTOPANIC_RETENTION_DAYS`):

```python
    SCORE_HISTORY_RETENTION_DAYS: int = 14
    VOLUME_SNAPSHOTS_RETENTION_DAYS: int = 14
```

- [ ] **Step 1.4: Run to verify pass**

```
uv run pytest tests/test_config.py -k "retention" -v
```

Expected: PASS (3 new tests).

- [ ] **Step 1.5: Commit**

```
git add scout/config.py tests/test_config.py
git commit -m "feat(config): SCORE_HISTORY_RETENTION_DAYS + VOLUME_SNAPSHOTS_RETENTION_DAYS"
```

---

### Task 2: Database.prune_score_history method

**Files:**
- Modify: `scout/db.py` (next to `prune_perp_anomalies` ~line 4692)
- Test: `tests/test_db.py`

- [ ] **Step 2.1: Write the failing test**

```python
# tests/test_db.py
import pytest
from datetime import datetime, timedelta, timezone

@pytest.mark.asyncio
async def test_prune_score_history_keeps_recent(db):
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=5)).isoformat()
    old = (now - timedelta(days=20)).isoformat()
    await db._conn.execute(
        "INSERT INTO score_history (contract_address, score, scanned_at) VALUES (?, ?, ?)",
        ("0xRECENT", 50.0, recent),
    )
    await db._conn.execute(
        "INSERT INTO score_history (contract_address, score, scanned_at) VALUES (?, ?, ?)",
        ("0xOLD", 50.0, old),
    )
    await db._conn.commit()

    deleted = await db.prune_score_history(keep_days=14)

    assert deleted == 1
    cur = await db._conn.execute("SELECT contract_address FROM score_history")
    rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["0xRECENT"]

@pytest.mark.asyncio
async def test_prune_score_history_empty_table_returns_zero(db):
    deleted = await db.prune_score_history(keep_days=14)
    assert deleted == 0

@pytest.mark.asyncio
async def test_prune_score_history_keep_days_zero_deletes_all(db):
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO score_history (contract_address, score, scanned_at) VALUES (?, ?, ?)",
        ("0xANY", 50.0, now),
    )
    await db._conn.commit()
    deleted = await db.prune_score_history(keep_days=0)
    assert deleted == 1
```

- [ ] **Step 2.2: Run to verify failure**

```
uv run pytest tests/test_db.py::test_prune_score_history_keeps_recent -v
```

Expected: FAIL with `AttributeError: 'Database' object has no attribute 'prune_score_history'`.

- [ ] **Step 2.3: Implement the method**

In `scout/db.py`, after `prune_perp_anomalies`:

```python
    async def prune_score_history(self, *, keep_days: int) -> int:
        """Delete score_history rows older than ``keep_days``. Returns rowcount.

        Uses <= so that scanned_at == cutoff prunes (matches prune_cryptopanic_posts
        semantics — keep_days=0 means "retain nothing as old as now").
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM score_history WHERE scanned_at <= ?",
            (cutoff,),
        )
        await self._conn.commit()
        return cur.rowcount or 0
```

- [ ] **Step 2.4: Run to verify pass**

```
uv run pytest tests/test_db.py -k "prune_score_history" -v
```

Expected: PASS (3 new tests).

- [ ] **Step 2.5: Commit**

```
git add scout/db.py tests/test_db.py
git commit -m "feat(db): prune_score_history method with TDD coverage"
```

---

### Task 3: Database.prune_volume_snapshots method

**Files:**
- Modify: `scout/db.py` (next to `prune_score_history`)
- Test: `tests/test_db.py`

- [ ] **Step 3.1: Write the failing test (mirror of Task 2)**

```python
@pytest.mark.asyncio
async def test_prune_volume_snapshots_keeps_recent(db):
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=5)).isoformat()
    old = (now - timedelta(days=20)).isoformat()
    await db._conn.execute(
        "INSERT INTO volume_snapshots (contract_address, volume_24h_usd, scanned_at) VALUES (?, ?, ?)",
        ("0xRECENT", 100000.0, recent),
    )
    await db._conn.execute(
        "INSERT INTO volume_snapshots (contract_address, volume_24h_usd, scanned_at) VALUES (?, ?, ?)",
        ("0xOLD", 100000.0, old),
    )
    await db._conn.commit()

    deleted = await db.prune_volume_snapshots(keep_days=14)

    assert deleted == 1
    cur = await db._conn.execute("SELECT contract_address FROM volume_snapshots")
    rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["0xRECENT"]

@pytest.mark.asyncio
async def test_prune_volume_snapshots_empty_table_returns_zero(db):
    deleted = await db.prune_volume_snapshots(keep_days=14)
    assert deleted == 0
```

- [ ] **Step 3.2: Run to verify failure**

```
uv run pytest tests/test_db.py::test_prune_volume_snapshots_keeps_recent -v
```

Expected: FAIL.

- [ ] **Step 3.3: Implement**

```python
    async def prune_volume_snapshots(self, *, keep_days: int) -> int:
        """Delete volume_snapshots rows older than ``keep_days``. Returns rowcount."""
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM volume_snapshots WHERE scanned_at <= ?",
            (cutoff,),
        )
        await self._conn.commit()
        return cur.rowcount or 0
```

- [ ] **Step 3.4: Run to verify pass**

```
uv run pytest tests/test_db.py -k "prune_volume_snapshots" -v
```

Expected: PASS.

- [ ] **Step 3.5: Commit**

```
git add scout/db.py tests/test_db.py
git commit -m "feat(db): prune_volume_snapshots method with TDD coverage"
```

---

### Task 4: Wire into main.py hourly loop

**Files:**
- Modify: `scout/main.py` (~line 1736, after `prune_perp_anomalies`)
- Test: `tests/test_main.py` OR new `tests/test_pruning_integration.py`

- [ ] **Step 4.1: Write the failing integration test**

```python
# tests/test_main.py (or new test_pruning_integration.py)
@pytest.mark.asyncio
async def test_main_hourly_loop_calls_score_history_prune(db_with_settings, monkeypatch):
    """Verify the hourly loop in main.py calls db.prune_score_history with the setting."""
    # Capture calls to prune_score_history
    calls = []
    original_prune = db_with_settings[0].prune_score_history
    async def wrapped(*, keep_days):
        calls.append(keep_days)
        return await original_prune(keep_days=keep_days)
    monkeypatch.setattr(db_with_settings[0], "prune_score_history", wrapped)

    # Trigger hourly task — extract the prune block to a function in main.py for testability,
    # OR call run_cycle with a mocked time to force the hourly branch.

    # Assert prune_score_history was called with settings.SCORE_HISTORY_RETENTION_DAYS
    assert calls == [db_with_settings[1].SCORE_HISTORY_RETENTION_DAYS]
```

NOTE for engineer: if the hourly block is not currently extractable as a function, refactor it into `_run_hourly_maintenance(db, settings, logger)` first as a tiny commit. This keeps the test reliable and the call site small.

- [ ] **Step 4.2: Run to verify failure**

```
uv run pytest tests/test_main.py -k "score_history_prune" -v
```

Expected: FAIL — prune call not wired.

- [ ] **Step 4.3: Wire the prune calls**

In `scout/main.py`, after `prune_perp_anomalies` (~line 1735), add:

```python
                        try:
                            pruned_sh = await db.prune_score_history(
                                keep_days=settings.SCORE_HISTORY_RETENTION_DAYS
                            )
                            if pruned_sh:
                                logger.info(
                                    "score_history_pruned",
                                    rows_deleted=pruned_sh,
                                    keep_days=settings.SCORE_HISTORY_RETENTION_DAYS,
                                )
                        except Exception:
                            logger.exception("score_history_prune_failed")

                        try:
                            pruned_vs = await db.prune_volume_snapshots(
                                keep_days=settings.VOLUME_SNAPSHOTS_RETENTION_DAYS
                            )
                            if pruned_vs:
                                logger.info(
                                    "volume_snapshots_pruned",
                                    rows_deleted=pruned_vs,
                                    keep_days=settings.VOLUME_SNAPSHOTS_RETENTION_DAYS,
                                )
                        except Exception:
                            logger.exception("volume_snapshots_prune_failed")
```

- [ ] **Step 4.4: Run to verify pass**

```
uv run pytest tests/test_main.py -k "prune" -v
```

Expected: PASS.

- [ ] **Step 4.5: Commit**

```
git add scout/main.py tests/test_main.py
git commit -m "feat(main): hourly prune of score_history + volume_snapshots via Settings"
```

---

### Task 5: Remove score_history + volume_snapshots from narrative loop + fix systemic silent-except

**Files:**
- Modify: `scout/narrative/agent.py:680-699`
- Test: extend existing narrative agent test OR add focused test for the silent-except fix

- [ ] **Step 5.1: Write the failing test**

```python
# tests/test_narrative_agent_prune.py (new) OR extend existing
import pytest
import structlog
from unittest.mock import AsyncMock, MagicMock

@pytest.mark.asyncio
async def test_narrative_extra_prune_logs_error_on_exception(capture_logs):
    """When narrative's extra-prune loop hits a bad table, it must log structured error, not silent pass."""
    db = MagicMock()
    db._conn = MagicMock()
    db._conn.execute = AsyncMock(side_effect=RuntimeError("simulated table missing"))
    db._conn.commit = AsyncMock()

    # Call the extracted helper that wraps the for-loop (refactor required in 5.3)
    from scout.narrative.agent import _run_extra_table_prune
    await _run_extra_table_prune(db)

    # Verify structured log emitted, not silent
    error_logs = [e for e in capture_logs if e.get("event") == "extra_prune_table_error"]
    assert len(error_logs) >= 1
    assert "table" in error_logs[0]
```

- [ ] **Step 5.2: Run to verify failure**

```
uv run pytest tests/test_narrative_agent_prune.py -v
```

Expected: FAIL — helper doesn't exist OR `except Exception: pass` swallows the error.

- [ ] **Step 5.3: Refactor narrative/agent.py and fix the systemic silent-except**

Extract the in-line loop at `agent.py:680-699` to a helper function `_run_extra_table_prune(db)`, remove the 2 score/volume entries from the list, and replace `except Exception: pass` with structured logging:

```python
async def _run_extra_table_prune(db) -> None:
    """Prune the 6 narrative-owned tables on the daily loop.

    NOTE: score_history + volume_snapshots are pruned hourly from scout.main
    via db.prune_score_history / db.prune_volume_snapshots — they are NOT in
    this list. The other 6 tables (volume_spikes, momentum_7d,
    trending_snapshots, learn_logs, chain_matches, holder_snapshots) are
    pending deferred parameterize+decouple — see backlog
    BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION.
    """
    for table, col, days in [
        ("volume_spikes", "detected_at", 30),
        ("momentum_7d", "detected_at", 30),
        ("trending_snapshots", "snapshot_at", 7),
        ("learn_logs", "created_at", 90),
        ("chain_matches", "completed_at", 30),
        ("holder_snapshots", "scanned_at", 14),
    ]:
        try:
            await db._conn.execute(
                f"DELETE FROM {table} WHERE datetime({col}) < datetime('now', '-{days} days')"
            )
        except Exception:
            logger.exception("extra_prune_table_error", table=table, days=days)
    try:
        await db._conn.commit()
    except Exception:
        logger.exception("extra_prune_commit_error")
```

Replace the existing `for table, col, days in [...]: try: ... except Exception: pass` block at `agent.py:680-699` with `await _run_extra_table_prune(db)`. Keep the outer `try: ... except Exception: logger.exception("extra_prune_error")` as defense-in-depth.

- [ ] **Step 5.4: Run to verify pass**

```
uv run pytest tests/test_narrative_agent_prune.py -v
```

Expected: PASS — structured log emitted.

- [ ] **Step 5.5: Run broader regression to verify no narrative behavior change**

```
uv run pytest tests/test_narrative*.py -v
```

Expected: existing narrative tests pass (no behavior change to narrative).

- [ ] **Step 5.6: Commit**

```
git add scout/narrative/agent.py tests/test_narrative_agent_prune.py
git commit -m "fix(narrative): structured logging for extra-prune errors + decouple score/volume"
```

---

### Task 6: File deferred follow-up + verify full regression

**Files:**
- Modify: `backlog.md` — add `BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION` entry
- Modify: `tasks/findings_backlog_drift_audit_2026_05_16.md` — note this PR closes the 2 drift-partial items

- [ ] **Step 6.1: File the deferred follow-up entry**

Append to `backlog.md` under the P2 section:

```markdown
### BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION: parameterize + decouple remaining 6 narrative-owned prunes
**Status:** PROPOSED 2026-05-16 — filed as residual from this PR's §7a partial-match reframe.
**Why:** The PR that closed BL-NEW-SCORE-HISTORY-PRUNING + BL-NEW-VOLUME-SNAPSHOTS-PRUNING extracted those 2 tables out of `scout/narrative/agent.py` and parameterized them via Settings. The remaining 6 tables in `_run_extra_table_prune` (`volume_spikes`, `momentum_7d`, `trending_snapshots`, `learn_logs`, `chain_matches`, `holder_snapshots`) still use hardcoded retention values and run only inside the narrative daily-learn loop. Same defect class.
**Action:** add 6 Settings fields + 6 prune methods + hourly wiring (mirror the score/volume pattern). Per-table retention can be tuned independently.
**decision-by:** 6 weeks (lower urgency than score/volume — these tables write at slower rates).
```

- [ ] **Step 6.2: Update audit doc**

Append to `tasks/findings_backlog_drift_audit_2026_05_16.md`:

```markdown
---
## 2026-05-16 closure note

BL-NEW-SCORE-HISTORY-PRUNING + BL-NEW-VOLUME-SNAPSHOTS-PRUNING closed by PR #<TBD>
(branch `feat/score-volume-pruning-harden`). Reframed scope per §7a residual-gap
rule. Filed BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION for the remaining 6 tables.
```

- [ ] **Step 6.3: Full regression**

```
uv run pytest --tb=short -q
```

Expected: full suite passes (or at minimum, no NEW failures vs main). Note any pre-existing baseline failures in PR description.

- [ ] **Step 6.4: Commit**

```
git add backlog.md tasks/findings_backlog_drift_audit_2026_05_16.md
git commit -m "docs(backlog): file BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION residual"
```

---

## Test plan summary

- 3 config tests (settings defaults + env override)
- 5 db tests (prune_score_history: keeps_recent, empty, keep_days_zero; prune_volume_snapshots: keeps_recent, empty)
- 1 narrative test (silent-except → structured-log replacement)
- 1 integration test (main.py hourly loop calls both prune methods)
- Full regression must pass (or baseline-only failures called out in PR description)

Total: 10 new tests + full regression.

---

## Deployment verification (after PR review + merge — operator-gated, NOT autonomous)

After deploy on srilu-vps:

1. Verify settings load: `journalctl -u gecko-pipeline | grep SCORE_HISTORY_RETENTION_DAYS` — confirm setting visible at startup
2. Wait 1-2 hours for first hourly maintenance loop firing
3. Verify structured log emits: `journalctl -u gecko-pipeline --since "1 hour ago" | grep -E "score_history_pruned|volume_snapshots_pruned"` — expect at least 1 of each per hour with `rows_deleted` field
4. Verify no silent failures: `journalctl -u gecko-pipeline | grep "_prune_failed"` — should be empty unless real failure
5. Optionally: `sqlite3 scout.db "SELECT COUNT(*) FROM score_history WHERE scanned_at < datetime('now', '-14 days')"` — should return 0

**Revert path if needed:**
- `.env` override: `SCORE_HISTORY_RETENTION_DAYS=365` + `VOLUME_SNAPSHOTS_RETENTION_DAYS=365` effectively disables pruning without code rollback
- Full rollback: revert the PR's main.py block; narrative loop's score/volume pruning will resume via the agent.py removal being reverted

---

## Out of scope (explicit non-goals)

- Per-token rolling retention ("keep last N=10 per contract_address") — original backlog Action mentioned this as an alternative, but time-based at 14d already mirrors the production behavior and is simpler. Per-token retention is filed as a separate consideration if/when 14d proves insufficient.
- The other 6 narrative-pruned tables — filed as BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION.
- §12a freshness SLO / watchdog for score_history + volume_snapshots — gated on §12a daemon being built (separate backlog item BL-NEW-SCORE-HISTORY-WATCHDOG-SLO + BL-NEW-VOLUME-SNAPSHOTS-WATCHDOG-SLO).
- VPS deployment — operator-gated per user "do not deploy until PR is reviewed by me" rule.
