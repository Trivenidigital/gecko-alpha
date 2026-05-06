**New primitives introduced:** `SIGNAL_REVIVAL_MIN_SOAK_DAYS: int = 7` Pydantic Settings field with bounded validator (must be >= 0), `force: bool = False` keyword argument on `Database.revive_signal_with_baseline()`, ValueError raise path with structured error message that names the offending signal_type + last revival timestamp + bypass-kwarg hint, `signal_params_audit`-based revival-history query (filter `field_name='enabled' AND old_value='0' AND new_value='1' AND applied_by='operator'` — POSITIVE filter, per plan-stage reviewer MUST-FIX, to prevent future calibrate / dashboard / other applied_by values from accidentally triggering the cool-off), structlog WARNING event `revive_signal_force_bypass` emitted when `force=True` overrides the cool-off (per plan-stage reviewer RECOMMEND, gives operator dashboards an aggregable signal of bypass usage independent of audit-row inspection).

# Plan: BL-NEW-REVIVAL-COOLOFF — operator-revival cool-off

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Per-signal operator-action rate-limiting | none found — Hermes index covers agent orchestration / retrieval / browser automation, not internal-rule rate-limiting | build from scratch |
| SQLite audit-row query for "last operator action of type X" | covered by existing `signal_params_audit` table + idiomatic `SELECT ... ORDER BY applied_at DESC LIMIT 1` pattern (see `scout/trading/auto_suspend.py:174-180`) | use existing internal pattern |
| Pydantic Settings field with bounded validator | covered by existing pattern in `scout/config.py` (e.g. `PAPER_HIGH_PEAK_FADE_RETRACE_PCT` validator) | use existing internal pattern |

**Awesome-hermes-agent ecosystem check:** none relevant.

**Drift-check (per global CLAUDE.md §7a):**
- `grep -rn "SIGNAL_REVIVAL_MIN_SOAK_DAYS\|revival_min_soak\|revival_cooloff" scout/ tests/` → 0 hits
- `grep -rn "force.*revive\|bypass.*revive" scout/ tests/` → 0 hits
- No prior PR mentions BL-NEW-REVIVAL-COOLOFF

**Verdict:** building from scratch — pure internal rule layer over existing audit table.

---

## Goal

Add a cool-off window between consecutive operator revivals of the same signal so the `drawdown_baseline_at` knob (added in PR #79) can't be repeatedly stamped to immortalize a structurally-broken signal.

This was the **strategy reviewer's RECOMMEND** on PR #79 (BL-NEW-AUTOSUSPEND-FIX), deferred at merge time pending paper-trade-period prioritization. Operator + audit trail were stated as interim mitigation.

The threat model: an operator (or automation) calls `revive_signal_with_baseline()` repeatedly each time `auto_suspend` re-fires, effectively bypassing the suspension rule. Today this is gated by operator self-discipline + visible audit rows; the cool-off makes it mechanically enforced.

## Scope

**IN scope:**
- New `SIGNAL_REVIVAL_MIN_SOAK_DAYS: int = 7` Settings field
- `Database.revive_signal_with_baseline(signal_type, *, reason, operator='operator', force=False)` — new `force` kwarg
- Pre-write check: query last operator-issued revival audit row; if `< SIGNAL_REVIVAL_MIN_SOAK_DAYS` days ago, raise `ValueError` with structured message naming signal_type + last revival timestamp + days remaining + bypass hint
- The audit-row filter must isolate OPERATOR revivals (not auto_suspend's enabled→0 rows). Filter: `field_name='enabled' AND old_value='0' AND new_value='1' AND applied_by = 'operator'`.
- `force=True` bypasses the cool-off and writes an audit reason that explicitly notes the bypass

**OUT of scope:**
- Changing the cool-off duration default in production. 7 days is a pragmatic starting point; revisit if operator hits the wall.
- Auto-extending cool-off based on signal performance (e.g., longer cool-off for repeat offenders). Future BL-NEW-COOLOFF-ADAPTIVE.
- Cool-off across signal_type boundaries. Each signal independent.
- Notification on bypass (`force=True`) — audit row is the record of truth.
- Cool-off on the FIRST revival of a never-revived signal. The audit row must EXIST for cool-off to fire; absence = first revival = always allowed.

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `scout/config.py` | Modify | Add `SIGNAL_REVIVAL_MIN_SOAK_DAYS: int = 7` + bounded `_validate_revival_min_soak_days` validator (>= 0) |
| `scout/db.py` | Modify | Extend `revive_signal_with_baseline`: query last revival, compute delta, raise ValueError if cool-off violated and `not force` |
| `tests/test_signal_params_auto_suspend.py` | Extend | Add 6 tests covering: first revival never blocks, second within window blocks, second after window allows, force=True bypasses, force=True writes bypass-marker reason, cross-signal independence |

No changes to: existing tests (regression-clean), production callers (current operator scripts pass no `force` kwarg → default False → cool-off active).

## Tasks

### Task 1: Failing tests for cool-off semantics

Append to `tests/test_signal_params_auto_suspend.py`:

```python
async def test_revive_first_time_never_blocks(tmp_path, settings_factory):
    """Signal with NO prior revival audit row — must succeed regardless of cool-off."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Pre-suspend (no operator revival history)
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0, suspended_reason='auto_suspend' "
        "WHERE signal_type='gainers_early'"
    )
    await db._conn.commit()
    # First revival ever — no cool-off applies
    await db.revive_signal_with_baseline(
        "gainers_early", reason="first revival",
    )
    cur = await db._conn.execute(
        "SELECT enabled FROM signal_params WHERE signal_type='gainers_early'"
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()


async def test_revive_within_cooloff_window_raises(tmp_path, settings_factory):
    """Second revival within SIGNAL_REVIVAL_MIN_SOAK_DAYS must raise ValueError."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0 WHERE signal_type='gainers_early'"
    )
    await db._conn.commit()
    await db.revive_signal_with_baseline("gainers_early", reason="first")
    # Re-suspend (simulate auto_suspend re-firing)
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0, suspended_reason='auto_suspend' "
        "WHERE signal_type='gainers_early'"
    )
    await db._conn.commit()
    with pytest.raises(ValueError, match="cool-off|cooloff"):
        await db.revive_signal_with_baseline(
            "gainers_early", reason="second within window"
        )
    await db.close()


async def test_revive_after_cooloff_window_allows(tmp_path, settings_factory):
    """Backdate the prior revival audit row > 7 days; second revival succeeds."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0 WHERE signal_type='gainers_early'"
    )
    await db._conn.commit()
    await db.revive_signal_with_baseline("gainers_early", reason="first")
    # Backdate the audit row 8 days ago
    eight_days_ago = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    await db._conn.execute(
        "UPDATE signal_params_audit SET applied_at=? "
        "WHERE signal_type='gainers_early' AND applied_by='operator'",
        (eight_days_ago,),
    )
    # Re-suspend
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0, suspended_reason='auto_suspend' "
        "WHERE signal_type='gainers_early'"
    )
    await db._conn.commit()
    # Should succeed — past cool-off
    await db.revive_signal_with_baseline(
        "gainers_early", reason="second after window"
    )
    cur = await db._conn.execute(
        "SELECT enabled FROM signal_params WHERE signal_type='gainers_early'"
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()


async def test_revive_force_true_bypasses_cooloff(tmp_path, settings_factory):
    """force=True must bypass the cool-off check."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0 WHERE signal_type='gainers_early'"
    )
    await db._conn.commit()
    await db.revive_signal_with_baseline("gainers_early", reason="first")
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0 WHERE signal_type='gainers_early'"
    )
    await db._conn.commit()
    # Should succeed even though within window
    await db.revive_signal_with_baseline(
        "gainers_early", reason="emergency override", force=True
    )
    cur = await db._conn.execute(
        "SELECT enabled FROM signal_params WHERE signal_type='gainers_early'"
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()


async def test_revive_force_true_audit_marks_bypass(tmp_path, settings_factory):
    """The bypass revival's audit row must contain a marker so operators
    reading history can see the cool-off was overridden."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0 WHERE signal_type='gainers_early'"
    )
    await db._conn.commit()
    await db.revive_signal_with_baseline("gainers_early", reason="first")
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0 WHERE signal_type='gainers_early'"
    )
    await db._conn.commit()
    await db.revive_signal_with_baseline(
        "gainers_early", reason="emergency override", force=True
    )
    cur = await db._conn.execute(
        "SELECT reason FROM signal_params_audit WHERE signal_type='gainers_early' "
        "AND applied_by='operator' ORDER BY applied_at DESC LIMIT 1"
    )
    reason = (await cur.fetchone())[0]
    assert "force" in reason.lower() or "bypass" in reason.lower(), (
        f"force=True audit must mark bypass; got: {reason}"
    )
    await db.close()


async def test_revive_cooloff_independent_per_signal(tmp_path, settings_factory):
    """Reviving signal A within window does NOT block reviving signal B."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute(
        "UPDATE signal_params SET enabled=0 "
        "WHERE signal_type IN ('gainers_early', 'losers_contrarian')"
    )
    await db._conn.commit()
    await db.revive_signal_with_baseline("gainers_early", reason="first GE")
    # Should succeed despite gainers_early being within cool-off
    await db.revive_signal_with_baseline("losers_contrarian", reason="first LC")
    cur = await db._conn.execute(
        "SELECT signal_type, enabled FROM signal_params "
        "WHERE signal_type IN ('gainers_early', 'losers_contrarian') ORDER BY signal_type"
    )
    rows = await cur.fetchall()
    assert all(r[1] == 1 for r in rows)
    await db.close()
```

### Task 2: Settings field + validator

In `scout/config.py`, add near the existing `SIGNAL_SUSPEND_*` block:

```python
SIGNAL_REVIVAL_MIN_SOAK_DAYS: int = 7
"""BL-NEW-REVIVAL-COOLOFF: minimum days between consecutive operator
revivals of the same signal_type via revive_signal_with_baseline.
0 disables the check. Bypass with force=True per call."""
```

And the validator:

```python
@field_validator("SIGNAL_REVIVAL_MIN_SOAK_DAYS")
@classmethod
def _validate_revival_min_soak_days(cls, v: int) -> int:
    if v < 0:
        raise ValueError(
            f"SIGNAL_REVIVAL_MIN_SOAK_DAYS must be >= 0; got={v}"
        )
    return v
```

### Task 3: Modify revive_signal_with_baseline

Update signature + add cool-off check before BEGIN EXCLUSIVE:

```python
async def revive_signal_with_baseline(
    self,
    signal_type: str,
    *,
    reason: str,
    operator: str = "operator",
    force: bool = False,
) -> None:
    """Atomic operator revival ... [existing docstring]
    
    BL-NEW-REVIVAL-COOLOFF: enforces a SIGNAL_REVIVAL_MIN_SOAK_DAYS cool-off
    between consecutive operator revivals of the same signal. Set force=True
    for emergency override; the audit row will reflect the bypass.
    """
    if self._conn is None:
        raise RuntimeError("Database not initialized.")
    conn = self._conn

    if not force:
        # Check last operator revival audit row (filter by enabled 0→1
        # operator path; ignore auto_suspend's 1→0 rows).
        cur = await conn.execute(
            """SELECT applied_at FROM signal_params_audit
               WHERE signal_type = ?
                 AND field_name = 'enabled'
                 AND old_value = '0'
                 AND new_value = '1'
                 AND applied_by = 'operator'
               ORDER BY applied_at DESC LIMIT 1""",
            (signal_type,),
        )
        row = await cur.fetchone()
        if row is not None:
            from scout.config import get_settings
            settings = get_settings()
            cool_off_days = getattr(
                settings, "SIGNAL_REVIVAL_MIN_SOAK_DAYS", 7
            )
            if cool_off_days > 0:
                last_at = datetime.fromisoformat(row[0])
                delta = datetime.now(timezone.utc) - last_at
                if delta < timedelta(days=cool_off_days):
                    days_remaining = cool_off_days - delta.days
                    raise ValueError(
                        f"revive_signal_with_baseline cool-off: "
                        f"{signal_type} was last revived at {row[0]} "
                        f"({delta.days} days ago); minimum {cool_off_days} "
                        f"days required between consecutive revivals "
                        f"({days_remaining} days remaining). "
                        f"Pass force=True to bypass."
                    )

    # ... existing BEGIN EXCLUSIVE flow continues, with reason tweak when force=True

    audit_reason = reason
    if force:
        audit_reason = f"{reason} [force=True bypass of revival cool-off]"
    
    # ... rest unchanged, except the audit INSERT uses audit_reason
```

### Task 4: Run regression

```bash
uv run pytest tests/test_signal_params_auto_suspend.py -v
```

All 21 existing + 6 new = 27 tests must pass.

### Task 5: Black + commit

### Task 6: PR + 3 reviewers (statistical / code / strategy) + fix + merge + deploy

---

## Done criteria

- 27 tests pass (21 existing + 6 new)
- black --check clean
- PR merged
- Deployed to VPS, smoke-test that existing `revive_signal_with_baseline` calls (operator scripts) still work — they should, since they don't pass `force` and the cool-off only fires after a recent prior revival.
- Memory entry recording the cool-off semantics.
- todo.md closes BL-NEW-REVIVAL-COOLOFF item.
