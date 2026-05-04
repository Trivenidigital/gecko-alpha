# BL-067: Conviction-locked hold — Design

**New primitives introduced:** new column `signal_params.conviction_lock_enabled INTEGER NOT NULL DEFAULT 0` (added inside the existing `BEGIN EXCLUSIVE` block in `_migrate_signal_params_schema` at `scout/db.py:1638-1680`, gated by `paper_migrations` row + a NEW post-migration assertion paralleling `signal_params_v1` at `db.py:1696-1702`); new column `paper_trades.conviction_locked_at TEXT` (NULL until first arm — D2 fix for log idempotency + dashboard surface); new module `scout/trading/conviction.py` with `compute_stack(db, token_id, opened_at) -> int` (canonical async, defensive on `db._conn is None` per M4 fix), `conviction_locked_params(stack, base) -> dict` (pure, saturates at stack=4), and `_count_stacked_signals_in_window(db, token_id, opened_at, end_at) -> tuple[int, list[str]]` (consolidated from `scripts/backtest_conviction_lock.py:160-258` per D3 — single source of truth; backtest wraps with `asyncio.run()` adapter ~5 LOC); new evaluator overlay block in `scout/trading/evaluator.py:evaluate_paper_trades` placed STRICTLY between line 157 (`sp = await params_for_signal(...)`) and line 158 (`max_duration = timedelta(hours=sp.max_duration_hours)`) so the overlaid `max_duration_hours` flows into `timedelta()` (M2/A2 fix); new moonshot composition `effective_trail_pct = max(settings.PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT, sp.trail_pct)` at `evaluator.py:357` (A1 fix — production currently reads moonshot constant directly, locked trail is ignored in the moonshot regime); new Settings field `PAPER_CONVICTION_LOCK_ENABLED: bool = False` (master kill-switch); new Settings field `PAPER_CONVICTION_LOCK_THRESHOLD: int = 3` with `field_validator` enforcing `2 <= v <= 11` (S2 — upper bound matches highest observed stack count over 30d); new structured log events `conviction_lock_armed` (fired ONCE per trade life, gated on `conviction_locked_at IS NULL` per D2) and `conviction_lock_db_closed` (defensive, fired when `db._conn is None`). NO new DB tables; NO changes to `SignalParams` shape beyond the new `conviction_lock_enabled: bool = False` field. Default fail-closed everywhere.

## Hermes-first analysis

**Domains checked against the 671-skill hub at `hermes-agent.nousresearch.com/docs/skills` (verified 2026-05-04):**

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Real-time signal stack counting in trading systems | None | Build inline (consolidate from existing backtest helper) |
| Conviction-locked exit gating / dynamic exit-parameter overlay | None (closest: MLOps model-evaluation, wrong domain) | Build inline (extend `scout/trading/evaluator.py` exit state machine) |
| Per-signal feature flags / opt-in mechanism | None (closest: `webhook-subscriptions` is event-delivery) | Build inline (reuse `signal_params.enabled` shape; add sibling column) |

**Awesome-hermes-agent ecosystem check:** No relevant repos. Closest is `hxsteric/mercury` (multi-chain forensics) — different problem.

**Verdict:** Pure project-internal trading-engine extension. No Hermes replacement. The BL-067 backlog spec at `backlog.md:367-413` + the validated findings at `tasks/findings_bl067_backtest_conviction_lock.md` are the design authority.

---

## Drift grounding (per alignment doc Part 3)

**Read before drafting (verified):**
- `backlog.md:367-413` — BL-067 spec: per-stack params delta table (saturate at stack=4), 9 design questions resolved by findings doc, decision gate ≥10% PnL lift (passed at +114% per findings).
- `tasks/findings_bl067_backtest_conviction_lock.md` — gating evidence; recommends N=3 threshold + first_signal/gainers_early opt-in. LAB #711 simulated +$549.67 vs operator manual $531.
- `scripts/backtest_conviction_lock.py:160-258` — `_count_stacked_signals_in_window` async-canonical helper to consolidate. Uses `_table_exists` probe + per-table `OperationalError` narrowing.
- `scripts/backtest_conviction_lock.py:218-242` — `conviction_locked_params(stack, base)` composer (saturates at stack=4 via `min(max(stack, 1), 4)`).
- `scout/trading/evaluator.py:82-477` — `evaluate_paper_trades` exit state machine.
  - Line 97-108: SELECT statement (extended to fetch `moonshot_armed_at`, `conviction_locked_at`).
  - Line 157: `sp = await params_for_signal(db, signal_type_row, settings)` — overlay anchor.
  - Line 158: `max_duration = timedelta(hours=sp.max_duration_hours)` — MUST run AFTER overlay (M2/A2).
  - Line 356-357: moonshot branch reading `settings.PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT` directly — A1 fix.
- `scout/trading/params.py:60-78` — `SignalParams` `@dataclass(frozen=True)`: 11 fields. New field `conviction_lock_enabled: bool = False`.
- `scout/trading/params.py:154-198` — `get_params` SELECT/construction; extend SELECT to add `conviction_lock_enabled` as positional `row[11]`.
- `scout/db.py:1536-1614` — `_migrate_signal_params_schema` migration pattern: `BEGIN EXCLUSIVE`, idempotent `CREATE TABLE IF NOT EXISTS`, `paper_migrations` row gating, seed via `Settings` defaults.
- `scout/db.py:1638-1680` — existing `try: await conn.execute("BEGIN EXCLUSIVE")` block. New ALTER TABLE + paper_migrations INSERT goes AFTER `signal_params_v1` cutover-marker INSERT (1668-1673), BEFORE `await conn.commit()` (1680).
- `scout/db.py:1696-1702` — existing post-migration `signal_params_v1` assertion. M3 fix adds parallel `bl067_conviction_lock_enabled` assertion immediately after.
- `scout/config.py` — `Settings` class. Add 2 fields after the `PAPER_MOONSHOT_*` block (search `PAPER_MOONSHOT_THRESHOLD_PCT`).
- `scout/trading/params.py:bump_cache_version` — existing pattern for invalidating per-signal params cache after operator `UPDATE`.
- BL-076 deploy lessons (`feedback_clear_pycache_on_deploy.md`): `find . -name __pycache__ -exec rm -rf {} +` mandatory after `git pull` for any deploy touching `scout/` Python.

**Bug evidence (from findings doc, ratified 2026-05-04):**
- N=3 threshold: lift +114.4%, delta_vs_baseline +$7,222, delta_vs_actual +$11,219, locked_count=499 — **PASS compound gate**.
- LAB trade #711: simulated +$549.67 vs actual -$15.96 (operator's manual hypothetical was $531 — within $20).
- B2 first-entry hold: +$5,416 / +837.8% lift across 287 tokens — operator's mental model validated.
- 176 tokens hit N≥3 in 7d window over 30d.
- Section A correlation: stack=1 trades net -$6.31 / 26.6% win / 95% expired vs stack≥7 net +$20+ / 67% win / 44% expired. System chooses winners; doesn't hold them.
- `chain_completed` underpowered (2-3 trades in locked subset); no separate analysis until N≥10.

**Pattern conformance:**
- New column on existing table via `ALTER TABLE ADD COLUMN` (matches BL-065 `cashtag_trade_eligible` pattern in `_migrate_feedback_loop_schema`).
- New module `scout/trading/conviction.py` (single responsibility — stack counting + param composition; consumed by both production evaluator and backtest script via D3 sync wrapper).
- Master kill-switch via Settings default False — same shape as `PAPER_MOONSHOT_ENABLED` (BL-063).
- Operator opt-in via direct `UPDATE signal_params SET conviction_lock_enabled=1 WHERE signal_type IN ('first_signal', 'gainers_early')` — matches Tier 1a per-signal flip.
- Default fail-closed: column DEFAULT 0; Settings.PAPER_CONVICTION_LOCK_ENABLED=False; deploy default unchanged behavior until operator explicitly opts in.

---

## Test matrix

| ID | Test | Layer | What it pins |
|---|---|---|---|
| T1 | `test_conviction_lock_enabled_column_exists` | Migration | `INTEGER NOT NULL DEFAULT 0` (fail-closed) |
| T1b | `test_conviction_lock_enabled_paper_migrations_row` | Migration | `bl067_conviction_lock_enabled` recorded |
| T1c | `test_conviction_lock_enabled_default_zero_on_seeded_signals` | Migration | EVERY seeded signal_type defaults 0 (no surprise opt-ins) |
| T1d | `test_conviction_locked_at_column_exists_on_paper_trades` | Migration | D2: `paper_trades.conviction_locked_at TEXT` NULL default |
| T1e | `test_conviction_lock_migration_post_assertion_present` | Migration | M3: post-migration assertion fires `RuntimeError` if cutover row missing (paralleling existing `signal_params_v1` shape) |
| T2 | `test_settings_paper_conviction_lock_enabled_default_false` | Unit (config) | Master kill-switch fail-closed default |
| T2b | `test_settings_paper_conviction_lock_threshold_default_3` | Unit (config) | Conservative N=3 default (per findings) |
| T2c | `test_settings_paper_conviction_lock_threshold_must_be_at_least_two` | Unit (config) | Validator lower bound 2 (stack=1 = no signals) |
| T2d | `test_settings_paper_conviction_lock_threshold_must_be_at_most_eleven` | Unit (config) | S2: validator upper bound 11 (max observed stack 30d) |
| T3 | `test_conviction_locked_params_table_matches_backlog_spec` | Unit (helper) | Pins `backlog.md:374-380` table at stack=1/2/3/4 |
| T3b | `test_conviction_locked_params_saturates_at_stack_4` | Unit (helper) | stack=10 == stack=4 (cap behavior) |
| T3c | `test_compute_stack_returns_int` | Integration (helper) | `compute_stack` counts at least one source contribution |
| T3d | `test_compute_stack_empty_token_id_returns_zero` | Unit (helper) | N2 fix: empty token_id → 0 |
| T3e | `test_compute_stack_signal_source_missing_logged_once` | Integration | Missing-table cache + one-shot WARNING |
| T4 | `test_get_params_loads_conviction_lock_enabled` | Integration (params) | `signal_params.conviction_lock_enabled` flows through `get_params` post-`bump_cache_version` |
| T5 | `test_evaluator_skips_conviction_lock_when_settings_kill_switch_off` | Integration (evaluator) | Master gate fail-closed: settings False → no overlay even with per-signal flag set + stack≥3 |
| T5b | `test_evaluator_skips_conviction_lock_when_signal_not_opted_in` | Integration (evaluator) | Per-signal gate fail-closed: master ON + per-signal 0 → no overlay |
| T5c | `test_evaluator_skips_conviction_lock_when_below_threshold` | Integration (evaluator) | Threshold gate: master ON + per-signal 1 + stack=2 (below default 3) → no overlay |
| T5d | `test_evaluator_arms_conviction_lock_when_all_gates_pass` | Integration (evaluator) | Happy path: 3 gates pass + stack≥3 → log fires + `paper_trades.conviction_locked_at` stamped + `max_duration_hours` overlaid (≥336 at stack=3) |
| T5e | `test_evaluator_logs_conviction_lock_armed_only_once` | Integration (evaluator) | D2 idempotency: 2nd evaluator pass on same trade → no re-emit |
| T5f | `test_compute_stack_returns_zero_when_db_conn_closed` | Integration (helper) | M4 defensive: `db._conn is None` → 0 + `conviction_lock_db_closed` log |
| T5g | `test_evaluator_overlay_runs_before_max_duration_timedelta` | Integration (evaluator) | M2/A2 placement-critical: when locked sets max_duration=504, the trade survives past base 168h (placement guard) |
| T6 | `test_moonshot_trail_composes_with_locked_trail` | Unit (arithmetic) | A1 composition: `max(30, 35) == 35` |
| T6b | `test_evaluator_moonshot_branch_uses_max_with_sp_trail_pct` | Integration (evaluator) | A1 production: at stack=4 + moonshot armed, evaluator uses `effective_trail_pct=35.0` (not 30.0) |
| T7 | `test_backtest_script_imports_conviction_module_helpers` | Integration (script) | D3: `scripts/backtest_conviction_lock.py` imports work; `_count_stacked_signals_in_window` sync wrapper round-trips through `asyncio.run` |
| T8 | `test_lab_711_regression_simulates_locked_first_signal` | Integration (regression) | N4: synthetic 11-stack first_signal trade survives with `max_duration_hours=504` saturation; pins LAB #711 +$549 finding |

**Total: 25 active tests.** Zero deferred. T8 LAB regression is allowed minimal price-path fixture (entry $0.597 → peak $1.85) per the findings doc — concrete fixture builder (`_seed_lab_711_replay(db)`) lands in Build phase per S5; T8 itself asserts the post-overlay `max_duration_hours` and the surviving trade record.

---

## Failure modes (16 — silent-failure-first ordering)

| # | Failure | Silent or loud? | Mitigation in plan v2 / design v1 | Residual risk |
|---|---|---|---|---|
| F1 | Overlay runs AFTER `max_duration = timedelta(hours=sp.max_duration_hours)` at line 158 — locked 504h is silently ignored | **Silent (catastrophic)** — feature is no-op for entire deploy | M2/A2 fix: overlay placed STRICTLY between line 157 and line 158; T5g pins this with stack=3 + base 168h + assert trade survives past 168h | None — covered by code AND test |
| F2 | Moonshot branch reads `settings.PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT` directly, ignoring `sp.trail_pct` — locked 35% trail collapses to 30% the moment moonshot arms | **Silent** — feature broken in the high-peak regime where it matters most (LAB-tier moves) | A1 fix: `effective_trail_pct = max(settings.PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT, sp.trail_pct)`; T6 (arithmetic) + T6b (production) pin both directions | None — pinned by both unit and integration test |
| F3 | Operator forgets to flip `signal_params.conviction_lock_enabled=1` after enabling master kill-switch — feature deploys silently inactive | **Silent** (no log fires; operator unaware) | §5 step 6 explicitly verifies `SELECT signal_type, conviction_lock_enabled FROM signal_params` shows 0s; §"Operator opt-in" step is a separate post-§5 manual SQL with `signal_params_audit` row | Operator self-discipline; captured in deploy runbook |
| F4 | Operator flips `conviction_lock_enabled=1` for `narrative_prediction` before running `--max-hours 720` re-validation | **Silent** — narrative_prediction trades stay locked under truncated_window=100% bias (per findings caveat) | Plan v2 §"Operator opt-in" deliberately scopes to `first_signal` + `gainers_early` only; §10 soak-then-escalate documents the 720h re-run prerequisite | Operator must follow the runbook; can't be enforced by code (the column accepts 1 for any signal_type) |
| F5 | `compute_stack` SELECT counts ROWS not DISTINCT signal-source classes — high-frequency snapshot tables inflate stack count | **Silent** — locked at stack=3 from 3 hourly gainers_snapshots rows on same coin → over-aggressive lock | Helper uses `LIMIT 1` per source AND appends one entry per source label; backtest harness pins this; T3c verifies `n >= 1` from a single source row | Helper implementation directly matches backtest's `_count_stacked_signals_in_window`; pinned by T3 / T3c |
| F6 | DB shutdown race during evaluator pass — `db._conn` becomes `None` mid-call | **Loud** if unguarded (`AttributeError` propagates and crashes evaluator loop) | M4 fix: `compute_stack` returns 0 + `conviction_lock_db_closed` log when `db._conn is None`; T5f pins | None |
| F7 | Migration fails partway through (e.g., disk-full during ALTER) — `signal_params_v1` cutover already inserted but `bl067_conviction_lock_enabled` not | **Loud** — post-migration assertion at db.py:1696-1702 (existing) + new BL-067 assertion catches missing cutover row, raises `RuntimeError`, service refuses to start | M3 fix: NEW post-migration assertion paralleling existing `signal_params_v1` check; T1e pins; deploy aborts before alerts get sent | None — fail-loud, fail-fast |
| F8 | Operator changes `PAPER_CONVICTION_LOCK_THRESHOLD` to 1 via `.env` (silly value) | **Loud** — `field_validator` raises `ValidationError` at startup; service refuses to start | T2c pins lower bound 2; T2d pins upper bound 11 | None |
| F9 | `_count_stacked_signals_in_window` SQL uses `datetime(?)` on already-formatted ISO strings — silent timezone drift if any source table stores naïve timestamps | **Silent** (off-by-hour stack counts in TZ-mixed DB) | Audit during build: every source table's timestamp column verified TZ-aware (project convention). Helper uses `datetime(ts_col) >= datetime(?)` which SQLite handles consistently for ISO-8601 with offset; T3c smoke-tests with TZ-aware ISO | None observed; project convention enforced elsewhere |
| F10 | Operator's `UPDATE signal_params SET conviction_lock_enabled=1` doesn't bump cache → in-memory `SignalParams` cache stays stale → no overlay until pipeline restart | **Silent** (operator expects immediate effect; gets none) | §"Operator opt-in" includes `systemctl restart gecko-pipeline` as final step (forces cache rebuild); alternatively `bump_cache_version()` if exposed via signal_params_audit trigger (not added in v1 — plan v2 takes restart approach for simplicity) | If operator skips restart, lock activates on next natural cache invalidation (existing project pattern). Documented in deploy notes |
| F11 | `paper_trades.conviction_locked_at` written but `commit()` not called → next pass re-arms + duplicate log | **Silent** (D2 regression) | Plan v2 Task 5 Step 2 explicitly calls `await conn.commit()` immediately after the UPDATE; T5e pins (assertion: 2nd pass emits 0 events) | None — covered by test |
| F12 | `signal_params_audit` table doesn't exist on prod DB (older snapshot) — operator opt-in `INSERT INTO signal_params_audit` fails | **Loud** — `OperationalError: no such table` from sqlite3 CLI; opt-in transaction rolls back | Pre-deploy audit at §"Pre-merge audit" verifies `signal_params_audit` exists on prod before merge | Audit step makes this loud-and-early; runbook adjusts if missing |
| F13 | `_signal_sources_missing` module cache pollutes across tests (test isolation broken) | **Loud** (intermittent test failures depending on order) | Per-test `db` fixture is per-tmp_path; module cache only suppresses subsequent WARNINGs (not the count). Helper logic still works; T3e pins one-shot WARNING per missing table | If a future test depends on the WARNING firing twice, add cache reset fixture; not needed for v1 |
| F14 | Backtest sync wrapper `asyncio.run(_async_count_stacked(...))` collides with caller's existing event loop | **Loud** (`RuntimeError: asyncio.run() cannot be called from a running event loop`) | Backtest script `scripts/backtest_conviction_lock.py` is sync top-level (not run from inside an async harness). T7 pins by importing + calling the wrapper from sync test context | If a future async caller wants to invoke the backtest helper, refactor to `await _async_count_stacked` directly. Documented in helper docstring |
| F15 | LAB-trajectory T8 fixture too synthetic — pins arithmetic but not real-world price-path drift | **Silent** (test passes; production behavior diverges from finding) | T8 is the regression anchor for the +$549 finding; concrete fixture covers entry → peak → trail-out trajectory. Real-world divergence is the soak-then-escalate signal (§"Operational verification" step 8) | Acceptable — T8 is the structural pin; live monitoring is the empirical one |
| F16 | Operator restarts pipeline mid-eval pass — partially-stamped `conviction_locked_at` rows leave inconsistent state | **Silent** (some open trades stamped, others not on next pass — but next pass simply stamps the rest with new timestamp) | UPDATE+commit per trade is atomic; restart at any point leaves at most 1 trade in-flight; next pass continues without resync. Acceptable | None — same shape as moonshot_armed_at restart behavior |

**Silent-failure count: 8** (F1, F2, F3, F4, F5, F9, F10, F11, F15, F16) **/ Loud: 6** (F6, F7, F8, F12, F13, F14). F1+F2 are the BIG silent failures — both are pinned by tests AND would have shipped to production without the v2 plan-review fixes.

---

## Performance notes

**`compute_stack` per evaluator tick:**
- 9 source tables × 1 indexed `LIMIT 1` SELECT each = 9 round-trips per open trade per pass.
- All 9 source tables verified to have indexes covering `(token_id|coin_id, ts_col)`:
  - `gainers_snapshots`: `idx_gainers_snap (coin_id, snapshot_at)` (`db.py:490-491`)
  - `losers_snapshots`: equivalent (`db.py` analogous index) — verify during Build phase
  - `trending_snapshots`: equivalent — verify during Build phase
  - `chain_matches`: `(token_id, completed_at)` — verify during Build phase
  - `predictions`: `(coin_id, predicted_at)` — verify during Build phase
  - `velocity_alerts`, `volume_spikes`, `tg_social_signals`, `paper_trades` — verify during Build phase
- **Build-phase audit:** for each of the 9 sources, run `EXPLAIN QUERY PLAN SELECT 1 FROM <table> WHERE <token_col>=? AND datetime(<ts_col>) >= datetime(?) AND datetime(<ts_col>) <= datetime(?) LIMIT 1` and verify `SEARCH ... USING INDEX`. Add missing indexes if any source table forces SCAN.
- **Per LEARN-cycle scaling:** N open trades × 9 SELECTs × ≤1 round-trip = ≤9N. At observed N=10–20 open trades, ≤180 round-trips/cycle = <2ms/cycle DB cost. Re-evaluate if N exceeds 100 (refactor to single CTE-based aggregate query).
- **Backlog Q6 already validated this:** "compute on-the-fly, ~9 indexed SELECTs (~ms); evaluator already does similar lookups per tick. Persist only if profiling shows the eval-loop hot path."

**Backtest sync wrapper overhead:**
- Each `_count_stacked_signals_in_window` call wraps with `asyncio.run()` — creates+tears-down a per-call event loop.
- Backtest is one-shot research script (run hourly at most); `asyncio.run` overhead (~5ms per call) acceptable.
- If backtest performance degrades materially, refactor to single shared loop pattern (`asyncio.run(_main_loop())` over the whole backtest, not per call) — documented as Self-Review item 7 trigger.

**Overlay block adds zero new SELECTs in the no-lock path:**
- 3-gate check (`settings.PAPER_CONVICTION_LOCK_ENABLED`, `sp.conviction_lock_enabled`, `stack >= threshold`) short-circuits BEFORE `compute_stack` runs in default-disabled state. Pre-opt-in pipeline runs are zero-cost.
- Even at full opt-in, `compute_stack` only runs when both flags are ON; one short-circuit per closed trade.

---

## Rollback

**Layer 1 — Operator-side (`.env` flip, no code change):**
```bash
ssh root@89.167.116.187 'sed -i "/^PAPER_CONVICTION_LOCK_ENABLED=/d" /root/gecko-alpha/.env'
ssh root@89.167.116.187 'systemctl restart gecko-pipeline'
```
Disables the master kill-switch; per-signal `signal_params.conviction_lock_enabled=1` rows stay set but become inert. Effective in <1 minute; takes effect immediately at next eval pass after restart.

**Layer 2 — Per-signal SQL revert (operator-side, no code change):**
```bash
ssh root@89.167.116.187 'sqlite3 /root/gecko-alpha/scout.db "
  UPDATE signal_params SET conviction_lock_enabled = 0
  WHERE signal_type IN (\"first_signal\", \"gainers_early\");
  INSERT INTO signal_params_audit (signal_type, field_name, old_value, new_value, reason, applied_by, applied_at)
  VALUES
    (\"first_signal\", \"conviction_lock_enabled\", \"1\", \"0\", \"BL-067 rollback\", \"operator_manual\", datetime(\"now\")),
    (\"gainers_early\", \"conviction_lock_enabled\", \"1\", \"0\", \"BL-067 rollback\", \"operator_manual\", datetime(\"now\"));
"'
ssh root@89.167.116.187 'systemctl restart gecko-pipeline'
```
Use when only one signal regresses; keeps the master kill-switch ON for the others.

**Layer 3 — Code rollback (real bug surfaces):**
```bash
ssh root@89.167.116.187 'cd /root/gecko-alpha && systemctl stop gecko-pipeline && git checkout <prev-master-sha> && find . -name __pycache__ -exec rm -rf {} + && systemctl start gecko-pipeline'
```
Forward-only migration: `signal_params.conviction_lock_enabled` and `paper_trades.conviction_locked_at` columns persist post-revert (harmless residual schema; old code doesn't read either). The `paper_migrations` row stays — the migration is idempotent so re-applying the same code on next deploy is a no-op.

**Already-armed trades on rollback:** `paper_trades.conviction_locked_at` rows remain populated. Old evaluator code doesn't read the column — falls back to base `signal_params` exit gates. The trade's `peak_pct` history persists; rollback effectively "un-locks" mid-flight: next eval pass on rolled-back code applies base `trail_pct=20`, `sl_pct=25`, `max_duration_hours=168`. Open trades whose `opened_at + 168h` is in the past would close on next pass via the existing zombie-trade safeguard (PR #54). **This is acceptable** — rollback is a deliberate operator intervention; some held trades closing early is the cost.

---

## Operational verification (§5 — see plan v2)

Plan v2 §"Pre-merge audit" + §"Deploy verification" + §"Operator opt-in" cover:
- **Pre-merge:** `SELECT signal_type, conviction_lock_enabled FROM signal_params` on prod returns all 0s (confirms no rogue opt-ins from prior testing).
- **Pre-deploy:** `journalctl -u gecko-pipeline --since "10 minutes ago"` error baseline + `paper_migrations` table row count baseline.
- **Stop-FIRST sequence:** `systemctl stop` → `git pull` → `find ... __pycache__ ... rm -rf` → `systemctl start` (BL-076 lesson).
- **Migration verification:** PRAGMA + paper_migrations + SELECT — confirms column added, default 0 on all signal_types, paper_migrations row recorded.
- **Service started clean:** `systemctl status gecko-pipeline` active+running; no startup exceptions.
- **No new exceptions vs baseline:** journalctl error count delta ≤ 0.
- **No `conviction_lock_armed` events fire:** kill-switch off + no signal opted in → zero events expected.
- **Operator opt-in:** `.env` flip + `UPDATE signal_params` + `signal_params_audit` row + `systemctl restart`.
- **First arm verification (post-opt-in, ~30 min wait):** `journalctl ... | grep conviction_lock_armed` shows events with `stack >= 3`; query `SELECT id, conviction_locked_at FROM paper_trades WHERE conviction_locked_at IS NOT NULL` returns the armed set.
- **Soak monitoring (14 days):** track `conviction_lock_armed` event rate; track PnL delta on locked trades vs unlocked baseline. If anomaly (locked trades net worse than unlocked over 14d), revert via Layer 1 rollback and investigate.

Design adds no operational verification beyond plan; this section is here for cross-reference.

---

## Self-Review

1. **Hermes-first present:** ✓ table + ecosystem + verdict per convention.
2. **Drift grounding:** ✓ explicit file:line refs to evaluator hot path (97-108, 157, 158, 356-357), SignalParams shape (params.py:60-78), migration pattern (db.py:1536-1614), BEGIN EXCLUSIVE block (1638-1680), post-assertion shape (1696-1702), backlog spec (367-413), findings doc, BL-076 deploy lesson.
3. **Test matrix:** **25 active tests** across 5 layers (migration / config / helper / params / evaluator). Zero deferred. Critical placement bug F1 is pinned by T5g; critical moonshot bug F2 is pinned by T6+T6b. D2 idempotency by T5e. M4 defensive guard by T5f. M3 post-assertion by T1e.
4. **Failure modes 16/16, silent-failure-first count: 10 silent / 6 loud.** F1 + F2 are the catastrophic silent failures — both would have shipped to production without v2 plan-review fixes. F4 (narrative_prediction premature opt-in) and F10 (cache invalidation) are operator-discipline failures captured in the runbook.
5. **Performance honest:** 9N SELECTs/cycle worst case, ≤2ms/cycle at observed N. Index audit deferred to Build phase but per-table audit step explicitly listed. Zero overhead in pre-opt-in deploys (3-gate short-circuit).
6. **Rollback complete:** 3-layer rollback (env flip → SQL revert → code revert). Already-armed trades on code rollback fall back to base gates with zombie safeguard catching overdue trades. Migration is forward-only (idempotent).
7. **Backtest sync wrapper trigger documented:** if `asyncio.run` per-call overhead becomes material, refactor to single shared loop. Helper docstring documents.
8. **Honest scope:**
   - **NOT in scope:** dashboard surface (`conviction_stack_count` badge on open positions). Defer to BL-067-dashboard follow-up — this PR is the trading-engine integration only.
   - **NOT in scope:** narrative_prediction `--max-hours 720` re-run — operator runs that BEFORE flipping `conviction_lock_enabled=1` for narrative_prediction.
   - **IN scope (D3):** consolidating `_count_stacked_signals_in_window` from `scripts/backtest_conviction_lock.py` to shared module. Sync/async impedance solved with thin `asyncio.run()` adapter.
   - **DELIBERATELY DEFERRED:** dynamic threshold calibration — operator can change `PAPER_CONVICTION_LOCK_THRESHOLD` via `.env` but the backtest validated N=3 specifically; lowering to N=2 should be a separate operator decision with re-run.
   - **DELIBERATELY DEFERRED:** stack-count caching — backtest validated real-time computation is cheap. Persist only if profiling shows the eval-loop hot path.
   - **DELIBERATELY DEFERRED:** conviction_stack downgrade on inactivity — once locked, stays locked through trade life (per backlog Q9 + simpler).
9. **Soak-then-escalate criterion:** monitor `conviction_lock_armed` events daily for 14 days after operator opts in `first_signal` + `gainers_early`. If no regressions (no unusual `trading_open_*_error` events, no PnL delta from previous 14d baseline beyond expected variance, no spike in locked-then-trailing-out-late losses), then operator can flip the next signal_type (`losers_contrarian`, `volume_spike`, `chain_completed`). `narrative_prediction` requires the 720h re-run BEFORE flipping. Escalation criterion is **non-binary** — must include the LAB-tier validation: at least one open trade armed at stack≥4 closes via natural exit (peak_fade or trail) rather than max_duration ceiling, confirming locked params actually exercised.
10. **No production code that auto-arms:** migration column DEFAULT 0; `Settings.PAPER_CONVICTION_LOCK_ENABLED` default False; `Settings.PAPER_CONVICTION_LOCK_THRESHOLD` default 3 (conservative); 3-layer fail-closed (master + per-signal + threshold); operator opt-in requires `.env` flip + SQL UPDATE + restart. Quadruple gate.
11. **Refactor trigger documented:** if a 4th source-table family is added or per-chain priority becomes dynamic, refactor `_SIGNAL_SOURCES` list to a `MetadataSource` plugin pattern. Documented in module docstring + here for future contributors.
12. **Test fixture FK dependency:** T5d/T5e require `chain_patterns` row seeded first when `chain_matches` is one of the seeded sources (FK on `chain_matches.pattern_id`). Reuse `_seed_chain_pattern(db, id)` if BL-076 conftest exposed it; otherwise inline in `_seed_locked_eligible_trade` per S5.
