**New primitives introduced:** `Database.probe_wal_state()` method returning `dict` with `wal_size_bytes`, `wal_pages`, `shm_size_bytes` (V20#3 fold), `db_size_bytes`, `freelist_count`, `journal_mode`, `wal_autocheckpoint`. Structured log events `sqlite_wal_probe` (debug, hourly — V21 fold log-level parity with cycle 3) and `sqlite_wal_bloat_observed` (warning, threshold breach). New `SQLITE_WAL_PROFILE_ENABLED: bool = True` Settings flag + `SQLITE_WAL_BLOAT_BYTES: int = 50_000_000` threshold setting. Hourly hook in `_run_hourly_maintenance`. Operator helper `scripts/wal_summary.sh` with longest-consecutive-run aggregator (V20 SHOULD-FIX fold) + `scripts/wal_archive.sh` weekly cron. Filed follow-up `BL-NEW-SQLITE-WAL-TUNING-DECISION` with pre-registered criteria. Week-1 baseline-calibration documented (V21#2 fold) — operator reads steady-state from probes then tunes `SQLITE_WAL_BLOAT_BYTES` to ~1.5×p95.

# Plan: BL-NEW-SQLITE-WAL-PROFILE — instrument SQLite WAL bloat

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans to implement task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Measure SQLite WAL file size + freelist bloat at gecko-alpha's ~17k writes/hr/table combined load (score_history + volume_snapshots + candidates upsert + cycle 2's 6 narrative-table prunes). Pre-registered decision: TUNE `wal_autocheckpoint` (or add explicit `PRAGMA wal_checkpoint(TRUNCATE)` cadence) if bloat exceeds threshold; ACCEPT otherwise.

**Architecture:** Lightweight `Database.probe_wal_state()` method called once per hour from `_run_hourly_maintenance` (next to the existing prune calls). Emits structured log with current WAL/DB sizes. Operator summary script analyzes journalctl + archive over the 4-week soak. Pattern mirrors cycle 3's TG-burst-profile exactly.

**Tech Stack:** aiosqlite (existing), structlog, Pydantic Settings.

## Week-1 baseline calibration (V21 MUST-FIX #2 fold)

The `SQLITE_WAL_BLOAT_BYTES = 50_000_000` default is a Rorschach threshold without empirical baseline. **Operator procedure:**

1. Deploy with default. `sqlite_wal_probe` events emit at DEBUG every hour for week 1.
2. After 168 probes (~1 week): `scripts/wal_summary.sh 168` reports `p50 / p95 / max wal_size_bytes`.
3. Operator sets `SQLITE_WAL_BLOAT_BYTES` in `.env` to `ceil(p95 × 1.5)` rounded to nearest 5MB. Restarts service.
4. Weeks 2-4 run with the tuned threshold; `sqlite_wal_bloat_observed` now meaningful.

If operator skips calibration: default 50MB stays in effect. False-positive rate depends on actual baseline; document in the BL follow-up.

## Decision criteria (pre-registered per V14 anchor)

After the full 4-week measurement window (~2026-06-14) with the operator-tuned threshold from Week-1 calibration:

| Condition | Action |
|---|---|
| **WAL bloat sustained**: `sqlite_wal_bloat_observed` fires on ≥12 STRICTLY consecutive hourly probes (V21#3 fold — any dip below threshold resets the streak; `wal_summary.sh` reports max consecutive-run length per the awk-aggregator, V20 SHOULD-FIX fold) | **TUNE** — lower `wal_autocheckpoint` from default 1000 pages OR add explicit `PRAGMA wal_checkpoint(TRUNCATE)` after each hourly prune |
| **Runaway WAL**: any single probe shows `wal_size_bytes > 500MB` | **TUNE-IMMEDIATELY** — escalate; runaway detection |
| **DB-file fragmentation**: `freelist_count > 0.10 × page_count` on ANY single probe (V21#1 fold — freelist is monotonic-until-VACUUM, "sustained" was operationally undefined; one-shot trigger is the right shape) | **VACUUM scheduled** (separate scope; file BL-NEW-SQLITE-VACUUM-SCHEDULE) |
| **Zero events**: zero `sqlite_wal_bloat_observed` AND zero freelist-trip AND zero runaway events in the 4-week window after Week-1 calibration | **ACCEPT** (default config sufficient at observed load) |

Filed `BL-NEW-SQLITE-WAL-TUNING-DECISION` with these criteria + memory checkpoint for 2026-06-14.

---

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| SQLite WAL size monitoring / autocheckpoint tuning | None — Hermes Skill Hub (DevOps + MLOps categories, 2026-05-17 probe). | Build in-tree. |
| Generic SQLite PRAGMA helpers | None — aiosqlite + raw PRAGMA calls suffice | Build in-tree. |

awesome-hermes-agent: 404 (consistent). **Verdict:** custom-code path; mirror cycle 3's measurement-layer pattern.

## Drift check (post-fetch)

- **Step 0 (per memory `feedback_drift_check_after_pull.md`):** `git fetch && git log -10 origin/master` → top is `a36acd6` cycle 3 merge. Branch off origin/master.
- `grep wal_size|wal_pages|wal_autocheckpoint scout/ scripts/` → no matches. Net-new instrumentation surface.
- `scout/db.py:86 PRAGMA journal_mode=WAL` already enabled at initialize. Default `wal_autocheckpoint = 1000 pages`; default `journal_size_limit = -1` (no limit). Net-new instrumentation surface.

Backlog: `BL-NEW-SQLITE-WAL-PROFILE` filed 2026-05-13 from BL-NEW-CYCLE-CHANGE-AUDIT. decision-by 8 weeks. This PR measures; the follow-up `BL-NEW-SQLITE-WAL-TUNING-DECISION` files the decision.

## Reader-window analysis (V21 SHOULD-FIX fold)

| Consumer | What it reads | Window |
|---|---|---|
| Operator (manual) | `journalctl ... \| grep sqlite_wal_probe` (DEBUG-level — requires `-p debug` flag) | journalctl default ~30d retention |
| Operator (manual) | `journalctl ... \| grep sqlite_wal_bloat_observed` (WARNING — always visible) | same |
| `scripts/wal_summary.sh` aggregator | both events from journalctl + archive | 4-week window via archive |
| Follow-up TUNE decision (out of scope) | aggregated event counts | TBD post-measurement |

No code consumer. Same shape as cycle 3 — operator-only via journalctl + archive.

---

## File map

- **Modify:**
  - `scout/db.py` — add `Database.probe_wal_state()` method (~line 4900 next to other PRAGMA-using helpers)
  - `scout/main.py` — call `db.probe_wal_state()` + structured log in `_run_hourly_maintenance` (after the 6 narrative prunes from cycle 2)
  - `scout/config.py` — add `SQLITE_WAL_PROFILE_ENABLED: bool = True` + `SQLITE_WAL_BLOAT_BYTES: int = 50_000_000`
- **Create:**
  - `scripts/wal_summary.sh` — read journalctl `sqlite_wal_probe` + `sqlite_wal_bloat_observed` events; emit summary (max wal_size, % of probes over threshold, time-of-day histogram of bloat events)
  - `scripts/wal_archive.sh` — weekly cron dumping events to `/var/log/gecko-alpha/wal-archive/` (mirrors cycle 3 archive script)
  - `tests/test_wal_probe.py` — unit tests for `probe_wal_state()`
- **Backlog:** close BL-NEW-SQLITE-WAL-PROFILE + file BL-NEW-SQLITE-WAL-TUNING-DECISION
- **Memory:** `project_sqlite_wal_tuning_checkpoint_2026_06_14.md` (cycle 1/3 pattern)

---

## Tasks

### Task 1: Settings + probe method

**Files:** `scout/config.py`, `scout/db.py`, `tests/test_config.py`, `tests/test_wal_probe.py`

- [ ] **Step 1.1: Failing tests**

```python
# tests/test_config.py — append
def test_sqlite_wal_profile_enabled_default_true():
    s = Settings(_env_file=None, TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
    assert s.SQLITE_WAL_PROFILE_ENABLED is True


def test_sqlite_wal_bloat_bytes_default_50mb():
    s = Settings(_env_file=None, TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
    assert s.SQLITE_WAL_BLOAT_BYTES == 50_000_000
```

```python
# tests/test_wal_probe.py (new)
import pytest
from pathlib import Path
from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "wal_test.db"))
    await database.initialize()
    yield database
    await database.close()


async def test_probe_wal_state_returns_required_fields(db):
    state = await db.probe_wal_state()
    # V20 SHOULD-FIX: explicit type assertions catch future PRAGMA driver
    # changes (e.g., None returns) that would otherwise be papered over.
    assert isinstance(state["wal_size_bytes"], int)
    assert isinstance(state["wal_pages"], int)
    assert isinstance(state["shm_size_bytes"], int)
    assert isinstance(state["db_size_bytes"], int)
    assert isinstance(state["page_count"], int)
    assert isinstance(state["page_size"], int)
    assert isinstance(state["freelist_count"], int)
    assert isinstance(state["wal_autocheckpoint"], int)
    # V20 MUST-FIX #2: defensive lowercase compare
    assert state["journal_mode"] == "wal", (
        f"journal_mode={state['journal_mode']!r} — WAL mode silently rejected? "
        f"PRAGMA journal_mode=WAL is set in Database.initialize()"
    )
    # Non-negative sanity
    assert state["wal_size_bytes"] >= 0
    assert state["shm_size_bytes"] >= 0
    assert state["page_count"] > 0  # tables exist post-initialize


async def test_probe_wal_state_after_writes(db, token_factory):
    # Initial probe — small/empty
    initial = await db.probe_wal_state()
    initial_pages = initial["wal_pages"]

    # Force some writes
    for i in range(100):
        token = token_factory(contract_address=f"0xtest_{i}", quant_score=50.0)
        await db.upsert_candidate(token)

    after = await db.probe_wal_state()
    # wal_pages may or may not grow (depends on autocheckpoint firing);
    # db_size_bytes definitely grows
    assert after["db_size_bytes"] > initial["db_size_bytes"]


async def test_probe_wal_state_wal_file_size_matches_stat(db):
    """If a .db-wal sidecar exists, its size should equal wal_size_bytes."""
    state = await db.probe_wal_state()
    wal_path = Path(db._db_path + "-wal")
    if wal_path.exists():
        assert state["wal_size_bytes"] == wal_path.stat().st_size
    else:
        assert state["wal_size_bytes"] == 0
```

- [ ] **Step 1.2: Run → FAIL**

- [ ] **Step 1.3: Add settings fields in `scout/config.py`** (near other observability flags from cycle 3):

```python
    # BL-NEW-SQLITE-WAL-PROFILE: hourly SQLite WAL size probe.
    # Default True for 4-week measurement window; pre-registered TUNE
    # threshold at SQLITE_WAL_BLOAT_BYTES (50MB default — well above
    # SQLite's default 1000-page = ~4MB autocheckpoint trigger).
    SQLITE_WAL_PROFILE_ENABLED: bool = True
    SQLITE_WAL_BLOAT_BYTES: int = 50_000_000
```

- [ ] **Step 1.4: Add `Database.probe_wal_state()`** in `scout/db.py` (near `prune_score_history`):

```python
    # ------------------------------------------------------------------
    # Observability — measurement-only methods (no DB mutations)
    # ------------------------------------------------------------------

    async def probe_wal_state(self) -> dict:
        """Read SQLite WAL + DB size pragmas for observability.

        BL-NEW-SQLITE-WAL-PROFILE cycle 4. Called hourly from
        scout.main._run_hourly_maintenance to detect WAL bloat. Returns
        a structured dict for log emission; values are near-real-time and
        may lag pending writes by ms-scale (V20 fold concurrency note).

        V20 PR-review MUST-FIX #1: `PRAGMA wal_autocheckpoint` (no arg)
        has a documented checkpoint side-effect — it triggers a passive
        checkpoint if the page-count threshold is currently exceeded.
        We use the table-valued `pragma_wal_autocheckpoint` form to read
        the value WITHOUT side effects. Per SQLite docs:
        https://www.sqlite.org/pragma.html#pragma_wal_autocheckpoint
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        import os

        async def _pragma(name: str) -> object:
            cur = await self._conn.execute(f"PRAGMA {name}")
            row = await cur.fetchone()
            return row[0] if row else None

        # `journal_mode` is a pure read when called with no argument; SQLite
        # returns the mode normalized to lowercase. Apply .lower() defensively
        # in case driver normalization differs (V20 MUST-FIX #2).
        jm_raw = await _pragma("journal_mode")
        journal_mode = str(jm_raw).lower() if jm_raw is not None else None
        page_count = int(await _pragma("page_count") or 0)
        page_size = int(await _pragma("page_size") or 4096)
        freelist_count = int(await _pragma("freelist_count") or 0)

        # V20 MUST-FIX #1: read autocheckpoint via table-valued function form
        # (pure read, no side effect) instead of `PRAGMA wal_autocheckpoint`
        # which can trigger a checkpoint as a side effect.
        cur = await self._conn.execute("SELECT * FROM pragma_wal_autocheckpoint")
        ac_row = await cur.fetchone()
        wal_autocheckpoint = int(ac_row[0]) if ac_row else 0

        # WAL + SHM file sizes from filesystem (sidecars `<db>-wal`, `<db>-shm`)
        wal_path = self._db_path + "-wal"
        shm_path = self._db_path + "-shm"
        wal_size_bytes = (
            os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
        )
        shm_size_bytes = (
            os.path.getsize(shm_path) if os.path.exists(shm_path) else 0
        )
        wal_pages = wal_size_bytes // page_size if page_size else 0

        return {
            "wal_size_bytes": wal_size_bytes,
            "wal_pages": wal_pages,
            "shm_size_bytes": shm_size_bytes,  # V20#3 fold
            "db_size_bytes": page_count * page_size,
            "page_count": page_count,
            "page_size": page_size,
            "freelist_count": freelist_count,
            "journal_mode": journal_mode,
            "wal_autocheckpoint": wal_autocheckpoint,
        }
```

- [ ] **Step 1.5: Run → PASS. Commit.**

---

### Task 2: Hourly wiring in `_run_hourly_maintenance`

**Files:** `scout/main.py`, `tests/test_hourly_maintenance.py`

- [ ] **Step 2.1: Failing integration test** — assert `_run_hourly_maintenance` calls `db.probe_wal_state()` when enabled and emits `sqlite_wal_probe` log:

```python
async def test_run_hourly_maintenance_emits_wal_probe_when_enabled(tmp_path):
    settings = _make_settings(tmp_path)  # SQLITE_WAL_PROFILE_ENABLED defaults True
    db = _make_db_mock()
    db.probe_wal_state = AsyncMock(return_value={
        "wal_size_bytes": 1024, "wal_pages": 0, "db_size_bytes": 4096,
        "freelist_count": 0, "journal_mode": "wal", "wal_autocheckpoint": 1000,
    })
    session = MagicMock()
    import structlog
    with structlog.testing.capture_logs() as logs:
        await _run_hourly_maintenance(db, session, settings, MagicMock())
    probe_events = [e for e in logs if e.get("event") == "sqlite_wal_probe"]
    assert len(probe_events) == 1
    assert probe_events[0]["wal_size_bytes"] == 1024


async def test_run_hourly_maintenance_emits_bloat_observed_above_threshold(tmp_path):
    settings = _make_settings(tmp_path, SQLITE_WAL_BLOAT_BYTES=1000)
    db = _make_db_mock()
    db.probe_wal_state = AsyncMock(return_value={
        "wal_size_bytes": 1_000_000, "wal_pages": 244, "db_size_bytes": 4096,
        "freelist_count": 0, "journal_mode": "wal", "wal_autocheckpoint": 1000,
    })
    session = MagicMock()
    import structlog
    with structlog.testing.capture_logs() as logs:
        await _run_hourly_maintenance(db, session, settings, MagicMock())
    bloat = [e for e in logs if e.get("event") == "sqlite_wal_bloat_observed"]
    assert len(bloat) == 1
    assert bloat[0]["wal_size_bytes"] == 1_000_000


async def test_run_hourly_maintenance_skips_wal_probe_when_disabled(tmp_path):
    settings = _make_settings(tmp_path, SQLITE_WAL_PROFILE_ENABLED=False)
    db = _make_db_mock()
    db.probe_wal_state = AsyncMock()
    session = MagicMock()
    await _run_hourly_maintenance(db, session, settings, MagicMock())
    db.probe_wal_state.assert_not_called()
```

(_make_settings extended with kwargs override; _make_db_mock extended with probe_wal_state default returning empty dict.)

- [ ] **Step 2.2: Run → FAIL**

- [ ] **Step 2.3: Wire in `_run_hourly_maintenance`** after the cycle 2 narrative-prune loop:

```python
    # BL-NEW-SQLITE-WAL-PROFILE cycle 4: hourly WAL state probe.
    # V21 SHOULD-FIX fold: emit at DEBUG to keep journalctl clean
    # (matches cycle 3 tg_dispatch_observed downgrade rationale). Bloat
    # event stays at WARNING — that's the actionable signal.
    if settings.SQLITE_WAL_PROFILE_ENABLED:
        try:
            state = await db.probe_wal_state()
            logger.debug("sqlite_wal_probe", **state)
            if state.get("wal_size_bytes", 0) > settings.SQLITE_WAL_BLOAT_BYTES:
                logger.warning(
                    "sqlite_wal_bloat_observed",
                    threshold_bytes=settings.SQLITE_WAL_BLOAT_BYTES,
                    **state,
                )
        except Exception:
            logger.exception("sqlite_wal_probe_failed")
```

- [ ] **Step 2.4: Run → PASS. Commit.**

---

### Task 3: Operator helper scripts

**Files:** `scripts/wal_summary.sh`, `scripts/wal_archive.sh`

Mirror cycle 3's `tg_burst_summary.sh` + `tg_burst_archive.sh` shape. Archive: weekly cron, dated filename rotation, same-day re-run appends `.N` suffix, 8-week retention, filename-date-based rotation (not mtime). Summary script reads journalctl `-p debug` AND archive (since probe is DEBUG-level per V21 fold).

**V20 SHOULD-FIX fold — longest-consecutive-run aggregator in `wal_summary.sh`:**

```bash
# Compute longest consecutive-run of bloat events from journalctl/archive.
# Pre-registered TUNE criterion (≥12 strictly consecutive hourly probes) per
# plan §Decision criteria. Each hourly probe is ~1h apart; "consecutive"
# means gap ≤ 90 minutes between sorted timestamps.
COMBINED_BLOAT=$(printf "%s\n" "$COMBINED" | grep '"event": "sqlite_wal_bloat_observed"' | jq -r '.timestamp' | sort)
MAX_RUN=$(printf "%s\n" "$COMBINED_BLOAT" | awk '
BEGIN { run = 0; max = 0; prev = 0 }
{
    cmd = "date -d \"" $0 "\" +%s"
    cmd | getline ts
    close(cmd)
    if (prev == 0 || ts - prev <= 5400) {
        run++
    } else {
        if (run > max) max = run
        run = 1
    }
    prev = ts
}
END {
    if (run > max) max = run
    print max
}')
echo "Longest consecutive-run of bloat events: $MAX_RUN"
echo "(Pre-registered TUNE threshold: ≥12 consecutive)"
```

Output gives operator a one-line answer to the TUNE criterion.

- [ ] Add Week-1 baseline section to `wal_summary.sh` for first-pass calibration:

```bash
echo "--- Week-1 baseline calibration ---"
# p50/p95/max wal_size_bytes from sqlite_wal_probe events
P_OUTPUT=$(printf "%s\n" "$COMBINED" | grep '"event": "sqlite_wal_probe"' | jq -r '.wal_size_bytes' | sort -n)
if [[ -n "$P_OUTPUT" ]]; then
    N=$(printf "%s\n" "$P_OUTPUT" | wc -l)
    P50=$(printf "%s\n" "$P_OUTPUT" | awk -v n="$N" 'NR == int(n/2)+1')
    P95=$(printf "%s\n" "$P_OUTPUT" | awk -v n="$N" 'NR == int(n*0.95)+1')
    MAX=$(printf "%s\n" "$P_OUTPUT" | tail -1)
    echo "  p50 wal_size_bytes: $P50"
    echo "  p95 wal_size_bytes: $P95"
    echo "  max wal_size_bytes: $MAX"
    SUGGESTED=$(awk -v p="$P95" 'BEGIN { print int((p * 1.5 + 4999999) / 5000000) * 5000000 }')
    echo "  Suggested SQLITE_WAL_BLOAT_BYTES (~1.5× p95, rounded to 5MB): $SUGGESTED"
fi
```

- [ ] Implement + chmod +x + commit

---

### Task 4: Memory checkpoint + backlog

**Files:** `~/.claude/projects/.../memory/project_sqlite_wal_tuning_checkpoint_2026_06_14.md`, `backlog.md`

- [ ] Mirror cycle 3 memory file shape with: criteria, `wal_summary.sh 672` invocation, archive dir pointer
- [ ] Close BL-NEW-SQLITE-WAL-PROFILE; file BL-NEW-SQLITE-WAL-TUNING-DECISION with criteria

---

## Test plan summary

- 2 config tests
- 3 probe-method tests (required fields, post-write growth, wal-file-size match)
- 3 hourly-maintenance integration tests (emits, bloat-threshold, disabled-skip)
- Full regression must pass

Total: 8 new tests.

---

## Deployment verification (autonomous)

1. journalctl retention probe (informational; archive cron runs regardless)
2. Install `wal_archive.sh` cron unconditionally + mkdir/chmod
3. Restart + verify `sqlite_wal_probe` (DEBUG) emits within first hour: `journalctl -u gecko-pipeline --since "5 minutes ago" -p debug | grep sqlite_wal_probe | head -3`
4. `wal_summary.sh 1` smoke test
5. Memory checkpoint already filed pre-merge
6. **Week-1 baseline calibration reminder** in memory checkpoint + BL follow-up: at ~2026-05-24 (7 days post-deploy), operator runs `wal_summary.sh 168` and sets `SQLITE_WAL_BLOAT_BYTES` per the script's suggested value
7. Pre-registered review at 2026-06-14

---

## Out of scope

- Active WAL tuning (lower `wal_autocheckpoint`, explicit `PRAGMA wal_checkpoint(TRUNCATE)`) — measurement first; decision per `BL-NEW-SQLITE-WAL-TUNING-DECISION`
- DB-side VACUUM scheduling — separate concern; `freelist_count` metric surfaces the need
- §12a watchdog on WAL probe — covered by deferred §12a daemon item
