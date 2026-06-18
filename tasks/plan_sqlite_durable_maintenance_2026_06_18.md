# P0 Part B — Durable SQLite Maintenance Implementation Plan

**New primitives introduced:** NONE (uses existing SQLite PRAGMAs, existing
`scout.alerter`, existing `Settings`/structlog patterns; adds one new
observability module + two DB methods, no new external dependency, no schema)

> **For agentic workers:** TDD task-by-task. Steps use `- [ ]` checkbox syntax.

**Goal:** Make `scout.db` self-maintaining inside the hourly loop so the
2026-06-18 incident (54.7% freelist bloat + a WAL pinned by orphaned readers)
cannot silently recur.

**Architecture:** Three remediations run from `_run_hourly_maintenance`, each
behind a `Settings` flag: (1) `wal_checkpoint(TRUNCATE)` with full tuple
logging + busy handling, (2) `incremental_vacuum` on a freelist threshold
(works online now that auto_vacuum=INCREMENTAL), (3) a `/proc`-based
stale-reader watchdog that alerts the operator — the actual incident root
cause, and the thing that makes a `busy` checkpoint actionable instead of
silently ineffective. Orchestration lives in a new light module
(`scout/observability/sqlite_maintenance.py`) so it is testable without
importing the aiohttp-heavy `scout.main`.

**Tech Stack:** Python asyncio, aiosqlite, structlog, Pydantic Settings,
pytest (asyncio auto mode), `/proc` (Linux) stdlib only.

## Hermes-first analysis (CLAUDE.md §7b)

| Domain | Hermes skill found? | Decision |
|---|---|---|
| SQLite WAL/freelist PRAGMA maintenance | none found (Hermes covers per-VPS SQLite *state*, not engine-internal maintenance) | build from scratch — DB-engine internals |
| `/proc` process/file-holder introspection | none found | build from scratch — OS-level, no Hermes capability |
| Operator alert (Telegram) | yes — Hermes multi-channel response | reuse in-tree `scout.alerter` (pipeline owns its own transport; routing through Hermes would be drift) |

awesome-hermes-agent ecosystem check: no DB-vacuum / file-holder-watchdog
skill exists. Verdict: `extends-Hermes` — pipeline-local infra below the
substrate, reusing the in-tree alerter for the notify step. Receipt:
`tasks/.hermes-check-receipts/p0-part-b-durable-sqlite-maintenance.json`.

## Drift-check (CLAUDE.md §7a)

`grep 'wal_checkpoint|incremental_vacuum|stale.?reader|holder|fuser|lsof|
auto_vacuum' scout/**/*.py` → only `probe_wal_state` (db.py:6570, read-only)
and token-`holder_count` (unrelated). `main.py:1446` only *logs* WAL state. No
remediation exists. Net-new confirmed; no redundant PR.

## Gate-1 review folds (2026-06-18, Codex/operator review) — SUPERSEDE the inline blocks below

These corrections are authoritative; where a Task code block disagrees, follow this section.

**Fold 1 (P0) — `incremental_vacuum` reclaims 1 page per `execute()`; drive it with `fetchall()`.**
Empirically verified on SQLite 3.50.4 (stdlib `sqlite3`, which aiosqlite wraps):
`PRAGMA incremental_vacuum`, `incremental_vacuum(0)`, and `incremental_vacuum(N)`
each reclaim only **1 page per `execute()`** — the pragma returns *one result row
per freed page* and the work is driven by stepping the result. `await cur.fetchall()`
drains all requested pages in a single call; `incremental_vacuum(N)` caps at N. So
`run_incremental_vacuum` does NOT loop — it executes the pragma (with-arg = capped,
no-arg = all) then `fetchall()`:
```python
        async with self._txn_lock:
            auto_vacuum = await _pragma_int("auto_vacuum")
            before = await _pragma_int("freelist_count")
            if auto_vacuum == 2 and before > 0:
                if max_pages > 0:
                    cur = await self._conn.execute(
                        f"PRAGMA incremental_vacuum({int(max_pages)})"
                    )
                else:
                    cur = await self._conn.execute("PRAGMA incremental_vacuum")
                await cur.fetchall()  # drives the pragma (1 result row per freed page)
                await self._conn.commit()
            after = await _pragma_int("freelist_count")
```
Tests (Task 3): (a) **drain-all** — freelist ≫1 (e.g. 45) → after==0, pages_reclaimed==before;
(b) **cap** — `max_pages=N` with N<freelist → after==before-N (reclaim exactly N).

**Fold 2 (P1) — stale-reader alert must not falsely log "delivered".**
`alerter.send_telegram_message` only raises on non-200 when `raise_on_failure=True`
(alerter.py:215). So `_alert_stale_readers` MUST pass `raise_on_failure=True`, and
emit `sqlite_stale_reader_alert_delivered` + add to `_ALERTED_PIDS` ONLY after the
call returns without raising; the `except` logs `_alert_failed` and leaves dedup
unchanged so it retries next run:
```python
    logger.info("sqlite_stale_reader_alert_dispatched", pids=[h.pid for h in new])
    try:
        await alerter.send_telegram_message(
            "\n".join(lines), session, settings, parse_mode=None,
            raise_on_failure=True, source="sqlite_stale_reader_watchdog")
    except Exception:
        logger.exception("sqlite_stale_reader_alert_failed")
        return
    for h in new:
        _ALERTED_PIDS.add(h.pid)
    logger.info("sqlite_stale_reader_alert_delivered", pids=[h.pid for h in new])
```
Test: assert `raise_on_failure=True` kwarg; add a test where `send` raises →
`_delivered` NOT emitted and pid NOT deduped (re-alerts next run).

**Fold 3 (P1) — allowlist expected services instead of blanket `.service`.**
Blanket `".service" in cgroup` would hide a rogue long-lived systemd/cron job.
Replace with an explicit allowlist (new Settings flag
`SQLITE_EXPECTED_SERVICE_UNITS: list[str] = ["gecko-pipeline.service",
"gecko-dashboard.service"]`). Watchdog API becomes:
```python
def is_expected_service(cgroup: str, expected_units: list[str]) -> bool:
    return any(u in cgroup for u in expected_units)
# DbHolder.is_service -> DbHolder.is_expected_service
# scan_db_holders(..., expected_units: list[str] | None = None)  # default []
# find_stale_readers excludes h.is_expected_service (and own_pid, and age<=cutoff)
```
Any holder NOT in the allowlist (session scope OR an unexpected `.service`) that is
older than `max_age_hours` is flagged — caught by the age gate so transient cron
python (seconds old) never alerts. Orchestrator passes
`settings.SQLITE_EXPECTED_SERVICE_UNITS` to `scan_db_holders`. Test: a holder under
`cron.service` aged 9h with empty/`gecko`-only allowlist → flagged stale.

**Fold 4 (P2) — config test injects the fixture, not import.**
`settings_factory` is a pytest fixture: `def test_...(settings_factory): s = settings_factory()`.
Same for all tests using it (orchestrator tests already take it as a param).

**Fold 5 (rec) — bounded numeric Settings via `Field`.**
```python
from pydantic import Field  # (add if not already imported in config.py)
    SQLITE_WAL_CHECKPOINT_THRESHOLD_BYTES: int = Field(default=100_000_000, ge=0)
    SQLITE_INCREMENTAL_VACUUM_FREELIST_THRESHOLD: int = Field(default=50_000, ge=0)
    SQLITE_INCREMENTAL_VACUUM_MAX_PAGES: int = Field(default=200_000, ge=0)
    SQLITE_STALE_READER_MAX_AGE_HOURS: float = Field(default=6.0, gt=0)
    SQLITE_EXPECTED_SERVICE_UNITS: list[str] = Field(
        default_factory=lambda: ["gecko-pipeline.service", "gecko-dashboard.service"])
```
Add a test asserting `ge`/`gt` rejects a negative/zero value (ValidationError).

**Fold 6 (rec) — real busy-checkpoint test (Task 2).**
A second `aiosqlite` connection holding an open read transaction pins the WAL so
`checkpoint_wal_truncate()` returns `busy==1`:
```python
async def test_checkpoint_busy_with_concurrent_reader(tmp_path):
    import aiosqlite
    db = await _wal_db(tmp_path)
    await db._conn.execute("CREATE TABLE t(x)")
    await db._conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(300)])
    await db._conn.commit()
    reader = await aiosqlite.connect(str(tmp_path / "t.db"))
    await reader.execute("BEGIN")
    await (await reader.execute("SELECT COUNT(*) FROM t")).fetchall()  # pins snapshot
    await db._conn.execute("INSERT INTO t VALUES (999)")
    await db._conn.commit()
    res = await db.checkpoint_wal_truncate()
    assert res["busy"] == 1
    await reader.rollback(); await reader.close(); await db.close()
```

## Global Constraints

- No hardcoded thresholds — every threshold from `Settings` / `.env`.
- A `busy != 0` checkpoint is NOT success — log at WARNING, never INFO-success.
- Telegram alerts: `parse_mode=None`; emit `*_alert_dispatched` +
  `*_alert_delivered` structured logs around the call (CLAUDE.md §12b).
- Each remediation wrapped in its own try/except — one failure must not crash
  the cycle, and instrumentation failure must not be mis-attributed as a DB
  failure (per [[feedback_resilience_layered_failure_modes]]).
- Async everywhere; DB writes go through `db._txn_lock`.
- Watchdog must run on Linux prod and degrade to a no-op (no crash) elsewhere.
- `black` formatted (PostToolUse hook enforces `--check`).

## File Structure

- `scout/config.py` — +8 Settings flags (after line 414).
- `scout/db.py` — +2 methods: `checkpoint_wal_truncate`, `run_incremental_vacuum`.
- `scout/observability/sqlite_holder_watchdog.py` (new) — `/proc` scan + classify.
- `scout/observability/sqlite_maintenance.py` (new) — orchestrator `run_sqlite_maintenance(db, session, settings, logger)`.
- `scout/main.py` — one call into the orchestrator after the WAL-probe block (~1457).
- `tests/test_sqlite_maintenance_config.py` (new) — flag defaults.
- `tests/test_sqlite_maintenance_db.py` (new) — db method tests (real aiosqlite).
- `tests/test_sqlite_holder_watchdog.py` (new) — watchdog tests (fake /proc).
- `tests/test_sqlite_maintenance_orchestrator.py` (new) — orchestration + alert tests.

---

### Task 1: Config flags

**Files:** Modify `scout/config.py` (after line 414); Test `tests/test_sqlite_maintenance_config.py`

**Interfaces — Produces:** `Settings.SQLITE_WAL_CHECKPOINT_ENABLED: bool`,
`SQLITE_WAL_CHECKPOINT_THRESHOLD_BYTES: int`,
`SQLITE_INCREMENTAL_VACUUM_ENABLED: bool`,
`SQLITE_INCREMENTAL_VACUUM_FREELIST_THRESHOLD: int`,
`SQLITE_INCREMENTAL_VACUUM_MAX_PAGES: int`,
`SQLITE_STALE_READER_WATCHDOG_ENABLED: bool`,
`SQLITE_STALE_READER_MAX_AGE_HOURS: float`,
`SQLITE_STALE_READER_ALERT_ENABLED: bool`

- [ ] **Step 1: Write failing test**
```python
# tests/test_sqlite_maintenance_config.py
from tests.conftest import settings_factory  # use project fixture pattern

def test_sqlite_maintenance_flag_defaults():
    s = settings_factory()
    assert s.SQLITE_WAL_CHECKPOINT_ENABLED is True
    assert s.SQLITE_WAL_CHECKPOINT_THRESHOLD_BYTES == 100_000_000
    assert s.SQLITE_INCREMENTAL_VACUUM_ENABLED is True
    assert s.SQLITE_INCREMENTAL_VACUUM_FREELIST_THRESHOLD == 50_000
    assert s.SQLITE_INCREMENTAL_VACUUM_MAX_PAGES == 200_000
    assert s.SQLITE_STALE_READER_WATCHDOG_ENABLED is True
    assert s.SQLITE_STALE_READER_MAX_AGE_HOURS == 6.0
    assert s.SQLITE_STALE_READER_ALERT_ENABLED is True
```

- [ ] **Step 2: Run → FAIL** (`uv run pytest tests/test_sqlite_maintenance_config.py -v`) — AttributeError.

- [ ] **Step 3: Implement** (add after config.py:414)
```python
    # BL-NEW-SQLITE-DURABLE-MAINTENANCE (P0 Part B): active WAL/freelist
    # remediation + stale-reader watchdog in _run_hourly_maintenance.
    # Incident 2026-06-18: auto_vacuum=NONE (freelist 54.7%) + 2 orphaned
    # 65-day reader processes pinning the WAL. auto_vacuum flipped to
    # INCREMENTAL during the one-time VACUUM, so incremental_vacuum reclaims
    # freelist online. See tasks/plan_sqlite_durable_maintenance_2026_06_18.md.
    SQLITE_WAL_CHECKPOINT_ENABLED: bool = True
    SQLITE_WAL_CHECKPOINT_THRESHOLD_BYTES: int = 100_000_000
    SQLITE_INCREMENTAL_VACUUM_ENABLED: bool = True
    SQLITE_INCREMENTAL_VACUUM_FREELIST_THRESHOLD: int = 50_000  # pages (~200MB @4K)
    SQLITE_INCREMENTAL_VACUUM_MAX_PAGES: int = 200_000  # cap/run (0 = all)
    SQLITE_STALE_READER_WATCHDOG_ENABLED: bool = True
    SQLITE_STALE_READER_MAX_AGE_HOURS: float = 6.0
    SQLITE_STALE_READER_ALERT_ENABLED: bool = True
```

- [ ] **Step 4: Run → PASS**.
- [ ] **Step 5: Commit** `feat(sqlite): config flags for durable maintenance`.

---

### Task 2: `db.checkpoint_wal_truncate()`

**Files:** Modify `scout/db.py` (near `probe_wal_state` ~6570); Test `tests/test_sqlite_maintenance_db.py`

**Interfaces — Produces:** `async Database.checkpoint_wal_truncate() -> dict`
with keys `busy:int`, `log_frames:int`, `checkpointed_frames:int`.

- [ ] **Step 1: Write failing test**
```python
# tests/test_sqlite_maintenance_db.py
from scout.db import Database

async def _wal_db(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.initialize()
    return db

async def test_checkpoint_wal_truncate_returns_tuple(tmp_path):
    db = await _wal_db(tmp_path)
    await db._conn.execute("CREATE TABLE t(x)")
    await db._conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(500)])
    await db._conn.commit()
    res = await db.checkpoint_wal_truncate()
    assert set(res) == {"busy", "log_frames", "checkpointed_frames"}
    assert res["busy"] == 0           # sole connection → not busy
    assert res["checkpointed_frames"] >= 0
    await db.close()
```

- [ ] **Step 2: Run → FAIL** (no method).

- [ ] **Step 3: Implement**
```python
    async def checkpoint_wal_truncate(self) -> dict:
        """Run PRAGMA wal_checkpoint(TRUNCATE); return the result tuple.

        Returns {busy, log_frames, checkpointed_frames}. busy != 0 means the
        WAL could NOT be fully checkpointed/truncated (a reader is pinning
        frames) — callers MUST treat busy != 0 as not-success.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        async with self._txn_lock:
            cur = await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            row = await cur.fetchone()
        if not row:
            return {"busy": 1, "log_frames": 0, "checkpointed_frames": 0}
        return {
            "busy": int(row[0]),
            "log_frames": int(row[1]),
            "checkpointed_frames": int(row[2]),
        }
```

- [ ] **Step 4: Run → PASS**.
- [ ] **Step 5: Commit** `feat(sqlite): checkpoint_wal_truncate with tuple result`.

---

### Task 3: `db.run_incremental_vacuum()`

**Files:** Modify `scout/db.py`; Test add to `tests/test_sqlite_maintenance_db.py`

**Interfaces — Produces:** `async Database.run_incremental_vacuum(max_pages: int = 0) -> dict`
with keys `auto_vacuum:int`, `freelist_before:int`, `freelist_after:int`, `pages_reclaimed:int`.

- [ ] **Step 1: Write failing test**
```python
async def test_incremental_vacuum_reclaims_freelist(tmp_path):
    db = Database(str(tmp_path / "iv.db"))
    await db.initialize()
    await db._conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    await db._conn.execute("VACUUM")  # apply the mode on the empty db
    await db._conn.execute("CREATE TABLE big(x)")
    await db._conn.executemany("INSERT INTO big VALUES (?)", [(i,) for i in range(5000)])
    await db._conn.commit()
    await db._conn.execute("DELETE FROM big")
    await db._conn.commit()
    before = (await (await db._conn.execute("PRAGMA freelist_count")).fetchone())[0]
    assert before > 0
    res = await db.run_incremental_vacuum(max_pages=0)
    assert res["auto_vacuum"] == 2
    assert res["pages_reclaimed"] == before
    assert res["freelist_after"] == 0
    await db.close()

async def test_incremental_vacuum_noop_when_auto_vacuum_none(tmp_path):
    db = await _wal_db(tmp_path)  # default auto_vacuum=0
    res = await db.run_incremental_vacuum()
    assert res["auto_vacuum"] == 0
    assert res["pages_reclaimed"] == 0
    await db.close()
```

- [ ] **Step 2: Run → FAIL**.

- [ ] **Step 3: Implement**
```python
    async def run_incremental_vacuum(self, max_pages: int = 0) -> dict:
        """Reclaim freelist pages via PRAGMA incremental_vacuum.

        Requires auto_vacuum=INCREMENTAL (else a no-op: 0 reclaimed).
        max_pages=0 reclaims all freelist pages; >0 bounds work per run.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized")

        async def _pragma_int(name: str) -> int:
            cur = await self._conn.execute(f"PRAGMA {name}")
            row = await cur.fetchone()
            return int(row[0]) if row else 0

        async with self._txn_lock:
            auto_vacuum = await _pragma_int("auto_vacuum")
            before = await _pragma_int("freelist_count")
            if auto_vacuum == 2 and before > 0:
                if max_pages > 0:
                    await self._conn.execute(
                        f"PRAGMA incremental_vacuum({int(max_pages)})"
                    )
                else:
                    await self._conn.execute("PRAGMA incremental_vacuum")
                await self._conn.commit()
            after = await _pragma_int("freelist_count")
        return {
            "auto_vacuum": auto_vacuum,
            "freelist_before": before,
            "freelist_after": after,
            "pages_reclaimed": before - after,
        }
```

- [ ] **Step 4: Run → PASS**.
- [ ] **Step 5: Commit** `feat(sqlite): run_incremental_vacuum (online freelist reclaim)`.

---

### Task 4: Stale-reader watchdog module

**Files:** Create `scout/observability/sqlite_holder_watchdog.py`; Test `tests/test_sqlite_holder_watchdog.py`

**Interfaces — Produces:**
- `@dataclass DbHolder(pid:int, cmdline:str, cgroup:str, age_seconds:float, is_service:bool)`
- `scan_db_holders(db_paths:list[str], *, proc_root="/proc", own_pid=None, now=None, clk_tck=None) -> list[DbHolder]`
- `find_stale_readers(holders, *, max_age_hours:float, own_pid:int) -> list[DbHolder]`
- `classify_is_service(cgroup:str) -> bool` (`".service" in cgroup`)

- [ ] **Step 1: Write failing tests** (fake `/proc` under tmp_path: `<proc>/stat`
  with `btime`, per-pid `<proc>/<pid>/{fd/<n> symlink, cmdline, cgroup, stat}`):
```python
# tests/test_sqlite_holder_watchdog.py
import os, time
from scout.observability.sqlite_holder_watchdog import (
    scan_db_holders, find_stale_readers, classify_is_service, DbHolder,
)

def _mk_proc(tmp_path, btime, pids):
    proc = tmp_path / "proc"; proc.mkdir()
    (proc / "stat").write_text(f"cpu  0 0\nbtime {int(btime)}\n")
    for pid, (target, cmd, cgroup, starttime_ticks) in pids.items():
        d = proc / str(pid); (d / "fd").mkdir(parents=True)
        os.symlink(target, d / "fd" / "6")
        (d / "cmdline").write_bytes(cmd.encode() + b"\x00")
        (d / "cgroup").write_text(cgroup)
        fields = ["3"] * 19 + [str(int(starttime_ticks))]  # field22=starttime
        (d / "stat").write_text(f"{pid} (python3) S " + " ".join(fields) + "\n")
    return str(proc)

def test_scan_detects_db_holder(tmp_path):
    db = str(tmp_path / "scout.db"); open(db, "w").close()
    btime = time.time() - 100000
    proc = _mk_proc(tmp_path, btime, {
        4242: (db, "python3 _report.py", "0::/user.slice/session-7.scope", 50000)})
    holders = scan_db_holders([db], proc_root=proc, own_pid=1, now=time.time(), clk_tck=100)
    assert len(holders) == 1 and holders[0].pid == 4242
    assert holders[0].is_service is False and holders[0].age_seconds > 90000

def test_classify_service_vs_session():
    assert classify_is_service("0::/system.slice/gecko-pipeline.service") is True
    assert classify_is_service("0::/user.slice/user-0.slice/session-7.scope") is False

def test_find_stale_readers_filters_service_own_young():
    H = lambda pid, age, svc: DbHolder(pid, "x", "c", age, svc)
    stale = find_stale_readers(
        [H(99, 7*3600, False), H(98, 9*3600, True), H(1, 9*3600, False), H(97, 3600, False)],
        max_age_hours=6.0, own_pid=1)
    assert [h.pid for h in stale] == [99]
```

- [ ] **Step 2: Run → FAIL** (module missing).

- [ ] **Step 3: Implement** (full module — see below; pure stdlib, proc_root-injectable):
```python
"""Stale-reader watchdog: find non-service processes holding scout.db open.

Root cause of the 2026-06-18 WAL-bloat incident was 2 orphaned interactive
reader processes pinning the WAL for 65+ days. A pinned WAL makes
wal_checkpoint(TRUNCATE) return busy and silently ineffective — so this
watchdog makes that busy actionable. Linux /proc only; degrades to [] on
non-Linux or unreadable /proc.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass


@dataclass
class DbHolder:
    pid: int
    cmdline: str
    cgroup: str
    age_seconds: float
    is_service: bool


def classify_is_service(cgroup: str) -> bool:
    return ".service" in cgroup


def _read_text(path: str) -> str:
    try:
        with open(path, "r") as fh:
            return fh.read()
    except OSError:
        return ""


def _read_btime(proc_root: str) -> float:
    for line in _read_text(os.path.join(proc_root, "stat")).splitlines():
        if line.startswith("btime "):
            try:
                return float(line.split()[1])
            except (IndexError, ValueError):
                return 0.0
    return 0.0


def _start_epoch(proc_root: str, pid: int, btime: float, clk_tck: int) -> float:
    raw = _read_text(os.path.join(proc_root, str(pid), "stat"))
    if not raw or ")" not in raw:
        return 0.0
    after = raw.rsplit(")", 1)[1].split()  # fields 3.. ; starttime=field22=idx19
    if len(after) <= 19:
        return 0.0
    try:
        return btime + (int(after[19]) / clk_tck)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _cmdline(proc_root: str, pid: int) -> str:
    return _read_text(os.path.join(proc_root, str(pid), "cmdline")).replace("\x00", " ").strip()


def _holds_any(proc_root: str, pid: int, targets: set[str]) -> bool:
    fd_dir = os.path.join(proc_root, str(pid), "fd")
    try:
        names = os.listdir(fd_dir)
    except OSError:
        return False
    for n in names:
        try:
            tgt = os.readlink(os.path.join(fd_dir, n))
        except OSError:
            continue
        if tgt in targets or os.path.realpath(tgt) in targets:
            return True
    return False


def scan_db_holders(db_paths, *, proc_root="/proc", own_pid=None, now=None, clk_tck=None):
    now = time.time() if now is None else now
    if clk_tck is None:
        try:
            clk_tck = int(os.sysconf("SC_CLK_TCK")) or 100
        except (ValueError, OSError, AttributeError):
            clk_tck = 100
    own_pid = os.getpid() if own_pid is None else own_pid

    targets: set[str] = set()
    for p in db_paths:
        rp = os.path.realpath(p)
        for suffix in ("", "-wal", "-shm"):
            targets.add(p + suffix)
            targets.add(rp + suffix)

    try:
        entries = os.listdir(proc_root)
    except OSError:
        return []  # non-Linux / no /proc → no-op

    btime = _read_btime(proc_root)
    holders = []
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == own_pid or not _holds_any(proc_root, pid, targets):
            continue
        start = _start_epoch(proc_root, pid, btime, clk_tck)
        age = max(0.0, now - start) if start else 0.0
        cgroup = _read_text(os.path.join(proc_root, str(pid), "cgroup")).strip()
        holders.append(DbHolder(pid, _cmdline(proc_root, pid), cgroup, age,
                                classify_is_service(cgroup)))
    return holders


def find_stale_readers(holders, *, max_age_hours, own_pid):
    cutoff = max_age_hours * 3600.0
    return [h for h in holders
            if (not h.is_service) and h.pid != own_pid and h.age_seconds > cutoff]
```

- [ ] **Step 4: Run → PASS**.
- [ ] **Step 5: Commit** `feat(sqlite): /proc stale-reader watchdog module`.

---

### Task 5: Orchestrator + §12b alert

**Files:** Create `scout/observability/sqlite_maintenance.py`; Test `tests/test_sqlite_maintenance_orchestrator.py`

**Interfaces — Consumes:** `db.probe_wal_state()`, `db.checkpoint_wal_truncate()`,
`db.run_incremental_vacuum()`, watchdog `scan_db_holders`/`find_stale_readers`,
lazy `scout.alerter.send_telegram_message`.
**Produces:** `async run_sqlite_maintenance(db, session, settings, logger) -> None`;
`_reset_alert_dedup_for_tests()`.

Structured events (acceptance): `sqlite_incremental_vacuum_attempted`,
`sqlite_incremental_vacuum_completed`, `sqlite_wal_checkpoint_attempted`,
`sqlite_wal_checkpoint_succeeded`, `sqlite_wal_checkpoint_busy` (WARNING),
`sqlite_stale_reader_scan`, `sqlite_stale_reader_detected` (WARNING),
`sqlite_stale_reader_alert_dispatched`, `sqlite_stale_reader_alert_delivered`.

- [ ] **Step 1: Write failing tests** (inject a fake `db` with async stubs;
  capture structlog via a `cap_logs` fixture; monkeypatch watchdog + alerter):
```python
# tests/test_sqlite_maintenance_orchestrator.py
import structlog
from unittest.mock import AsyncMock
import scout.observability.sqlite_maintenance as m

class FakeDB:
    def __init__(self, **kw): self._r = kw
    async def probe_wal_state(self): return self._r["probe"]
    async def checkpoint_wal_truncate(self): return self._r["ckpt"]
    async def run_incremental_vacuum(self, max_pages=0): return self._r["iv"]

async def test_busy_checkpoint_logs_warning_not_success(cap_logs, settings_factory):
    db = FakeDB(probe={"wal_size_bytes": 999_000_000, "freelist_count": 0},
                ckpt={"busy": 1, "log_frames": 10, "checkpointed_frames": 0},
                iv={"auto_vacuum": 2, "pages_reclaimed": 0, "freelist_before":0, "freelist_after":0})
    s = settings_factory(SQLITE_STALE_READER_WATCHDOG_ENABLED=False,
                         SQLITE_INCREMENTAL_VACUUM_ENABLED=False)
    await m.run_sqlite_maintenance(db, object(), s, structlog.get_logger())
    events = [e["event"] for e in cap_logs]
    assert "sqlite_wal_checkpoint_busy" in events
    assert "sqlite_wal_checkpoint_succeeded" not in events

async def test_stale_reader_alert_dispatched_and_delivered(monkeypatch, cap_logs, settings_factory):
    from scout.observability.sqlite_holder_watchdog import DbHolder
    monkeypatch.setattr(m, "scan_db_holders", lambda *a, **k: [
        DbHolder(999, "python3 _report.py", "session-7.scope", 7*3600, False)])
    fake_send = AsyncMock()
    monkeypatch.setattr("scout.alerter.send_telegram_message", fake_send)
    db = FakeDB(probe={"wal_size_bytes": 0, "freelist_count": 0},
                ckpt={"busy":0,"log_frames":0,"checkpointed_frames":0},
                iv={"auto_vacuum":2,"pages_reclaimed":0,"freelist_before":0,"freelist_after":0})
    s = settings_factory(SQLITE_WAL_CHECKPOINT_ENABLED=False,
                         SQLITE_INCREMENTAL_VACUUM_ENABLED=False)
    m._reset_alert_dedup_for_tests()
    await m.run_sqlite_maintenance(db, object(), s, structlog.get_logger())
    events = [e["event"] for e in cap_logs]
    assert "sqlite_stale_reader_detected" in events
    assert "sqlite_stale_reader_alert_dispatched" in events
    assert "sqlite_stale_reader_alert_delivered" in events
    fake_send.assert_awaited_once()
    assert fake_send.await_args.kwargs.get("parse_mode") is None
```
(Add a `cap_logs` fixture in conftest using structlog capture if absent. Mirror
the structlog-capture pattern used in `tests/test_tg_dispatch_counter.py`.)

- [ ] **Step 2: Run → FAIL**.

- [ ] **Step 3: Implement** `scout/observability/sqlite_maintenance.py`
```python
"""Durable SQLite maintenance orchestration (P0 Part B). Imported by
scout.main; kept free of aiohttp imports so it is unit-testable in isolation
(alerter imported lazily only on the alert path)."""
from __future__ import annotations

import os

from scout.observability.sqlite_holder_watchdog import (
    find_stale_readers,
    scan_db_holders,
)

_ALERTED_PIDS: set[int] = set()  # in-memory dedup; resets on restart (ok)


def _reset_alert_dedup_for_tests() -> None:
    _ALERTED_PIDS.clear()


async def run_sqlite_maintenance(db, session, settings, logger) -> None:
    try:
        state = await db.probe_wal_state()
    except Exception:
        logger.exception("sqlite_maintenance_probe_failed")
        return
    freelist = int(state.get("freelist_count", 0))
    wal_bytes = int(state.get("wal_size_bytes", 0))

    ran_iv = False
    if (settings.SQLITE_INCREMENTAL_VACUUM_ENABLED
            and freelist > settings.SQLITE_INCREMENTAL_VACUUM_FREELIST_THRESHOLD):
        try:
            logger.info("sqlite_incremental_vacuum_attempted", freelist=freelist)
            res = await db.run_incremental_vacuum(
                max_pages=settings.SQLITE_INCREMENTAL_VACUUM_MAX_PAGES)
            ran_iv = res.get("pages_reclaimed", 0) > 0
            logger.info("sqlite_incremental_vacuum_completed", **res)
        except Exception:
            logger.exception("sqlite_incremental_vacuum_failed")

    if settings.SQLITE_WAL_CHECKPOINT_ENABLED and (
            wal_bytes > settings.SQLITE_WAL_CHECKPOINT_THRESHOLD_BYTES or ran_iv):
        try:
            logger.info("sqlite_wal_checkpoint_attempted", wal_bytes=wal_bytes)
            ck = await db.checkpoint_wal_truncate()
            if ck.get("busy", 1) == 0:
                logger.info("sqlite_wal_checkpoint_succeeded", **ck)
            else:
                logger.warning("sqlite_wal_checkpoint_busy", **ck)
        except Exception:
            logger.exception("sqlite_wal_checkpoint_failed")

    if settings.SQLITE_STALE_READER_WATCHDOG_ENABLED:
        try:
            own = os.getpid()
            holders = scan_db_holders([str(settings.DB_PATH)], own_pid=own)
            logger.info("sqlite_stale_reader_scan", holders=len(holders))
            stale = find_stale_readers(
                holders, max_age_hours=settings.SQLITE_STALE_READER_MAX_AGE_HOURS,
                own_pid=own)
            for h in stale:
                logger.warning("sqlite_stale_reader_detected", pid=h.pid,
                               cmdline=h.cmdline[:200],
                               age_hours=round(h.age_seconds / 3600, 1),
                               cgroup=h.cgroup[:120])
            if stale and settings.SQLITE_STALE_READER_ALERT_ENABLED:
                await _alert_stale_readers(stale, session, settings, logger)
        except Exception:
            logger.exception("sqlite_stale_reader_watchdog_failed")


async def _alert_stale_readers(stale, session, settings, logger) -> None:
    new = [h for h in stale if h.pid not in _ALERTED_PIDS]
    if not new:
        return
    from scout import alerter  # lazy: keep aiohttp out of import path
    lines = ["WARNING stale scout.db reader(s) pinning the WAL:"]
    for h in new:
        lines.append(f"pid {h.pid} age {round(h.age_seconds/3600,1)}h :: {h.cmdline[:120]}")
    lines.append("Kill the orphan(s) so wal_checkpoint can truncate.")
    logger.info("sqlite_stale_reader_alert_dispatched", pids=[h.pid for h in new])
    try:
        await alerter.send_telegram_message(
            "\n".join(lines), session, settings, parse_mode=None,
            source="sqlite_stale_reader_watchdog")
        for h in new:
            _ALERTED_PIDS.add(h.pid)
        logger.info("sqlite_stale_reader_alert_delivered", pids=[h.pid for h in new])
    except Exception:
        logger.exception("sqlite_stale_reader_alert_failed")
```

- [ ] **Step 4: Run → PASS**.
- [ ] **Step 5: Commit** `feat(sqlite): maintenance orchestrator + stale-reader alert (§12b)`.

---

### Task 6: Wire into the hourly loop

**Files:** Modify `scout/main.py` (after the WAL-probe block ~1457)

- [ ] **Step 1: Add import** near the other `scout.observability` imports:
```python
from scout.observability.sqlite_maintenance import run_sqlite_maintenance
```

- [ ] **Step 2: Call it** immediately after the existing
  `if settings.SQLITE_WAL_PROFILE_ENABLED:` probe block (after line ~1457):
```python
    # BL-NEW-SQLITE-DURABLE-MAINTENANCE (P0 Part B): active remediation.
    # The probe block above stays (observability); this performs the fixes.
    try:
        await run_sqlite_maintenance(db, session, settings, logger)
    except Exception:
        logger.exception("sqlite_maintenance_failed")
```

- [ ] **Step 3: Verify** `uv run python -m py_compile scout/main.py`.
- [ ] **Step 4: Commit** `feat(sqlite): wire durable maintenance into hourly loop`.

---

## Test / verification commands (for the final report)

```bash
uv run pytest tests/test_sqlite_maintenance_config.py tests/test_sqlite_maintenance_db.py \
  tests/test_sqlite_holder_watchdog.py tests/test_sqlite_maintenance_orchestrator.py -v
uv run black --check scout/observability/sqlite_maintenance.py \
  scout/observability/sqlite_holder_watchdog.py scout/db.py scout/config.py scout/main.py
uv run pytest --tb=short -q   # full suite (CI / Linux; Windows venv may hit OPENSSL_Uplink)
```

## Self-review (writing-plans)

- **Spec coverage:** checkpoint+tuple+busy (T2,T5), incremental_vacuum+threshold
  (T3,T5), stale-reader watchdog identify pid/cmd/age (T4,T5), no hardcoded
  thresholds (T1), §12b alert dispatched/delivered + parse_mode=None (T5),
  distinct structured events (T5), wiring (T6). All acceptance criteria mapped.
- **Placeholders:** none — full code in every step.
- **Type consistency:** dict keys (`busy/log_frames/checkpointed_frames`,
  `auto_vacuum/freelist_before/after/pages_reclaimed`) consistent across T2/T3/T5;
  `DbHolder` fields consistent across T4/T5.

## Risks / notes

- **Windows test runner:** full-suite + main-importing tests may hit
  OPENSSL_Uplink ([[feedback_windows_venv_openssl_state]]); the 4 new test files
  avoid importing `scout.main`. Trust CI/Linux for the full suite.
- **incremental_vacuum lock:** runs on the pipeline's own connection via
  `_txn_lock`; online, dashboard reads unaffected.
- **Watchdog false positives:** `.service`-cgroup heuristic + age gate excludes
  pipeline, dashboard, and transient cron python; only long-lived non-service
  holders (the orphan pattern) alert.
- **Alert dedup in-memory** → re-alerts after restart (acceptable).
- **No prod mutation in this PR.** Flags default-on; effect only when deployed
  code runs. `scout.db.pre-vacuum` deletion stays a separate approved step.

## Review section (fill after implementation)

- _Diff summary:_
- _Test results:_
- _Residual risks:_
