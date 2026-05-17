**New primitives introduced:** `scout/trading/revival_criteria.py` (new module: dataclasses `ClosedTrade` / `WindowDiagnostics` / `RevivalCriteriaResult` + enum `RevivalVerdict` 4 values; pure functions `compute_no_breakout_and_loss_rate`, `compute_stop_loss_frequency`, `compute_expired_loss_frequency`, `compute_exit_machinery_contribution`, `compute_wilson_lb`, `compute_bootstrap_lb_per_trade`, `split_at_cutover_boundary`; async DB layer `fetch_closed_trades`, `find_latest_regime_cutover`, `signal_type_exists`, `find_existing_keep_verdict`, `compute_recent_trade_rate`; orchestrator `evaluate_revival_criteria`; CLI helpers `_print_verdict`, `_emit_soak_verdict_sql`, `_validate_signal_type`, `_sql_escape`, `_parse_cutover_iso`; CLI `python -m scout.trading.revival_criteria <signal_type> [--cutover-iso ISO] [--operator NAME] [--emit-sql-only]`); 9 new `Settings` keys (see Task 8); 1 new findings doc (`tasks/findings_lc_revival_criteria_tightening_2026_05_17.md`); 1 new baselines doc (`tasks/baselines_revival_criteria_2026_05_17.md`). No DB schema changes. No modifications to `scout/db.py`, `scout/trading/auto_suspend.py`, `scout/main.py`, or `revive_signal_with_baseline`. No new alert paths. No production-runtime side-effects.

# losers_contrarian Revival-Criteria Tightening Implementation Plan (v3 — post-design-review fold)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a read-only revival-criteria evaluator that enforces (a) n≥100 floor, (b) cutover-stratified two-window split (with operator-revival exclusion), (c) Wilson-LB win-rate gate + bootstrap-LB per-trade-PnL gate per window, (d) 3 secondary diagnostic gates — before any future verdict can be defensibly written to `signal_params_audit`. **The verdict written is `keep_on_provisional_until_<iso>` (default 30d expiry), NOT `keep_on_permanent`** — the rename embeds a verdict-revocation expiry semantic that prevents the silent-falsification class the 2026-05-13 LC verdict belonged to. Active watchdog enforcement of expiry is a follow-up backlog item. No live config changes by this PR.

**Architecture:** New sibling module `scout/trading/revival_criteria.py` next to `auto_suspend.py` and `calibrate.py`. Pure async functions reading `paper_trades` + `signal_params_audit` + `signal_params`. CLI supports two modes: default prints diagnostic prose + SQL block; `--emit-sql-only` suppresses prose and prints clean SQL suitable for shell redirect. On PASS emits `BEGIN IMMEDIATE` + `PRAGMA busy_timeout=30000` + `INSERT ... soak_verdict='keep_on_provisional_until_<iso>'` + `COMMIT` block with full SQL-escape coverage on interpolated values + signal-type regex validation. Settings-driven thresholds, all defaultable via `.env`. PROVISIONAL default values from Task 0 empirical baseline derivation against healthy signals (chain_completed, volume_spike, narrative_prediction).

**Tech Stack:** Python 3.11, aiosqlite, structlog, Pydantic Settings, pytest-asyncio. Bootstrap CI uses stdlib `random.choices` + `statistics` (no numpy — avoids the Windows OpenSSL workaround per memory `reference_windows_openssl_workaround.md`).

**Hermes-first analysis:**

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Trading signal revival decisions | None (Hermes skill hub WebFetch 2026-05-17 + cycle-9 doc confirmation) | Build from scratch |
| Cutover-stratified backtest | None | Build (gecko-alpha-specific) |
| Signal-quality diagnostics | None | Build (gecko-alpha exit_reason taxonomy) |
| Wilson lower bound | stdlib math | Use stdlib; document derivation |
| Bootstrap CI on per-trade pnl | stdlib `random.choices` | Use stdlib; document seed |

awesome-hermes-agent ecosystem 404 consistent. Verdict: custom build justified.

**Drift-check:** `git fetch origin && git log -10 origin/master` performed 2026-05-17. Worktree HEAD = `5860d1740946a3f0838f3fba0b512d1216c35fde` = origin/master (zero divergence). Top 3 commits: `5860d17 fix(chains)`, `7fbd17f feat(audit): other-prod-config`, `256b169 feat(systemd): drift-watchdog`. Grep for `revival_criteria|no_breakout_and_loss|exit_machinery_contribution|wilson_lb|bootstrap_lb_per_trade|keep_on_provisional` returns ZERO files — diagnostic surface NET-NEW.

## Relationship to cycle-9 first_signal precedent

Cycle-9 used n≥10 trip-wire + NON-REGRESSION KEEP threshold for a **continue-observing decision** (reversible). This plan uses n≥100 + cutover-stratified bilateral gates + Wilson/bootstrap LB for a **`keep_on_provisional_until_<iso>` verdict-stamp decision** (time-boxed, revocable). Different decision classes, stricter bar appropriate for the more-load-bearing verdict.

## V3 fold summary (post-design-review)

| Finding | Severity | Resolution in v3 |
|---|---|---|
| C#1 — `_REGIME_BOUNDARY_FIELDS` allowlist misses calibrate fields | CRITICAL | Switched to **denylist** `_REGIME_NON_BOUNDARY_FIELDS = ('soak_verdict', 'last_calibration_at')` |
| C#6 — operator-revival treated as regime cutover | CRITICAL | `find_latest_regime_cutover` skips rows where `applied_by='operator' AND field_name='enabled' AND new_value='1'` (operator revival shape) — returns the prior boundary instead |
| C#9 / D#1 — output capture / paste friction | CRITICAL | New `--emit-sql-only` flag; defaults OFF (preserves diagnostic prose); when ON, prints ONLY the BEGIN/INSERT/COMMIT block |
| D#2 — no post-verdict monitoring | CRITICAL | Verdict renamed `keep_on_permanent` → `keep_on_provisional_until_<iso>` with `REVIVAL_CRITERIA_VERDICT_EXPIRY_DAYS=30` default; active watchdog deferred to backlog follow-up `BL-NEW-REVIVAL-VERDICT-WATCHDOG` (filed in Task 14) |
| C#2 — cutover staleness | IMPORTANT | `_print_verdict` prints `cutover_age_days` + WARN at >30d |
| C#3 — `BEGIN IMMEDIATE` vs writer lock | IMPORTANT | Emitted SQL gains `PRAGMA busy_timeout=30000;` atop; runbook §6d adds `systemctl stop` before paste |
| C#5 / D#4 — `--cutover-iso` validation | IMPORTANT | Argparse `type=_parse_cutover_iso` + range-check against `min/max(closed_at)`; OVERRIDE WARNING block printed when override differs from audit-derived cutover; embedded in SQL reason field |
| C#10 — signal_type typo path | IMPORTANT | New `signal_type_exists()` check; aborts with friendly error before evaluation |
| D#3 — absolute Wilson/bootstrap thresholds | IMPORTANT | Task 0 baseline derivation drives Settings defaults; defaults flagged `# PROVISIONAL — replaced post-Task-0` |
| D#5 — cool-off note hidden in SQL comment | IMPORTANT | `_print_verdict` prints `Cool-off status: ACTIVE until <iso>` or `CLEAR` on stdout BEFORE SQL block |
| D#6 — `BELOW_MIN_TRADES` has no re-eligible projection | IMPORTANT | `compute_recent_trade_rate()` + projection printed on BELOW verdict |
| D#7 — FAIL + existing keep verdict not surfaced | IMPORTANT | `find_existing_keep_verdict()` queries audit; if FAIL contradicts an existing keep, prints attention block with revoke SQL |
| C#4 — runbook §6e race | MINOR | Annotated as "informational; authoritative check is inside `revive_signal_with_baseline`" |
| C#12 — test platform note absent | MINOR | Added to design §3a (tests run on srilu Linux; Windows may hit OpenSSL workaround per memory) |
| C#14 — empty-string `--cutover-iso` | IMPORTANT | Argparse `type=` validator gives friendly error |
| C#15 — `Database._conn` private access | MINOR | Documented as deliberate-tight-coupling in module docstring |
| D#9 — gainers_early findings doc DO-NOT-ACT warning | MINOR | Task 13 step prepends WARNING |
| D#10 — threshold drift / no recalibration cadence | MINOR | Filed `BL-NEW-REVIVAL-CRITERIA-QUARTERLY-RECALIBRATION` follow-up in Task 14 |

---

## Existing primitives composed (NOT re-built)

- `Database.revive_signal_with_baseline` at `scout/db.py:4157` — operator-facing revival helper. Disjoint from this evaluator: cool-off filter at `db.py:4214` keys on `field_name='enabled'`; emitted `field_name='soak_verdict'` rows do NOT trip cool-off, by design.
- `scout/trading/calibrate.py` — CLI sibling pattern.
- `signal_params_audit` table — existing audit surface.

---

## Files to create / modify

### Create
- `scout/trading/revival_criteria.py` (~600 LOC est.)
- `tests/test_revival_criteria.py` (~26 tests est.)
- `tasks/baselines_revival_criteria_2026_05_17.md` (Task 0)
- `tasks/findings_lc_revival_criteria_tightening_2026_05_17.md` (Task 13)

### Modify
- `scout/config.py` — 9 Settings keys with field_validators (Task 8)
- `backlog.md` — flip status PROPOSED → SHIPPED at PR-merge time (Task 14)
- `tasks/todo.md` — flip board item at PR-merge time (Task 14)

### Do NOT modify
- `scout/db.py`, `scout/trading/auto_suspend.py`, `scout/main.py`, `scout/trading/calibrate.py`
- `.env` on srilu-vps — no operator config flips

---

## Task decomposition

All tasks use the worktree at `C:\projects\gecko-alpha\.claude\worktrees\feat+lc-revival-criteria-tightening`. Tests verified at `uv run pytest`.

### Task 0: Empirical baseline derivation (MUST run BEFORE Task 8)

**Files:** Create `tasks/baselines_revival_criteria_2026_05_17.md`

- [ ] **Step 1: Pull srilu scout.db read-only**

```bash
scp srilu-vps:/root/gecko-alpha/scout.db /tmp/scout_baseline.db > .ssh_scp_baseline.txt 2>&1
```

- [ ] **Step 2: Compute baseline diagnostics for chain_completed + volume_spike + narrative_prediction**

```sql
WITH closed AS (
  SELECT pnl_usd, peak_pct, exit_reason
  FROM paper_trades
  WHERE signal_type = ?
    AND status LIKE 'closed_%'
    AND pnl_usd IS NOT NULL AND pnl_pct IS NOT NULL
)
SELECT
  COUNT(*) AS n,
  ROUND(AVG(CASE WHEN (peak_pct IS NULL OR peak_pct <= 5.0) AND pnl_usd < 0 THEN 1.0 ELSE 0.0 END), 3) AS no_breakout_and_loss_rate,
  ROUND(AVG(CASE WHEN exit_reason = 'stop_loss' THEN 1.0 ELSE 0.0 END), 3) AS stop_loss_freq,
  ROUND(AVG(CASE WHEN exit_reason IN ('expired','expired_stale_price') AND pnl_usd < 0 THEN 1.0 ELSE 0.0 END), 3) AS expired_loss_freq,
  ROUND(
    SUM(CASE WHEN exit_reason IN ('peak_fade','trailing_stop','moonshot_trail') AND pnl_usd > 0 THEN pnl_usd ELSE 0.0 END)
    / NULLIF(SUM(CASE WHEN pnl_usd > 0 THEN pnl_usd ELSE 0.0 END), 0), 3) AS exit_machinery_contribution
FROM closed;
```

- [ ] **Step 3: Set provisional thresholds as `max(measured_healthy_value)` with 50% margin (conservative)**

For ratio thresholds: gate value = `1.5 × max(healthy_baseline)` for failure-mode metrics (no_breakout_and_loss, stop_loss_freq); gate value = `0.5 × min(healthy_baseline)` for must-have metrics (exit_machinery_contribution). Document numeric derivations in `baselines_revival_criteria_2026_05_17.md` per signal_type.

- [ ] **Step 4: Commit baseline doc**

```bash
git add tasks/baselines_revival_criteria_2026_05_17.md
git commit -m "docs(revival-criteria): empirical baselines from chain_completed + volume_spike + narrative_prediction"
```

---

### Task 1: dataclasses + 4-value enum

**Files:** Create `scout/trading/revival_criteria.py`, Create `tests/test_revival_criteria.py`

- [ ] **Step 1: Write failing test** (asserts 4 enum values + dataclass shapes; full code per v2 plan Task 1)

```python
def test_dataclasses_and_4_value_enum():
    assert {v.value for v in RevivalVerdict} == {
        "pass", "fail", "below_min_trades", "stratification_infeasible",
    }
```

(Full test body in v2 plan Task 1 step 1 — identical fields except `WindowDiagnostics` adds `win_pct_wilson_lb` + `per_trade_bootstrap_lb`; `RevivalCriteriaResult` adds `cutover_at` + `cutover_source` + `cutover_age_days`.)

- [ ] **Step 2: Run → FAIL** with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# scout/trading/revival_criteria.py
"""Revival-criteria evaluator (BL-NEW-LOSERS-CONTRARIAN-REVIVAL-CRITERIA-TIGHTENING).

Pure read-only evaluator. Reads paper_trades + signal_params_audit + signal_params.
On PASS verdict, CLI emits SQL the operator pastes to write a
`keep_on_provisional_until_<iso>` (30d default expiry) audit row.

Note on Database._conn private access (per C#15 fold): this module reaches into
db._conn directly via fetch_closed_trades + find_latest_regime_cutover +
signal_type_exists + find_existing_keep_verdict + compute_recent_trade_rate.
This mirrors the existing project convention (see scout/trading/calibrate.py
and scout/trading/auto_suspend.py). If Database._conn is renamed in a future
refactor, this module breaks loudly with AttributeError on import-resolve
(deliberate; cheap to detect).

See plan: tasks/plan_lc_revival_criteria_tightening.md (v3)
See design: tasks/design_lc_revival_criteria_tightening.md (v2)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class RevivalVerdict(Enum):
    PASS = "pass"
    FAIL = "fail"
    BELOW_MIN_TRADES = "below_min_trades"
    STRATIFICATION_INFEASIBLE = "stratification_infeasible"


@dataclass(frozen=True)
class ClosedTrade:
    id: int
    signal_type: str
    pnl_usd: float
    pnl_pct: float
    peak_pct: float | None
    exit_reason: str | None
    closed_at: datetime


@dataclass(frozen=True)
class WindowDiagnostics:
    start_at: datetime
    end_at: datetime
    n: int
    net_pnl_usd: float
    per_trade_usd: float
    win_pct: float
    win_pct_wilson_lb: float           # %
    per_trade_bootstrap_lb: float      # $
    no_breakout_and_loss_rate: float
    stop_loss_frequency: float
    expired_loss_frequency: float
    exit_machinery_contribution: float


@dataclass(frozen=True)
class RevivalCriteriaResult:
    signal_type: str
    verdict: RevivalVerdict
    n_trades: int
    cutover_at: datetime | None
    cutover_source: str               # e.g., "signal_params_audit:auto_suspend:enabled" or "operator_override" or "no_audit_events"
    cutover_age_days: int | None
    window_a: WindowDiagnostics | None
    window_b: WindowDiagnostics | None
    failure_reasons: list[str] = field(default_factory=list)
    evaluated_at: datetime | None = None
```

- [ ] **Step 4: Run → PASS**

- [ ] **Step 5: Commit**

```bash
git add scout/trading/revival_criteria.py tests/test_revival_criteria.py
git commit -m "feat(revival-criteria): scaffold dataclasses + 4-value verdict enum"
```

---

### Task 2: `compute_no_breakout_and_loss_rate`

(Identical to v2 plan Task 2 — predicate `(peak_pct ≤ threshold OR peak_pct IS NULL) AND pnl_usd < 0`; 4 tests; commit message `feat(revival-criteria): compute_no_breakout_and_loss_rate`.)

---

### Task 3: `compute_stop_loss_frequency` + `compute_expired_loss_frequency`

(Identical to v2 plan Task 3 — 4 tests; commit `feat(revival-criteria): compute_stop_loss_frequency + compute_expired_loss_frequency`.)

---

### Task 4: `compute_exit_machinery_contribution`

(Identical to v2 plan Task 4 — `peak_fade ∪ trailing_stop ∪ moonshot_trail` positive pnl / all positive pnl; 4 tests; commit `feat(revival-criteria): compute_exit_machinery_contribution`.)

---

### Task 5: `compute_wilson_lb`

(Identical to v2 plan Task 5 — Wilson score lower bound, default z=1.96; 4 tests; commit `feat(revival-criteria): compute_wilson_lb on win-rate (CLAUDE.md §11b mandate)`.)

---

### Task 6: `compute_bootstrap_lb_per_trade`

(Identical to v2 plan Task 6 — bootstrap percentile CI, default 10k resamples, seed=42; 3 tests; commit `feat(revival-criteria): compute_bootstrap_lb_per_trade (CLAUDE.md §11b mandate)`.)

---

### Task 7: `split_at_cutover_boundary` + `find_latest_regime_cutover` (V3 — denylist + operator-revival skip)

**V3 fold per C#1 (CRITICAL):** switched `_REGIME_BOUNDARY_FIELDS` allowlist → `_REGIME_NON_BOUNDARY_FIELDS` denylist.
**V3 fold per C#6 (CRITICAL):** `find_latest_regime_cutover` skips rows matching the operator-revival shape (`applied_by='operator' AND field_name='enabled' AND new_value='1'`) and returns the prior boundary instead.

**Files:** Modify `scout/trading/revival_criteria.py`, Modify `tests/test_revival_criteria.py` (7 tests — 4 split + 3 find_cutover incl. operator-revival skip)

- [ ] **Step 1: Write failing tests**

(Tests from v2 plan Task 7 step 1 — UPDATED: `test_find_latest_regime_cutover_skips_operator_revival_and_returns_prior` replaces the simpler v2 test; assert that when the most-recent audit row is `applied_by='operator', field_name='enabled', new_value='1'`, the cutover returned is the PRIOR row.)

```python
@pytest.mark.asyncio
async def test_find_latest_regime_cutover_skips_operator_revival(tmp_path):
    db = Database(str(tmp_path / "scout.db"))
    await db.connect()
    # Prior auto_suspend event
    await db._conn.execute(
        """INSERT INTO signal_params_audit
            (signal_type, field_name, old_value, new_value, reason, applied_by, applied_at)
           VALUES ('losers_contrarian', 'enabled', '1', '0', 'hard_loss', 'auto_suspend', '2026-05-01T00:00:00Z')""",
    )
    # Subsequent operator revival (must be skipped)
    await db._conn.execute(
        """INSERT INTO signal_params_audit
            (signal_type, field_name, old_value, new_value, reason, applied_by, applied_at)
           VALUES ('losers_contrarian', 'enabled', '0', '1', 'op revive', 'operator', '2026-05-06T00:00:00Z')""",
    )
    await db._conn.commit()
    cutover_at, source = await find_latest_regime_cutover(db, "losers_contrarian")
    assert cutover_at == datetime(2026, 5, 1, tzinfo=timezone.utc)
    assert "auto_suspend" in source
    await db.close()


@pytest.mark.asyncio
async def test_find_latest_regime_cutover_returns_calibrate_field(tmp_path):
    """V3 fold per C#1 — denylist must accept calibrate.py's dynamic field names."""
    db = Database(str(tmp_path / "scout.db"))
    await db.connect()
    await db._conn.execute(
        """INSERT INTO signal_params_audit
            (signal_type, field_name, old_value, new_value, reason, applied_by, applied_at)
           VALUES ('losers_contrarian', 'leg_1_pct', '25.0', '30.0', 'calibrate', 'calibrate', '2026-05-10T00:00:00Z')""",
    )
    await db._conn.commit()
    cutover_at, source = await find_latest_regime_cutover(db, "losers_contrarian")
    assert cutover_at == datetime(2026, 5, 10, tzinfo=timezone.utc)
    assert "leg_1_pct" in source
    await db.close()
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Write minimal implementation**

```python
# scout/trading/revival_criteria.py (append)
from datetime import timedelta, timezone


def split_at_cutover_boundary(
    trades: list[ClosedTrade], *,
    cutover_at: datetime,
    min_window_days: int,
    min_window_trades: int,
) -> tuple[list[ClosedTrade], list[ClosedTrade]] | None:
    sorted_trades = sorted(trades, key=lambda t: t.closed_at)
    a = [t for t in sorted_trades if t.closed_at < cutover_at]
    b = [t for t in sorted_trades if t.closed_at >= cutover_at]
    if len(a) < min_window_trades or len(b) < min_window_trades:
        return None
    if (a[-1].closed_at - a[0].closed_at) < timedelta(days=min_window_days):
        return None
    if (b[-1].closed_at - b[0].closed_at) < timedelta(days=min_window_days):
        return None
    return a, b


# V3 fold per C#1: denylist (NOT an allowlist) — accept all audit field_names
# EXCEPT consequence rows that aren't regime triggers.
_REGIME_NON_BOUNDARY_FIELDS = frozenset({"soak_verdict", "last_calibration_at"})


def _is_operator_revival_row(applied_by: str, field_name: str,
                              old_value: str | None, new_value: str | None) -> bool:
    """V3 fold per C#6: operator-revival shape — the OUTCOME of a regime decision,
    not the regime decision itself. The triggering event (auto_suspend, calibrate,
    etc.) is the actual regime cutover; the operator's flip-back is a follow-up.
    """
    return (
        applied_by == "operator"
        and field_name == "enabled"
        and (old_value or "") == "0"
        and (new_value or "") == "1"
    )


async def find_latest_regime_cutover(
    db, signal_type: str
) -> tuple[datetime | None, str]:
    """Return (cutover_at, source) of the latest regime-changing audit row.

    Iterates audit rows from newest to oldest, skipping (a) rows whose
    field_name is in _REGIME_NON_BOUNDARY_FIELDS, and (b) operator-revival
    shape rows. Returns first match or (None, 'no_audit_events').
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    sql = (
        "SELECT applied_at, applied_by, field_name, old_value, new_value "
        "FROM signal_params_audit "
        "WHERE signal_type = ? "
        "ORDER BY applied_at DESC"
    )
    cur = await db._conn.execute(sql, (signal_type,))
    rows = await cur.fetchall()
    for iso, by, field, old_val, new_val in rows:
        if field in _REGIME_NON_BOUNDARY_FIELDS:
            continue
        if _is_operator_revival_row(by, field, old_val, new_val):
            continue
        cutover = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return cutover, f"signal_params_audit:{by}:{field}"
    return None, "no_audit_events"
```

- [ ] **Step 4: Run → 7 PASS**

- [ ] **Step 5: Commit**

```bash
git add scout/trading/revival_criteria.py tests/test_revival_criteria.py
git commit -m "feat(revival-criteria): cutover detection with denylist + operator-revival skip (C#1 + C#6)"
```

---

### Task 8: Settings (9 keys — V3 adds `REVIVAL_CRITERIA_VERDICT_EXPIRY_DAYS`)

**Files:** Modify `scout/config.py`, Modify `tests/test_revival_criteria.py` (1 test)

- [ ] **Step 1: Write failing test**

```python
def test_settings_has_revival_criteria_defaults():
    s = Settings()
    assert s.REVIVAL_CRITERIA_MIN_TRADES == 100
    assert s.REVIVAL_CRITERIA_MIN_WINDOW_DAYS == 7
    assert s.REVIVAL_CRITERIA_MIN_WINDOW_TRADES == 50
    assert s.REVIVAL_CRITERIA_NO_BREAKOUT_PEAK_PCT == 5.0
    assert s.REVIVAL_CRITERIA_MAX_NO_BREAKOUT_AND_LOSS == 0.25  # PROVISIONAL
    assert s.REVIVAL_CRITERIA_EXIT_MACHINERY_MIN == 0.50         # PROVISIONAL
    assert s.REVIVAL_CRITERIA_WIN_WILSON_LB_MIN == 0.50
    assert s.REVIVAL_CRITERIA_BOOTSTRAP_RESAMPLES == 10_000
    assert s.REVIVAL_CRITERIA_VERDICT_EXPIRY_DAYS == 30  # V3 NEW
```

- [ ] **Step 2: Run → FAIL** with AttributeError

- [ ] **Step 3: Add Settings keys + validators**

In `scout/config.py` after `SIGNAL_REVIVAL_MIN_SOAK_DAYS`:

```python
# BL-NEW-LOSERS-CONTRARIAN-REVIVAL-CRITERIA-TIGHTENING:
# Read-only evaluator; values marked # PROVISIONAL come from Task 0 baseline derivation
# (tasks/baselines_revival_criteria_2026_05_17.md) and may be re-tuned via .env.
REVIVAL_CRITERIA_MIN_TRADES: int = 100
REVIVAL_CRITERIA_MIN_WINDOW_DAYS: int = 7
REVIVAL_CRITERIA_MIN_WINDOW_TRADES: int = 50
REVIVAL_CRITERIA_NO_BREAKOUT_PEAK_PCT: float = 5.0
REVIVAL_CRITERIA_MAX_NO_BREAKOUT_AND_LOSS: float = 0.25  # PROVISIONAL
REVIVAL_CRITERIA_EXIT_MACHINERY_MIN: float = 0.50         # PROVISIONAL
REVIVAL_CRITERIA_WIN_WILSON_LB_MIN: float = 0.50
REVIVAL_CRITERIA_BOOTSTRAP_RESAMPLES: int = 10_000
# V3 fold per D#2: time-boxed verdict expiry. Default 30d means a written
# soak_verdict ages out structurally even if the active watchdog
# (BL-NEW-REVIVAL-VERDICT-WATCHDOG follow-up) hasn't shipped yet.
REVIVAL_CRITERIA_VERDICT_EXPIRY_DAYS: int = 30
```

Add validators:

```python
@field_validator(
    "REVIVAL_CRITERIA_MIN_TRADES",
    "REVIVAL_CRITERIA_MIN_WINDOW_TRADES",
    "REVIVAL_CRITERIA_MIN_WINDOW_DAYS",
    "REVIVAL_CRITERIA_BOOTSTRAP_RESAMPLES",
    "REVIVAL_CRITERIA_VERDICT_EXPIRY_DAYS",
)
@classmethod
def _validate_revival_positive_int(cls, v: int) -> int:
    if v < 1:
        raise ValueError(f"revival-criteria count/days thresholds must be >= 1; got={v}")
    return v


@field_validator(
    "REVIVAL_CRITERIA_MAX_NO_BREAKOUT_AND_LOSS",
    "REVIVAL_CRITERIA_EXIT_MACHINERY_MIN",
    "REVIVAL_CRITERIA_WIN_WILSON_LB_MIN",
)
@classmethod
def _validate_revival_ratio(cls, v: float) -> float:
    if not 0.0 <= v <= 1.0:
        raise ValueError(f"revival-criteria ratio must be in [0,1]; got={v}")
    return v


@field_validator("REVIVAL_CRITERIA_NO_BREAKOUT_PEAK_PCT")
@classmethod
def _validate_revival_peak_pct(cls, v: float) -> float:
    if v < 0:
        raise ValueError(f"REVIVAL_CRITERIA_NO_BREAKOUT_PEAK_PCT must be >= 0; got={v}")
    return v
```

- [ ] **Step 4: Run → PASS**

- [ ] **Step 5: Commit**

```bash
git add scout/config.py tests/test_revival_criteria.py
git commit -m "feat(revival-criteria): 9 Settings keys with validators (V3 adds VERDICT_EXPIRY_DAYS)"
```

---

### Task 9: `fetch_closed_trades` + `signal_type_exists` + `find_existing_keep_verdict` + `compute_recent_trade_rate` (V3 — 4 DB helpers)

**Files:** Modify `scout/trading/revival_criteria.py`, Modify `tests/test_revival_criteria.py` (5 tests)

V3 adds three new helpers per design-review folds:
- `signal_type_exists` (C#10 — typo safety)
- `find_existing_keep_verdict` (D#7 — FAIL contradiction surfacing)
- `compute_recent_trade_rate` (D#6 — BELOW projection)

- [ ] **Step 1: Write failing tests**

```python
@pytest.mark.asyncio
async def test_fetch_closed_trades_returns_typed_rows(tmp_path):
    # (same as v2 plan Task 9 step 1 — insert closed trade, fetch, assert dataclass shape)
    ...

@pytest.mark.asyncio
async def test_fetch_closed_trades_null_peak_round_trips(tmp_path):
    # (same as v2 plan Task 9 step 1 NULL-peak test)
    ...

@pytest.mark.asyncio
async def test_signal_type_exists_true_for_default_signals(tmp_path):
    db = Database(str(tmp_path / "scout.db"))
    await db.connect()
    # Database.connect() seeds default signal_params rows; losers_contrarian is one
    assert await signal_type_exists(db, "losers_contrarian") is True
    assert await signal_type_exists(db, "nonexistent_signal_xyz") is False
    await db.close()

@pytest.mark.asyncio
async def test_find_existing_keep_verdict_returns_none_when_no_row(tmp_path):
    db = Database(str(tmp_path / "scout.db"))
    await db.connect()
    result = await find_existing_keep_verdict(db, "losers_contrarian")
    assert result is None
    await db.close()

@pytest.mark.asyncio
async def test_find_existing_keep_verdict_returns_most_recent_keep(tmp_path):
    db = Database(str(tmp_path / "scout.db"))
    await db.connect()
    await db._conn.execute(
        """INSERT INTO signal_params_audit
            (signal_type, field_name, old_value, new_value, reason, applied_by, applied_at)
           VALUES ('losers_contrarian', 'soak_verdict', NULL,
                   'keep_on_provisional_until_2026-06-15T00:00:00Z',
                   'test', 'operator', '2026-05-15T00:00:00Z')""",
    )
    await db._conn.commit()
    iso, value = await find_existing_keep_verdict(db, "losers_contrarian")
    assert iso == "2026-05-15T00:00:00Z"
    assert value.startswith("keep_on_provisional_until_")
    await db.close()

@pytest.mark.asyncio
async def test_compute_recent_trade_rate_returns_trades_per_day(tmp_path):
    db = Database(str(tmp_path / "scout.db"))
    await db.connect()
    # Insert 14 trades over 7 days = 2/day
    for i in range(14):
        await db._conn.execute(
            """INSERT INTO paper_trades (
                token_id, symbol, name, chain, signal_type, signal_data,
                entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
                status, exit_reason, pnl_usd, pnl_pct, peak_pct,
                opened_at, closed_at, created_at
            ) VALUES (?, 'SYM', 'Name', 'solana', 'losers_contrarian', '{}',
                1.0, 100.0, 100.0, 20.0, 25.0, 1.2, 0.75,
                'closed_peak_fade', 'peak_fade', 5.0, 5.0, 10.0,
                ?, ?, ?)""",
            (f"t_{i}", f"2026-05-{1+i//2:02d}T00:00:00Z",
             f"2026-05-{1+i//2:02d}T00:00:00Z", f"2026-05-{1+i//2:02d}T00:00:00Z"),
        )
    await db._conn.commit()
    rate = await compute_recent_trade_rate(db, "losers_contrarian", lookback_days=7)
    assert 1.5 < rate < 2.5  # ~2/day
    await db.close()
```

- [ ] **Step 2: Run → FAIL** with ImportError

- [ ] **Step 3: Write minimal implementation**

```python
# scout/trading/revival_criteria.py (append)

async def fetch_closed_trades(
    db, signal_type: str, *, since: datetime | None = None
) -> list[ClosedTrade]:
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    sql = (
        "SELECT id, signal_type, pnl_usd, pnl_pct, peak_pct, exit_reason, closed_at "
        "FROM paper_trades "
        "WHERE signal_type = ? AND status LIKE 'closed_%' "
        "AND pnl_usd IS NOT NULL AND pnl_pct IS NOT NULL "
    )
    params: list = [signal_type]
    if since is not None:
        sql += "AND datetime(closed_at) >= datetime(?) "
        params.append(since.isoformat())
    sql += "ORDER BY closed_at"
    cur = await db._conn.execute(sql, params)
    rows = await cur.fetchall()
    return [
        ClosedTrade(
            id=r[0], signal_type=r[1],
            pnl_usd=float(r[2]), pnl_pct=float(r[3]),
            peak_pct=float(r[4]) if r[4] is not None else None,
            exit_reason=r[5],
            closed_at=datetime.fromisoformat(r[6].replace("Z", "+00:00")),
        )
        for r in rows
    ]


async def signal_type_exists(db, signal_type: str) -> bool:
    """V3 fold per C#10: protect against signal_type typos before evaluation."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    cur = await db._conn.execute(
        "SELECT 1 FROM signal_params WHERE signal_type = ? LIMIT 1",
        (signal_type,),
    )
    row = await cur.fetchone()
    return row is not None


async def find_existing_keep_verdict(
    db, signal_type: str
) -> tuple[str, str] | None:
    """V3 fold per D#7: return (iso_str, verdict_value) of most-recent
    soak_verdict audit row, or None if no prior verdict exists.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    cur = await db._conn.execute(
        "SELECT applied_at, new_value FROM signal_params_audit "
        "WHERE signal_type = ? AND field_name = 'soak_verdict' "
        "ORDER BY applied_at DESC LIMIT 1",
        (signal_type,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return row[0], row[1]


async def compute_recent_trade_rate(
    db, signal_type: str, *, lookback_days: int = 7
) -> float:
    """V3 fold per D#6: trades-per-day rate for BELOW_MIN_TRADES projection."""
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trades "
        "WHERE signal_type = ? AND status LIKE 'closed_%' "
        "AND datetime(closed_at) >= datetime('now', ?)",
        (signal_type, f"-{lookback_days} days"),
    )
    row = await cur.fetchone()
    n = int(row[0]) if row else 0
    return n / lookback_days if lookback_days > 0 else 0.0
```

- [ ] **Step 4: Run → 5 PASS** (allowing for the first 2 reused from v2 plan Task 9)

- [ ] **Step 5: Commit**

```bash
git add scout/trading/revival_criteria.py tests/test_revival_criteria.py
git commit -m "feat(revival-criteria): fetch_closed_trades + 3 new DB helpers (typo/contradiction/rate)"
```

---

### Task 10: `evaluate_revival_criteria` orchestrator (V3 — adds cutover_age_days + BELOW projection + FAIL contradiction surfacing)

**Files:** Modify `scout/trading/revival_criteria.py`, Modify `tests/test_revival_criteria.py` (5 tests, mostly from v2)

- [ ] **Step 1: Write failing tests**

(Tests identical to v2 plan Task 10 step 1: BELOW_MIN_TRADES, STRATIFICATION_INFEASIBLE, PASS, FAIL on window_b bootstrap, explicit cutover override. ADD one test: `cutover_age_days` is populated correctly.)

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Write minimal implementation**

```python
# scout/trading/revival_criteria.py (append)
from scout.config import Settings


def _window_diagnostics(trades: list[ClosedTrade], *, settings: Settings) -> WindowDiagnostics:
    n = len(trades)
    net = sum(t.pnl_usd for t in trades)
    wins = sum(1 for t in trades if t.pnl_usd > 0)
    pnls = [t.pnl_usd for t in trades]
    return WindowDiagnostics(
        start_at=trades[0].closed_at, end_at=trades[-1].closed_at,
        n=n, net_pnl_usd=net,
        per_trade_usd=net / n if n else 0.0,
        win_pct=100.0 * wins / n if n else 0.0,
        win_pct_wilson_lb=100.0 * compute_wilson_lb(wins=wins, n=n),
        per_trade_bootstrap_lb=compute_bootstrap_lb_per_trade(
            pnls, n_resamples=settings.REVIVAL_CRITERIA_BOOTSTRAP_RESAMPLES
        ),
        no_breakout_and_loss_rate=compute_no_breakout_and_loss_rate(
            trades, threshold_pct=settings.REVIVAL_CRITERIA_NO_BREAKOUT_PEAK_PCT
        ),
        stop_loss_frequency=compute_stop_loss_frequency(trades),
        expired_loss_frequency=compute_expired_loss_frequency(trades),
        exit_machinery_contribution=compute_exit_machinery_contribution(trades),
    )


def _evaluate_window_gates(label: str, w: WindowDiagnostics, settings: Settings) -> list[str]:
    failures: list[str] = []
    if w.per_trade_bootstrap_lb <= 0:
        failures.append(f"window_{label}.per_trade_bootstrap_lb=${w.per_trade_bootstrap_lb:.2f} <= 0")
    if w.win_pct_wilson_lb < settings.REVIVAL_CRITERIA_WIN_WILSON_LB_MIN * 100:
        failures.append(
            f"window_{label}.win_pct_wilson_lb={w.win_pct_wilson_lb:.1f}% < "
            f"{settings.REVIVAL_CRITERIA_WIN_WILSON_LB_MIN * 100:.1f}%"
        )
    if w.no_breakout_and_loss_rate > settings.REVIVAL_CRITERIA_MAX_NO_BREAKOUT_AND_LOSS:
        failures.append(
            f"window_{label}.no_breakout_and_loss_rate={w.no_breakout_and_loss_rate:.2f} > "
            f"{settings.REVIVAL_CRITERIA_MAX_NO_BREAKOUT_AND_LOSS}"
        )
    if w.exit_machinery_contribution < settings.REVIVAL_CRITERIA_EXIT_MACHINERY_MIN:
        failures.append(
            f"window_{label}.exit_machinery_contribution={w.exit_machinery_contribution:.2f} < "
            f"{settings.REVIVAL_CRITERIA_EXIT_MACHINERY_MIN}"
        )
    return failures


async def evaluate_revival_criteria(
    db, signal_type: str, settings: Settings, *,
    cutover_override: datetime | None = None,
) -> RevivalCriteriaResult:
    trades = await fetch_closed_trades(db, signal_type)
    now = datetime.now(timezone.utc)
    n = len(trades)

    if n < settings.REVIVAL_CRITERIA_MIN_TRADES:
        return RevivalCriteriaResult(
            signal_type=signal_type,
            verdict=RevivalVerdict.BELOW_MIN_TRADES,
            n_trades=n, cutover_at=None,
            cutover_source="not_evaluated", cutover_age_days=None,
            window_a=None, window_b=None,
            failure_reasons=[
                f"n_trades={n} < REVIVAL_CRITERIA_MIN_TRADES={settings.REVIVAL_CRITERIA_MIN_TRADES}"
            ],
            evaluated_at=now,
        )

    if cutover_override is not None:
        cutover_at, cutover_source = cutover_override, "operator_override"
    else:
        cutover_at, cutover_source = await find_latest_regime_cutover(db, signal_type)

    if cutover_at is None:
        return RevivalCriteriaResult(
            signal_type=signal_type,
            verdict=RevivalVerdict.STRATIFICATION_INFEASIBLE,
            n_trades=n, cutover_at=None,
            cutover_source=cutover_source, cutover_age_days=None,
            window_a=None, window_b=None,
            failure_reasons=[
                f"no regime cutover found in signal_params_audit for {signal_type}; "
                f"pass --cutover-iso to override"
            ],
            evaluated_at=now,
        )

    cutover_age_days = (now - cutover_at).days

    split = split_at_cutover_boundary(
        trades, cutover_at=cutover_at,
        min_window_days=settings.REVIVAL_CRITERIA_MIN_WINDOW_DAYS,
        min_window_trades=settings.REVIVAL_CRITERIA_MIN_WINDOW_TRADES,
    )
    if split is None:
        return RevivalCriteriaResult(
            signal_type=signal_type,
            verdict=RevivalVerdict.STRATIFICATION_INFEASIBLE,
            n_trades=n, cutover_at=cutover_at,
            cutover_source=cutover_source, cutover_age_days=cutover_age_days,
            window_a=None, window_b=None,
            failure_reasons=[
                f"cutover at {cutover_at.isoformat()} cannot split into two "
                f">= {settings.REVIVAL_CRITERIA_MIN_WINDOW_DAYS}d / "
                f">= {settings.REVIVAL_CRITERIA_MIN_WINDOW_TRADES}-trade windows"
            ],
            evaluated_at=now,
        )

    a_trades, b_trades = split
    a = _window_diagnostics(a_trades, settings=settings)
    b = _window_diagnostics(b_trades, settings=settings)
    failures = _evaluate_window_gates("a", a, settings) + _evaluate_window_gates("b", b, settings)
    verdict = RevivalVerdict.PASS if not failures else RevivalVerdict.FAIL
    return RevivalCriteriaResult(
        signal_type=signal_type,
        verdict=verdict, n_trades=n,
        cutover_at=cutover_at, cutover_source=cutover_source,
        cutover_age_days=cutover_age_days,
        window_a=a, window_b=b,
        failure_reasons=failures,
        evaluated_at=now,
    )
```

- [ ] **Step 4: Run → 5+ PASS**

- [ ] **Step 5: Commit**

```bash
git add scout/trading/revival_criteria.py tests/test_revival_criteria.py
git commit -m "feat(revival-criteria): orchestrator with cutover_age_days + Wilson + bootstrap gates"
```

---

### Task 11: CLI + SQL emitter (V3 — emit-sql-only, cool-off pre-print, override warning, contradiction surfacing, busy_timeout, keep_on_provisional_until verdict)

**Files:** Modify `scout/trading/revival_criteria.py`, Modify `tests/test_revival_criteria.py` (8 tests)

V3 folds applied:
- C#9/D#1: `--emit-sql-only` flag
- C#3: `PRAGMA busy_timeout=30000;` in emitted SQL
- C#5/D#4: `_parse_cutover_iso` argparse type validator + range check + OVERRIDE WARNING
- D#2: verdict string `keep_on_provisional_until_<expiry_iso>` not `keep_on_permanent`
- D#5: cool-off status pre-printed
- D#6: BELOW projection printed
- D#7: FAIL + existing keep contradiction surfaced
- C#2: cutover_age_days printed + WARN at >30d

- [ ] **Step 1: Write failing tests**

(8 tests covering: validate signal_type accept/reject, sql_escape, parse_cutover_iso valid+invalid, emit_sql_returns_none_on_fail, emit_sql_PASS_includes_keep_on_provisional_until_AND_busy_timeout_AND_NULL_old_value, _print_verdict_includes_cool_off_status, main_emit_sql_only_suppresses_prose.)

```python
def test_emit_sql_uses_keep_on_provisional_until_with_30d_expiry():
    diag = WindowDiagnostics(...)
    result = RevivalCriteriaResult(
        signal_type="losers_contrarian", verdict=RevivalVerdict.PASS,
        n_trades=120, cutover_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        cutover_source="signal_params_audit:auto_suspend:enabled",
        cutover_age_days=10,
        window_a=diag, window_b=diag, failure_reasons=[],
        evaluated_at=datetime(2026, 5, 17, tzinfo=timezone.utc),
    )
    settings = Settings(REVIVAL_CRITERIA_VERDICT_EXPIRY_DAYS=30)
    sql = _emit_soak_verdict_sql(result, operator="operator", settings=settings)
    assert sql is not None
    assert "BEGIN IMMEDIATE" in sql
    assert "PRAGMA busy_timeout=30000" in sql  # V3 fold per C#3
    assert "COMMIT" in sql
    assert "NULL" in sql                # old_value NULL
    assert "keep_on_provisional_until_2026-06-16" in sql  # V3 fold per D#2 (30d from evaluated_at)
    assert "BL-NEW-REVIVAL-COOLOFF" in sql  # operator note
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Write minimal implementation**

```python
# scout/trading/revival_criteria.py (append)
import argparse
import asyncio
import re as _re
import sys

import structlog

from scout.db import Database  # V3: documented in module docstring as deliberate
log = structlog.get_logger(__name__)

_SIGNAL_TYPE_RE = _re.compile(r"^[a-z_][a-z0-9_]*$")


def _validate_signal_type(s: str) -> None:
    if not _SIGNAL_TYPE_RE.match(s):
        raise ValueError(f"signal_type must match {_SIGNAL_TYPE_RE.pattern}; got={s!r}")


def _sql_escape(s: str) -> str:
    return s.replace("'", "''")


def _parse_cutover_iso(s: str) -> datetime:
    """V3 fold per C#14: argparse type validator with friendly error."""
    if not s:
        raise argparse.ArgumentTypeError("--cutover-iso cannot be empty")
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"--cutover-iso must be a valid ISO 8601 timestamp; got={s!r} ({e})"
        )


def _print_verdict(
    result: RevivalCriteriaResult, *,
    existing_keep: tuple[str, str] | None,
    cool_off_active_until: datetime | None,
    recent_trade_rate: float | None,
    settings: Settings,
) -> None:
    print(f"\n=== Revival criteria evaluation: {result.signal_type} ===")
    print(f"Evaluated at: {result.evaluated_at}")
    print(f"Total closed trades: {result.n_trades}")
    if result.cutover_at is not None:
        age = result.cutover_age_days
        warn = " WARN: stale cutover" if age is not None and age > 30 else ""
        print(f"Cutover: {result.cutover_at} ({age}d ago) [source: {result.cutover_source}]{warn}")
    else:
        print(f"Cutover: NOT FOUND [source: {result.cutover_source}]")
    # V3 fold per D#5: cool-off status BEFORE verdict
    if cool_off_active_until is not None:
        print(f">>> Cool-off status: ACTIVE until {cool_off_active_until.isoformat()}")
    else:
        print(">>> Cool-off status: CLEAR")
    print(f"Verdict: {result.verdict.value.upper()}")

    if result.failure_reasons:
        print("\nFailure reasons:")
        for r in result.failure_reasons:
            print(f"  - {r}")

    # V3 fold per D#6: BELOW projection
    if result.verdict is RevivalVerdict.BELOW_MIN_TRADES and recent_trade_rate is not None and recent_trade_rate > 0:
        needed = settings.REVIVAL_CRITERIA_MIN_TRADES - result.n_trades
        days = needed / recent_trade_rate
        print(f"\n>>> Estimated re-evaluable in ~{days:.1f} days "
              f"(need {needed} more trades; recent rate = {recent_trade_rate:.2f}/day)")
        print(f">>> Note: PASS additionally requires >= {settings.REVIVAL_CRITERIA_MIN_WINDOW_DAYS}d "
              f"AND >= {settings.REVIVAL_CRITERIA_MIN_WINDOW_TRADES} trades on BOTH sides of cutover.")

    # V3 fold per D#7: FAIL + existing keep contradiction
    if result.verdict is RevivalVerdict.FAIL and existing_keep is not None:
        keep_iso, keep_value = existing_keep
        print(f"\n>>> ATTENTION: existing soak_verdict={keep_value!r} at {keep_iso} "
              f"is CONTRADICTED by current FAIL.")
        print(">>> To revoke, run:")
        print(f"sqlite3 <db> \"INSERT INTO signal_params_audit"
              f"(signal_type, field_name, old_value, new_value, reason, applied_by, applied_at) "
              f"VALUES('{_sql_escape(result.signal_type)}', 'soak_verdict', "
              f"'{_sql_escape(keep_value)}', 'revoked', "
              f"'revoke: revival_criteria FAIL at {result.evaluated_at.isoformat()}', "
              f"'operator', '{result.evaluated_at.isoformat()}');\"")

    if result.window_a is not None and result.window_b is not None:
        for label, w in (("A", result.window_a), ("B", result.window_b)):
            print(f"\nWindow {label}: {w.start_at.date()} → {w.end_at.date()} (n={w.n})")
            print(f"  net=${w.net_pnl_usd:.2f}  per_trade=${w.per_trade_usd:.2f}  win%={w.win_pct:.1f}")
            print(f"  win_pct_wilson_lb={w.win_pct_wilson_lb:.1f}%  per_trade_bootstrap_lb=${w.per_trade_bootstrap_lb:.2f}")
            print(f"  no_breakout_and_loss_rate={w.no_breakout_and_loss_rate:.2f}")
            print(f"  stop_loss_frequency={w.stop_loss_frequency:.2f}")
            print(f"  expired_loss_frequency={w.expired_loss_frequency:.2f}")
            print(f"  exit_machinery_contribution={w.exit_machinery_contribution:.2f}")


def _emit_soak_verdict_sql(
    result: RevivalCriteriaResult, *, operator: str, settings: Settings
) -> str | None:
    if result.verdict is not RevivalVerdict.PASS:
        return None
    _validate_signal_type(result.signal_type)
    _validate_signal_type(operator)
    sig = _sql_escape(result.signal_type)
    op = _sql_escape(operator)
    # V3 fold per D#2: time-boxed verdict
    expiry_at = result.evaluated_at + timedelta(days=settings.REVIVAL_CRITERIA_VERDICT_EXPIRY_DAYS)
    verdict_str = f"keep_on_provisional_until_{expiry_at.isoformat()}"
    reason = _sql_escape(
        f"PASS: n={result.n_trades}, cutover={result.cutover_at.isoformat()} ({result.cutover_age_days}d ago), "
        f"source={result.cutover_source}, "
        f"window_a per_trade=${result.window_a.per_trade_usd:.2f} bootstrap_lb=${result.window_a.per_trade_bootstrap_lb:.2f}, "
        f"window_b per_trade=${result.window_b.per_trade_usd:.2f} bootstrap_lb=${result.window_b.per_trade_bootstrap_lb:.2f}"
    )
    ts = _sql_escape(result.evaluated_at.isoformat())
    verdict_value = _sql_escape(verdict_str)
    return (
        f"-- Generated by scout.trading.revival_criteria at {result.evaluated_at.isoformat()}\n"
        f"-- NOTE: PASS does not bypass BL-NEW-REVIVAL-COOLOFF; check cool-off\n"
        f"--   before calling Database.revive_signal_with_baseline.\n"
        f"-- VERDICT EXPIRY: {expiry_at.isoformat()} ({settings.REVIVAL_CRITERIA_VERDICT_EXPIRY_DAYS}d)\n"
        f"PRAGMA busy_timeout=30000;\n"
        f"BEGIN IMMEDIATE;\n"
        f"INSERT INTO signal_params_audit\n"
        f"  (signal_type, field_name, old_value, new_value, reason, applied_by, applied_at)\n"
        f"VALUES\n"
        f"  ('{sig}', 'soak_verdict', NULL, '{verdict_value}',\n"
        f"   '{reason}',\n"
        f"   '{op}', '{ts}');\n"
        f"COMMIT;\n"
    )


async def _query_cool_off_status(
    db, signal_type: str, settings: Settings
) -> datetime | None:
    """Return the timestamp until which cool-off is active, or None if cleared."""
    if db._conn is None:
        return None
    cur = await db._conn.execute(
        "SELECT applied_at FROM signal_params_audit "
        "WHERE signal_type = ? AND field_name = 'enabled' "
        "AND old_value = '0' AND new_value = '1' AND applied_by = 'operator' "
        "ORDER BY applied_at DESC LIMIT 1",
        (signal_type,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    last_revival = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
    expires = last_revival + timedelta(days=settings.SIGNAL_REVIVAL_MIN_SOAK_DAYS)
    if expires > datetime.now(timezone.utc):
        return expires
    return None


async def _main_async(args: argparse.Namespace) -> int:
    from scout.config import get_settings
    settings = get_settings()
    db = Database(args.db)
    await db.connect()
    try:
        # V3 fold per C#10: typo protection
        if not await signal_type_exists(db, args.signal_type):
            print(f"ERROR: signal_type={args.signal_type!r} not found in signal_params table",
                  file=sys.stderr)
            return 3

        result = await evaluate_revival_criteria(
            db, args.signal_type, settings,
            cutover_override=args.cutover_iso,
        )

        # V3 fold per C#5: validate override is within trade timeline
        if args.cutover_iso is not None and result.verdict is RevivalVerdict.STRATIFICATION_INFEASIBLE:
            # Likely cause: override outside trade range. Provide better diagnostic.
            trades = await fetch_closed_trades(db, args.signal_type)
            if trades:
                tmin, tmax = trades[0].closed_at, trades[-1].closed_at
                if args.cutover_iso < tmin or args.cutover_iso > tmax:
                    print(f"WARN: --cutover-iso {args.cutover_iso.isoformat()} is outside "
                          f"trade range [{tmin.isoformat()}, {tmax.isoformat()}]",
                          file=sys.stderr)

        # V3 fold per D#4: OVERRIDE WARNING
        if args.cutover_iso is not None:
            audit_cutover, _ = await find_latest_regime_cutover(db, args.signal_type)
            if audit_cutover is not None and audit_cutover != args.cutover_iso:
                delta = (args.cutover_iso - audit_cutover).days
                print(f">>> OVERRIDE WARNING: audit-derived cutover was {audit_cutover.isoformat()}; "
                      f"operator override is {args.cutover_iso.isoformat()} (delta={delta}d)",
                      file=sys.stderr)

        existing_keep = await find_existing_keep_verdict(db, args.signal_type)
        cool_off_until = await _query_cool_off_status(db, args.signal_type, settings)
        recent_rate = await compute_recent_trade_rate(db, args.signal_type)

    finally:
        await db.close()

    sql = _emit_soak_verdict_sql(result, operator=args.operator, settings=settings)

    # V3 fold per C#9/D#1: emit-sql-only mode
    if args.emit_sql_only:
        if sql is not None:
            print(sql)
    else:
        _print_verdict(
            result, existing_keep=existing_keep,
            cool_off_active_until=cool_off_until,
            recent_trade_rate=recent_rate, settings=settings,
        )
        if sql is not None:
            print("\n--- Operator may paste the following SQL to write the audit row ---")
            print(sql)

    log.info(
        "revival_criteria_evaluated",
        signal_type=args.signal_type,
        verdict=result.verdict.value,
        n_trades=result.n_trades,
        cutover_source=result.cutover_source,
        cutover_age_days=result.cutover_age_days,
        cutover_override_used=args.cutover_iso is not None,
        failures=result.failure_reasons,
    )

    if result.verdict is RevivalVerdict.PASS:
        return 0
    if result.verdict is RevivalVerdict.FAIL:
        return 1
    return 2


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="revival_criteria")
    p.add_argument("signal_type", help="signal_type to evaluate")
    p.add_argument("--db", default="scout.db", help="path to scout.db")
    p.add_argument("--operator", default="operator",
                   help="applied_by value for emitted SQL")
    p.add_argument("--cutover-iso", default=None, type=_parse_cutover_iso,
                   help="explicit cutover ISO timestamp; overrides audit-derived cutover")
    p.add_argument("--emit-sql-only", action="store_true",
                   help="suppress diagnostic prose; print SQL only (redirect-pipeable)")
    args = p.parse_args(argv)
    _validate_signal_type(args.signal_type)
    _validate_signal_type(args.operator)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run → 8 PASS**

- [ ] **Step 5: Commit**

```bash
git add scout/trading/revival_criteria.py tests/test_revival_criteria.py
git commit -m "feat(revival-criteria): CLI v3 — emit-sql-only + cool-off pre-print + override warning + contradiction surfacing"
```

---

### Task 12: Full-suite regression verification

(Same as v2 plan Task 12. Run `uv run pytest tests/test_revival_criteria.py tests/test_config.py -v` and `uv run pytest tests/ -v -k "trading or signal_params or auto_suspend or revive or revival"`.)

---

### Task 13: Findings doc (V3 — DO-NOT-ACT warning per D#9)

(Same as v2 plan Task 13 with one addition: the findings doc MUST prepend a `>>> WARNING <<<` block stating "Gainers_early evaluation in this doc is FALSIFICATION-RISK ANALYSIS ONLY. Do NOT use this output to drive gainers_early state changes without re-running the evaluator against current data.")

---

### Task 14: Backlog + todo + memory + follow-up filing (post-merge)

**Files:**
- Modify: `backlog.md` (flip status PROPOSED → SHIPPED with PR link; FILE NEW: `BL-NEW-REVIVAL-VERDICT-WATCHDOG` and `BL-NEW-REVIVAL-CRITERIA-QUARTERLY-RECALIBRATION` and `BL-NEW-EVALUATION-HISTORY-PERSISTENCE` per D#2/D#10/C#16)
- Modify: `tasks/todo.md` (flip board item)
- Create: memory checkpoint via Write tool

This task does NOT run during build; runs in P7 after PR merge.

---

## Self-review checklist

- [ ] Spec coverage — every scope item from `backlog.md:1595` covered (with item 5 "post-verdict monitoring" implemented as `keep_on_provisional_until_<iso>` + follow-up watchdog backlog).
- [ ] CLAUDE.md plan-doc gate — `**New primitives introduced:**` header present.
- [ ] No live-config-flip risk — confirmed.
- [ ] CLAUDE.md §11b compliance — Wilson LB + bootstrap LB primary gates (Tasks 5, 6, 10).
- [ ] Reviewer A folds (plan-stage) — all 11 findings addressed in v2; preserved in v3.
- [ ] Reviewer B folds (plan-stage) — all addressed in v2; preserved in v3.
- [ ] Reviewer C folds (design-stage structural) — #1 denylist, #2 cutover_age_days, #3 busy_timeout, #5 cutover_iso range check, #6 operator-revival skip, #9 emit-sql-only, #10 signal_type_exists, #14 parse_cutover_iso, #15 documented private access.
- [ ] Reviewer D folds (design-stage strategy/safety) — #1 emit-sql-only, #2 keep_on_provisional_until + watchdog backlog, #3 PROVISIONAL flag in Settings, #4 OVERRIDE WARNING, #5 cool-off pre-print, #6 BELOW projection, #7 FAIL contradiction surface, #8 cool-off status on PASS, #9 gainers_early WARNING, #10 quarterly recalibration backlog.

## Out of scope (follow-up backlog candidates filed at Task 14)

- `BL-NEW-REVIVAL-VERDICT-WATCHDOG` — active enforcement of `keep_on_provisional_until_<iso>` expiry
- `BL-NEW-REVIVAL-CRITERIA-QUARTERLY-RECALIBRATION` — periodic re-derivation of Task 0 baselines
- `BL-NEW-EVALUATION-HISTORY-PERSISTENCE` — persist evaluator runs (not just structlog) for forensic review
- `BL-NEW-REVIVAL-CRITERIA-PER-SIGNAL-TUNING` — per-signal Settings overrides
