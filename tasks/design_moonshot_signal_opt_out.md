**New primitives introduced:** Same as `plan_moonshot_signal_opt_out.md` — see plan for the full list. This design doc derives the interface contracts + migration shape + evaluator branch from the plan, with all plan-stage reviewer fixes applied.

# Design: BL-NEW-MOONSHOT-OPT-OUT — interface contracts

Companion to `tasks/plan_moonshot_signal_opt_out.md`. Plan-stage reviewers (policy + structural) caught:

**MUST-FIX (structural reviewer, conf 88+85):**
1. Pin `paper_migrations` marker name to literal `bl_moonshot_opt_out_v1` (no "or similar" hedge)
2. `moonshot_enabled: bool` dataclass field MUST declare `= True` default to avoid latent `TypeError` in `_settings_params()` Settings-fallback path

**RECOMMEND (both reviewers):**
1. Code comment in evaluator opt-out branch documenting that `sp.trail_pct` reflects any conviction-lock overlay applied earlier in the cascade
2. Migration idempotency test (re-run on already-migrated DB returns cleanly)
3. PR description names the 3 currently-soaking signals (losers_contrarian, gainers_early, HPF)

All folded into this design.

## Migration

```python
async def _migrate_moonshot_opt_out_column(self) -> None:
    """BL-NEW-MOONSHOT-OPT-OUT: per-signal moonshot regime opt-out flag.

    Adds:
      - signal_params.moonshot_enabled INTEGER NOT NULL DEFAULT 1

    When 0, the evaluator skips the
    ``max(PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT, sp.trail_pct)`` floor in
    moonshot regime (peak >= 40%) and uses ``sp.trail_pct`` directly.
    Default 1 preserves current behavior for all existing rows.

    Wrapped in BEGIN EXCLUSIVE / ROLLBACK + paper_migrations cutover row
    + schema_version stamp, matching the BL-NEW-HPF migration pattern.
    Idempotent: column-add is guarded by PRAGMA existence-check.
    """
    import structlog

    _log = structlog.get_logger()
    if self._conn is None:
        raise RuntimeError("Database not initialized.")
    conn = self._conn
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        await conn.execute("BEGIN EXCLUSIVE")

        await conn.execute(
            """CREATE TABLE IF NOT EXISTS paper_migrations (
                name TEXT PRIMARY KEY,
                cutover_ts TEXT NOT NULL
            )"""
        )
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_version (
                version    INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL,
                description TEXT NOT NULL
            )"""
        )

        cur_pragma = await conn.execute("PRAGMA table_info(signal_params)")
        existing_cols = {row[1] for row in await cur_pragma.fetchall()}
        if "moonshot_enabled" not in existing_cols:
            await conn.execute(
                "ALTER TABLE signal_params "
                "ADD COLUMN moonshot_enabled INTEGER NOT NULL DEFAULT 1"
            )

        await conn.execute(
            "INSERT OR IGNORE INTO paper_migrations (name, cutover_ts) "
            "VALUES (?, ?)",
            ("bl_moonshot_opt_out_v1", now_iso),  # PINNED marker name
        )
        await conn.execute(
            "INSERT OR IGNORE INTO schema_version "
            "(version, applied_at, description) VALUES (?, ?, ?)",
            (20260507, now_iso, "bl_moonshot_opt_out_v1"),
        )
        await conn.commit()
    except Exception:
        try:
            await conn.execute("ROLLBACK")
        except Exception as rb_err:
            _log.exception("schema_migration_rollback_failed", err=str(rb_err))
        _log.error("SCHEMA_DRIFT_DETECTED", migration="bl_moonshot_opt_out_v1")
        raise

    cur = await conn.execute(
        "SELECT 1 FROM paper_migrations WHERE name = ?",
        ("bl_moonshot_opt_out_v1",),
    )
    if (await cur.fetchone()) is None:
        raise RuntimeError(
            "bl_moonshot_opt_out_v1 cutover row missing after migration"
        )
```

## SignalParams field

In `scout/trading/params.py`:

```python
@dataclass(frozen=True)
class SignalParams:
    # ... existing fields ...
    conviction_lock_enabled: bool = False
    high_peak_fade_enabled: bool = False
    moonshot_enabled: bool = True  # BL-NEW-MOONSHOT-OPT-OUT — default opt-in
```

The `= True` default is **load-bearing** — without it, `_settings_params()` (the Settings-fallback path used when `SIGNAL_PARAMS_ENABLED=False` or rows are missing) raises `TypeError` because the frozen dataclass instantiation lacks a positional value (per plan-stage structural reviewer MUST-FIX).

In the row-reader, append after the `high_peak_fade_enabled` mapping:

```python
moonshot_enabled=bool(row["moonshot_enabled"]),  # BL-NEW-MOONSHOT-OPT-OUT
```

(Or positional `bool(row[12])` if the existing pattern uses positional indexing — verify against current `params.py:200-201`.)

Update SELECT to include `moonshot_enabled`.

## Evaluator branch

In `scout/trading/evaluator.py:463-478`:

```python
if moonshot_armed_at is not None:
    # BL-067 A1: compose moonshot floor with locked trail. When
    # conviction-lock has overlaid sp.trail_pct above (e.g., to 35%
    # at stack=4), the locked trail wins whenever wider than the
    # moonshot constant.
    if not sp.moonshot_enabled:
        # BL-NEW-MOONSHOT-OPT-OUT: signal opted out of the moonshot
        # regime. Use sp.trail_pct directly without the global floor.
        # Note: sp.trail_pct here ALREADY reflects any BL-067
        # conviction-lock overlay applied upstream in the evaluator
        # cascade — opting out of moonshot does NOT bypass the lock's
        # widening effect. See tasks/findings_moonshot_floor_nullification.md
        # §3.2 for the conviction-lock interaction matrix.
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

## Test surface (8 tests, +1 from plan-stage reviewer RECOMMEND)

1. `test_moonshot_floor_applies_by_default` — pin current behavior (moonshot_enabled=1)
2. `test_moonshot_floor_skipped_when_opted_out` — sp.trail_pct=15, peak ≥ 40, opt-out → trail at 15% not 30%
3. `test_moonshot_opt_out_low_peak_path_unchanged` — peak < low_peak_threshold uses trail_pct_low_peak regardless of moonshot_enabled
4. `test_moonshot_opt_out_pre_moonshot_path_unchanged` — 20 ≤ peak < 40 uses sp.trail_pct directly
5. `test_moonshot_opt_out_with_conviction_lock` — locked-wider sp.trail_pct (35%) preserved when moonshot_enabled=0
6. `test_signal_params_has_moonshot_enabled_column` — migration creates the column
7. `test_moonshot_enabled_defaults_to_1_for_all_seed_signals` — default opt-in for existing rows
8. `test_migration_idempotent_on_rerun` — calling `_migrate_moonshot_opt_out_column` twice is a no-op (per plan-stage policy reviewer RECOMMEND)

## PR description requirements

Per plan-stage policy reviewer (Q4 hold-rationale + Q7 RECOMMEND):
- Explicitly call out: **DO NOT DEPLOY UNTIL 2026-05-13 SOAK REVIEW**
- Name the 3 currently-soaking signals: **losers_contrarian, gainers_early, BL-NEW-HPF dry-run**
- State the deploy hold is signal-soak-specific (default opt-in is no behavior change), so the merge is safe but `git pull` on VPS is not

## Done criteria (delta from plan)

- 8 tests pass (7 from plan + 1 idempotency from design-stage)
- `moonshot_enabled: bool = True` default in SignalParams dataclass (load-bearing for Settings fallback)
- Marker name `bl_moonshot_opt_out_v1` literal in code (no hedge)
- Code comment in evaluator opt-out branch documents conviction-lock interaction
- PR description has explicit "HOLD DEPLOY UNTIL 2026-05-13" + names the 3 soaking signals
