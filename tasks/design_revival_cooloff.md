**New primitives introduced:** Same as `plan_revival_cooloff.md` — see plan for the full list. This design doc derives the interface contracts + event/log shapes from the plan.

# Design: BL-NEW-REVIVAL-COOLOFF — interface contracts

Companion to `tasks/plan_revival_cooloff.md`. Plan covers scope + TDD task breakdown; this doc nails down the precise interface contracts, query shapes, and observability hooks so the implementer doesn't have to make tertiary decisions during build.

## Function signature

```python
async def revive_signal_with_baseline(
    self,
    signal_type: str,
    *,
    reason: str,
    operator: str = "operator",
    force: bool = False,
) -> None:
```

**Semantics:**
- `signal_type` — required, must exist in `signal_params` table; raises `ValueError("unknown signal_type: ...")` otherwise (existing behavior, unchanged).
- `reason` — required string for audit row.
- `operator` — applied_by field for audit row; defaults to `"operator"`. Tests + scripts can override (e.g., `"alice"`) but the cool-off query specifically filters on `applied_by = 'operator'` so non-default operator values bypass the cool-off naturally. This is INTENTIONAL — calls from automation paths that don't want cool-off enforcement just pass a different `operator` string. Documented in the docstring.
- `force` — boolean bypass. When `True`, skips the cool-off check entirely AND tags the audit reason with `[force=True bypass of revival cool-off]` AND emits `revive_signal_force_bypass` structlog WARNING event with the signal_type + timestamp + reason fields.

## SQL query for cool-off check (positive filter — POST-REVIEWER-FIX)

```sql
SELECT applied_at FROM signal_params_audit
WHERE signal_type = ?
  AND field_name = 'enabled'
  AND old_value = '0'
  AND new_value = '1'
  AND applied_by = 'operator'
ORDER BY applied_at DESC LIMIT 1
```

**Why positive filter?** Both #1 plan-stage reviewers (policy + scope) flagged the original negative filter (`applied_by != 'auto_suspend'`) as MUST-FIX. Reasoning:
- `calibrate.py:335` already writes audit rows with `applied_by='calibration'`. They have `field_name='trail_pct'` etc., NOT `field_name='enabled'`, so they're filtered out today by the `field_name='enabled'` clause. BUT if a future calibration path adds an enabled-toggle (e.g., "auto-reenable after calibration boost"), the negative filter would silently count it as an operator revival.
- The positive filter is strictly safer: matches only what `revive_signal_with_baseline` actually writes when called with default `operator="operator"`.
- Future automated paths that legitimately need to revive without triggering the cool-off pass a different `operator=` value (clean separation).

## Cool-off comparison

```python
if row is not None:
    settings = get_settings()
    cool_off_days = getattr(settings, "SIGNAL_REVIVAL_MIN_SOAK_DAYS", 7)
    if cool_off_days > 0:
        last_at = datetime.fromisoformat(row[0])
        delta = datetime.now(timezone.utc) - last_at
        if delta < timedelta(days=cool_off_days):
            days_remaining = max(0, cool_off_days - int(delta.total_seconds() // 86400))
            raise ValueError(
                f"revive_signal_with_baseline cool-off: "
                f"{signal_type} was last revived at {row[0]} "
                f"({delta.days} days ago); minimum {cool_off_days} "
                f"days required between consecutive revivals "
                f"({days_remaining} days remaining). "
                f"Pass force=True to bypass."
            )
```

`SIGNAL_REVIVAL_MIN_SOAK_DAYS=0` disables the check (no `cool_off_days > 0` branch runs); the existing audit query is harmless extra read.

## Force=True bypass — observability hook (POST-RECOMMEND)

When `force=True`:

```python
if force:
    log.warning(
        "revive_signal_force_bypass",
        signal_type=signal_type,
        operator=operator,
        reason=reason,
        # If a prior revival exists, surface it; else None (force on first
        # revival is legal but worth logging as audit-trail mark).
        prior_revival_at=row[0] if row is not None else None,
    )
audit_reason = reason
if force:
    audit_reason = f"{reason} [force=True bypass of revival cool-off]"
```

The structlog event surfaces in journalctl on VPS so operator dashboards / heartbeat aggregators can detect repeated `force=True` usage without a separate DB query. Audit row + log event are independent record paths.

## Settings field placement

```python
# Auto-suspension thresholds. PNL_THRESHOLD requires at least MIN_TRADES;
# HARD_LOSS bypasses the trade floor for catastrophic bleed.
SIGNAL_SUSPEND_PNL_THRESHOLD_USD: float = -200.0
SIGNAL_SUSPEND_HARD_LOSS_USD: float = -500.0
SIGNAL_SUSPEND_MIN_TRADES: int = 50

# Revival cool-off (BL-NEW-REVIVAL-COOLOFF). Minimum days between
# consecutive operator-issued revivals of the same signal via
# Database.revive_signal_with_baseline. 0 disables. Bypass per-call
# with force=True.
SIGNAL_REVIVAL_MIN_SOAK_DAYS: int = 7
```

Visual separation via comment block. Settings field stays in the SUSPEND block (near other signal-lifecycle knobs) but the comment makes the revival distinction explicit.

## Validator

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

## Behavior matrix

| Caller | force | Last revival audit row | Outcome |
|---|---|---|---|
| First revival ever | False | None | succeeds, writes audit |
| First revival ever | True | None | succeeds, writes audit + WARNING log |
| Second revival, < 7d after | False | exists | raises ValueError |
| Second revival, < 7d after | True | exists | succeeds, writes audit + WARNING log + `[force=True bypass...]` reason marker |
| Second revival, >= 7d after | False | exists | succeeds (cool-off cleared), writes audit |
| Second revival, >= 7d after | True | exists | succeeds, writes audit + WARNING log (force is logged regardless of whether it would have been needed) |
| Cross-signal: revive A then revive B | False | A has audit, B has none | both succeed (per-signal independence) |
| Operator suspends signal manually then revives | False | the suspension audit row is 1→0, NOT counted | first revival post-suspend succeeds (no prior 0→1 row) |
| Settings: SIGNAL_REVIVAL_MIN_SOAK_DAYS=0 | False | exists, recent | succeeds (check disabled) |

## Test invariants (from plan, refined)

The plan's 6 tests cover:
1. First revival never blocks
2. Within-window blocks (raises ValueError matching `cool-off`)
3. After-window allows (backdate audit row, retry)
4. force=True bypasses cool-off
5. force=True audit row contains `force` or `bypass` marker
6. Cross-signal independence

**Additions per design review:**
7. force=True emits `revive_signal_force_bypass` structlog event (use `caplog` fixture)
8. Operator-suspension-then-revive path (corner case from plan-policy reviewer Q4) — first revival post-operator-suspend succeeds because the operator-suspend audit row is 1→0, not 0→1.

Total: 8 new tests, 21 existing → 29 total in test_signal_params_auto_suspend.py.

## Done criteria (delta from plan)

- 29 tests pass (21 existing + 8 new — original 6 + 2 design-stage additions)
- Positive filter `applied_by = 'operator'` in the SQL query (NOT negative)
- WARNING-level structlog event `revive_signal_force_bypass` on every force=True call
- Audit row carries `[force=True bypass of revival cool-off]` marker on every force=True call
