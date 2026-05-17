**New primitives introduced:** Same as `tasks/plan_first_signal_retirement_decision.md` post V38/V39 fold — NONE. Pure decision artifact + operator-instruction block referencing existing `Database.revive_signal_with_baseline` helper.

# Design: BL-NEW-FIRST-SIGNAL-RETIREMENT-DECISION

**Plan reference:** `tasks/plan_first_signal_retirement_decision.md` (`e4bd974` + V38/V39 fold `f003b2b`)
**Cycle:** 9 of autonomous backlog knockdown.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Auto-suspend audit-trail interpretation | None | Project-internal — `scout/trading/auto_suspend.py` is source of truth |
| Signal-revival decision frameworks | None | Project-internal trading decision |
| Database revival helper invocation | N/A — using existing in-tree `Database.revive_signal_with_baseline` | Reuse, don't reinvent |

awesome-hermes-agent: 404 (consistent). **Verdict:** custom decision doc using existing in-tree primitives only.

## Design decisions

### D1. Decision is REVIVE-AND-SOAK (Option A), not bare DEFER

Plan's V38 fold established the affirmative case: positive-tail edge ($45/winner × 44 trades = $2,003; drag concentrated in `closed_expired` time-decay). Bare DEFER (Option C) leaves the dispatcher path live and produces NO new evidence; REVIVE-AND-SOAK produces durable data either way (regression → clean RETIRE, or non-regression → KEEP-PAPER).

### D2. Operator action goes through `Database.revive_signal_with_baseline`

Per V38 MUST-FIX: raw SQL bypasses (a) cool-off check, (b) joint `tg_alert_eligible` restore via DEFAULT_ALLOW_SIGNALS lookup, (c) `BEGIN EXCLUSIVE` atomicity, (d) consistent audit row, (e) structlog observability hooks. The helper exists at `scout/db.py:4056` for exactly this case.

The PR ships the doc + the operator invocation block; the actual revival is operator-manual.

**V40 MUST-FIX:** `applied_by='operator'` (helper default), NOT `'operator_cycle9_manual'`. The cool-off SELECT at `db.py:4113` filters on the literal string `'operator'`. Using a custom marker would silently bypass `BL-NEW-REVIVAL-COOLOFF` for any FUTURE revival (the marker row wouldn't match the cool-off filter). Cycle 9 context lives in the `reason=` field instead.

**V41 SHOULD-FIX:** the operator block now `systemctl stop`s the service BEFORE the revival python and `start`s AFTER (instead of post-only `restart`). Prevents the helper's `BEGIN EXCLUSIVE` from racing with live writers + hitting aiosqlite's default ~5s busy timeout. `cd /root/gecko-alpha` is required so `get_settings()` reads `.env` from cwd.

### D3. Decision criteria are pre-registered + data-bound

Per CLAUDE.md §11 (data-bound, not calendar-bound). Threshold table in plan §Pre-registered re-evaluation criteria. Key gates:

- n ≥ 10 required before applying the verdict table; n < 10 auto-extends to 28d
- KEEP demands NON-REGRESSION (PnL ≥ 0 AND positive-tail win rate ≥ 17%), not spontaneous profitability
- Escalation threshold (live-roadmap revisit) sits HIGHER at PnL ≥ +$200 + avg winner ≥ +$30

### D4. Memory checkpoint anchors the future decision (V40 SHOULD-FIX — must be self-sufficient)

`~/.claude/projects/C--projects-gecko-alpha/memory/project_first_signal_revival_decision_2026_05_31.md` is the SINGLE-SOURCE-OF-TRUTH for future-self at 2026-05-31. Must include:

- The complete operator revival block (verbatim, copy-paste runnable — not "see plan")
- The complete verdict table inline (not "see plan")
- The cool-off filter pin: `applied_by='operator'` (helper default; using anything else silently bypasses cool-off for future revivals)
- The `n ≥ 10` trip-wire + 28d auto-extend rule
- The four CLEAN invariants (helper signature, tg_alert restore, combined gate, audit schema)

If the plan/design files are renamed/moved, the memory file is still operational.

### D5. No code change in this PR

Pure decision artifact. The plan correctly defers the actual revival to operator-manual execution. Build artifacts:

| File | Type | Content |
|---|---|---|
| `tasks/findings_first_signal_retirement_decision_2026_05_17.md` | NEW | Decision findings doc (mirrors plan §Investigation + §Why-keep-paper + §Pre-registered criteria) |
| `backlog.md` | MODIFY | Flip `BL-NEW-FIRST-SIGNAL-RETIREMENT-DECISION` to SHIPPED-WITH-DECISION |
| `~/.claude/.../memory/project_first_signal_revival_decision_2026_05_31.md` | NEW | Future-self memory checkpoint |

## Cross-file invariants

| Invariant | Source | Verification |
|---|---|---|
| Helper exists with current signature | `scout/db.py:4056-4108` | Pinned by `tests/test_signal_params_auto_suspend.py:414+` (signature drift fails CI, not at 2026-05-31 runtime) |
| Helper restores `tg_alert_eligible` via `DEFAULT_ALLOW_SIGNALS` lookup at EXECUTION time | `scout/db.py:4198` does `from scout.trading.tg_alert_dispatch import DEFAULT_ALLOW_SIGNALS` inside the function body (V40 CLEAN) | `first_signal` ∉ `DEFAULT_ALLOW_SIGNALS` → `restored_to=0`; logged as `signal_revived_tg_eligible`. A future PR adding `first_signal` to `DEFAULT_ALLOW_SIGNALS` would be picked up at operator-run time without code change |
| Combined-gate logic unchanged from PR #79 | `scout/trading/auto_suspend.py:236-237` | V38+V39 both verified |
| `signal_params_audit` schema supports the helper's audit row; only 3 INSERT paths (`auto_suspend.py:143/152`, `db.py:4222/4229`, `calibrate.py:330`) all stamp `applied_at` | V40 CLEAN | No bypass path |
| `SIGNAL_REVIVAL_MIN_SOAK_DAYS` cool-off setting exists | `scout/config.py:612` with validator at `:806-811` (allows 0 to disable) | Helper reads `settings.SIGNAL_REVIVAL_MIN_SOAK_DAYS`; default 7. Operator override to 0 is supported behavior (documented), not a footgun |
| **V40 SHOULD-FIX add — cache invalidation requires service restart** | Helper does NOT call `bump_cache_version()` (cf. `calibrate.py:373`, `auto_suspend.py:352`); in-process `signal_params` cache (5min TTL at `scout/trading/params.py`) would otherwise stall up to 5min after the UPDATE | Operator block prescribes `systemctl stop`/`start` around the revival; that clears the cache. Restart is the ONLY mechanism that guarantees enabled=1 is picked up promptly |
| **V40 SHOULD-FIX add — calibration race** | `auto_suspend.py:88-90` window floor is `MAX(last_calibration_at, drawdown_baseline_at, 30d_default)` | Helper stamps `drawdown_baseline_at=NOW()` but NOT `last_calibration_at`. If calibrate runs between revival and next auto_suspend check, `last_calibration_at` may exceed `drawdown_baseline_at` — benign (window shrinks → more conservative, NOT undone). MAX(...) construction means revival baseline can never be silently undone |

## Commit sequence (3 commits, bisect-safe)

1. `feat(audit): first_signal retirement decision findings (cycle 9 commit 1/3)` — `tasks/findings_first_signal_retirement_decision_2026_05_17.md`
2. `docs(backlog): close BL-NEW-FIRST-SIGNAL-RETIREMENT-DECISION (cycle 9 commit 2/3)` — backlog flip + memory checkpoint
3. (Optional, deferred) operator-side revival happens out-of-band post-merge

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Operator forgets to run revival post-merge | Medium | Low | Memory checkpoint reminds at 2026-05-31; findings doc has operator instruction block |
| Cool-off check trips on revival (first revival → impossible) | Refuted | — | `signal_params_audit` has only auto_suspend row + a `sync_to_env` row (operator) + `conviction_lock_enabled` change — no prior `enabled` revival, so the cool-off SELECT returns no row, no check fires |
| Helper signature changes before operator runs revival | Very Low | Low | Pinned by `tests/test_signal_params_auto_suspend.py` (V40 fold); signature drift fails CI at PR time, not at 2026-05-31 runtime |
| Pre-existing `tg_alert_eligible=0` confuses interpretation | Refuted | — | Helper logs `restored_to=0` for first_signal explicitly; matches existing operator opt-out |
| 14d soak ambiguous outcome | Medium | Low | Verdict table includes EXTEND-SOAK + n<10 auto-extend trip-wire |
| **V41 SHOULD-FIX add — `BEGIN EXCLUSIVE` racing with live writers** | Medium | Low | Operator block prescribes `systemctl stop` BEFORE revival python, `start` AFTER. Avoids aiosqlite's default ~5s busy timeout hitting `database is locked` exception |
| **V40 MUST-FIX add — custom `applied_by` value silently bypasses cool-off** | Refuted | — | Operator block uses helper default `operator='operator'`. Cycle9 context in `reason=` field only |

## Out of scope

- Changes to `auto_suspend.py` (current logic is correct)
- Changes to `revive_signal_with_baseline` (existing helper is correct for this use case)
- Changes to `FIRST_SIGNAL_MIN_SIGNAL_COUNT` admission gate
- Auto-revival policy (operator-manual is the right safety posture)

## Deployment

Doc-only PR. No service restart, no schema change. Operator runs the revival helper invocation manually post-merge if/when they greenlight Option A.
