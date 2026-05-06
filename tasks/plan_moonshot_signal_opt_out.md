**New primitives introduced:** `signal_params.moonshot_enabled INTEGER NOT NULL DEFAULT 1` column (per-signal opt-OUT flag — when `0`, the moonshot regime's `effective_trail_pct = max(PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT, sp.trail_pct)` floor is bypassed and per-signal `sp.trail_pct` is used directly), `_migrate_moonshot_opt_out_column` migration in `scout/db.py` (BEGIN EXCLUSIVE wrapped, paper_migrations + schema_version 20260507 stamped), new SignalParams field `moonshot_enabled: bool` exposed via `scout/trading/params.py`, evaluator branch in `scout/trading/evaluator.py:463-474` that consults `sp.moonshot_enabled` before applying the floor.

# Plan: BL-NEW-MOONSHOT-OPT-OUT — per-signal moonshot floor escape hatch

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Per-signal exit-policy override | none found — Hermes index covers agent-orchestration / retrieval / browser, not paper-trading exit-engine | build from scratch |
| SQLite migration with PRAGMA-guarded ALTER + paper_migrations stamp | covered by canonical `_migrate_high_peak_fade_columns_and_audit_table` pattern | use existing internal pattern |
| Pydantic-style per-signal flag in dataclass | covered by existing `SignalParams.high_peak_fade_enabled` shape (PR #78) | mirror existing internal pattern |

**Awesome-hermes-agent ecosystem check:** none relevant.

**Drift-check (per global CLAUDE.md §7a):**
- `grep -rn "moonshot_enabled\|moonshot_opt" scout/ tests/` → 0 hits
- `grep -rn "moonshot_trail_drawdown_pct.*column\|moonshot.*signal_params" scout/ tests/` → 0 hits
- `tasks/findings_moonshot_floor_nullification.md` exists (106 lines, surfaced 2026-05-05) and §4 lists three candidate fixes; (b) `moonshot_enabled` opt-out is the chosen one per operator's "default" in this autonomous overnight task.

**Verdict:** building from scratch, single migration + 1 evaluator branch + 1 dataclass field.

---

## Goal

Resolve the structural finding documented in `tasks/findings_moonshot_floor_nullification.md`: the moonshot regime (peak ≥ 40%) silently dominates per-signal `trail_pct` via `max(PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT, sp.trail_pct)` at `scout/trading/evaluator.py:471-474`. Calibrate.py writes per-signal `trail_pct` values that have NO EFFECT in the moonshot regime — the floor wins.

The findings doc lists 3 candidate fixes (§4). Operator chose **(b) `moonshot_enabled` per-signal opt-OUT flag** as the simplest path: signals can choose to NOT participate in the moonshot regime at all, in which case their per-signal `trail_pct` controls the trail at all peak levels (subject only to the `low_peak_threshold` branch already in place).

**Default behavior unchanged:** `moonshot_enabled` defaults to `1` for all existing rows (current behavior). Operators opt OUT per-signal via SQL UPDATE when calibration data shows the signal benefits from a tighter or wider trail than the moonshot floor allows.

## Scope explicitly OUT

- Candidate (a) `moonshot_trail_drawdown_pct REAL` column — DEFERRED to a future BL-NEW-MOONSHOT-PER-SIGNAL-FLOOR if (b)'s opt-out turns out too coarse.
- Candidate (c) callable/strategy-object refactor — REJECTED as over-engineering.
- BL-067 conviction-lock interaction: the additive overlay `conviction_locked_params` widens `sp.trail_pct` upward (capped at 35%). When `moonshot_enabled=0`, the conviction-lock overlay still applies — the moonshot floor was never the only path that could widen `sp.trail_pct`. Verified per findings doc §3.2.
- Backfill of historical trades: open trades currently in moonshot regime keep their armed-time floor. New behavior applies forward only. (Migrations don't touch open trades; evaluator reads the column on each evaluator pass.)

## CRITICAL deploy constraint

Three active soaks end 2026-05-13 (losers_contrarian + gainers_early revivals + HPF dry-run). This PR changes evaluator behavior for any signal that opts out, and may indirectly affect signals that don't (if a future operator action stamps `moonshot_enabled=0` on a soaking signal). **Per operator's guidance, MERGE this PR but DO NOT DEPLOY it until 2026-05-13 soak review.** PR description must call this out so the post-merge deployer doesn't ship eagerly.

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `scout/db.py` | Modify | Add `_migrate_moonshot_opt_out_column` migration |
| `scout/trading/params.py` | Modify | Add `moonshot_enabled: bool` to SignalParams dataclass + read column |
| `scout/trading/evaluator.py` | Modify | Branch in moonshot regime: skip `max(...)` floor when `sp.moonshot_enabled=False` |
| `tests/test_moonshot_exit.py` | Extend | New tests for opt-out: trail behavior under opt-out, default opt-in unchanged, conviction-lock interaction |
| `tests/test_signal_params.py` (or test_db.py) | Extend | Migration tests |

## Tasks

### Task 1: Failing tests for opt-out semantics

Append to `tests/test_moonshot_exit.py`:

```python
async def test_moonshot_floor_applies_by_default(tmp_path, settings_factory):
    """moonshot_enabled=1 (default) — floor applies as today: 
    effective_trail = max(MOONSHOT_TRAIL_DRAWDOWN_PCT, sp.trail_pct)."""
    # ... setup signal with sp.trail_pct=15 (tighter than 30 floor)
    # peak >= 40 (moonshot armed)
    # expect: trail floor of 30 wins → trade closes when retrace >= 30%, not 15%

async def test_moonshot_floor_skipped_when_opted_out(tmp_path, settings_factory):
    """moonshot_enabled=0 — floor bypassed; per-signal sp.trail_pct controls."""
    # UPDATE signal_params SET moonshot_enabled=0 WHERE signal_type='X'
    # peak >= 40, sp.trail_pct=15
    # expect: trade closes when retrace >= 15%, not 30%

async def test_moonshot_opt_out_low_peak_path_unchanged(tmp_path, settings_factory):
    """Opt-out only affects moonshot regime. peak < low_peak_threshold uses
    sp.trail_pct_low_peak as before (independent path)."""

async def test_moonshot_opt_out_pre_moonshot_path_unchanged(tmp_path, settings_factory):
    """20 <= peak < 40 — uses sp.trail_pct directly regardless of moonshot_enabled."""

async def test_moonshot_opt_out_with_conviction_lock(tmp_path, settings_factory):
    """When BL-067 conviction-lock has overlaid sp.trail_pct upward (e.g. to 35%)
    AND moonshot_enabled=0, the locked-wider trail should still apply (the floor
    was just a lower bound; the lock raises sp.trail_pct directly). Verify
    effective_trail_pct = sp.trail_pct (35) in moonshot regime."""

async def test_signal_params_has_moonshot_enabled_column(tmp_path):
    """Migration adds moonshot_enabled INTEGER NOT NULL DEFAULT 1."""

async def test_moonshot_enabled_defaults_to_1_for_all_seed_signals(tmp_path):
    """Existing rows after migration default to enabled=1 (no behavior change)."""
```

### Task 2: Schema migration

Mirror `_migrate_high_peak_fade_columns_and_audit_table`:

```python
async def _migrate_moonshot_opt_out_column(self) -> None:
    """BL-NEW-MOONSHOT-OPT-OUT: per-signal moonshot regime opt-out."""
    # ... PRAGMA-guarded ALTER, BEGIN EXCLUSIVE, paper_migrations stamp
    # ... paper_migrations marker name: bl_moonshot_opt_out_v1 (pinned —
    # do NOT change post-deploy; see plan-stage structural reviewer
    # Issue 3 for rationale)
    # ... schema_version 20260507
    # Column: moonshot_enabled INTEGER NOT NULL DEFAULT 1
```

### Task 3: SignalParams dataclass + read

In `scout/trading/params.py`:

```python
@dataclass(frozen=True)
class SignalParams:
    # ... existing fields ...
    moonshot_enabled: bool = True  # BL-NEW-MOONSHOT-OPT-OUT — default opt-in

# In _load_from_row (or wherever):
moonshot_enabled=bool(row["moonshot_enabled"]),
```

Update SELECT to include `moonshot_enabled`.

### Task 4: Evaluator branch

`scout/trading/evaluator.py:471-474`:

```python
if moonshot_armed_at is not None:
    if not sp.moonshot_enabled:
        # BL-NEW-MOONSHOT-OPT-OUT: signal opted out of moonshot regime.
        # Use per-signal trail_pct directly without the global floor.
        # See tasks/findings_moonshot_floor_nullification.md and
        # tasks/plan_moonshot_signal_opt_out.md for the rationale.
        effective_trail_pct = sp.trail_pct
    else:
        effective_trail_pct = max(
            settings.PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT,
            sp.trail_pct,
        )
elif peak_pct is not None and peak_pct < sp.low_peak_threshold_pct:
    effective_trail_pct = sp.trail_pct_low_peak
else:
    effective_trail_pct = sp.trail_pct
```

### Task 5: Run regression

```bash
uv run pytest tests/test_moonshot_exit.py tests/test_signal_params_auto_suspend.py tests/test_high_peak_fade.py tests/test_db.py -q
```

### Task 6: Black + commit

### Task 7: PR + 3 reviewers + fix + merge — HOLD DEPLOY

PR description MUST call out: "DO NOT DEPLOY UNTIL 2026-05-13 SOAK REVIEW. Affects evaluator behavior for any signal where moonshot_enabled is flipped to 0; default opt-in preserves current behavior."

3-vector reviewer dispatch (statistical / code / strategy).

---

## Done criteria

- All new tests pass
- Existing moonshot tests unchanged
- Migration runs cleanly
- PR merged to master via squash
- Deploy held until 2026-05-13 soak review (PR body explicit)
- Memory entry recording the opt-out semantics + when to use it
- todo.md closes BL-NEW-MOONSHOT-OPT-OUT (or promotes the deferred candidate (a) per-signal-floor as the next step)
