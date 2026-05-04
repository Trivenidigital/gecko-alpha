# BL-067 Backtest: Conviction-lock simulation — Design

**New primitives introduced:** `scripts/backtest_conviction_lock.py` (read-only research script); helpers `_count_stacked_signals_in_window`, `_simulate_conviction_locked_exit` (incl. moonshot per A2), `_reconstruct_price_path`, `_load_signal_params` (M3), `_path_density_score` (S4); CLI args `--db`, `--as-of`, `--days`; auto-generated `tasks/findings_bl067_backtest_conviction_lock.{md,json}`. NO production code, NO DB schema changes, NO settings changes.

## Hermes-first analysis

Per plan v2 Hermes-first section: 5/5 negative (no skill or repo for trading-strategy backtesting / paper-trade replay / signal-stack analysis / exit-policy simulation / historical price-path reconstruction). Building inline; reuses `scripts/backtest_v1_signal_stacking.py:68-159` helper pattern.

**Verdict:** Pure project-internal research script. No Hermes-skill replacement.

---

## Drift grounding

Per plan v2 §"Drift grounding": verified file:line refs to `backlog.md:367-413` (BL-067 spec), `scripts/backtest_v1_signal_stacking.py:68-159` (helper to copy verbatim with TODO comment per D4), `scout/db.py:557-600` (paper_trades schema), 5 snapshot tables for price-path reconstruction, `scout/trading/params.py:60-110` (SignalParams), `scout/config.py:267-272` (moonshot constants — threshold=40, trail=30).

---

## Test matrix

| ID | Test | Layer | What it pins |
|---|---|---|---|
| T1 | `test_count_stacked_signals_returns_zero_for_isolated_token` | Helper | Stack count = 0 for token with no signals in window |
| T1b | `test_count_stacked_signals_counts_distinct_sources` | Helper | 3 different sources → stack=3 (DISTINCT-source semantics, BIO principle) |
| T2 | `test_conviction_locked_params_for_stack_count` | Helper | BL-067 spec table at `backlog.md:374-380` (stack 1-4 + saturation at 10) |
| T3 | `test_reconstruct_price_path_returns_chronological_prices` | Helper | UNION of 5 snapshot tables, sorted ASC, prices > 0 |
| T4 | `test_simulate_exit_hits_stop_loss` | Helper (simulator) | Entry 1.0, SL 20%: exit at observed gap-down price (N4) |
| T4b | `test_simulate_exit_hits_trailing_stop` | Helper (simulator) | Peak 50%, trail 20% → close at observed price (N4) |
| T4c | `test_simulate_exit_max_duration` | Helper (simulator) | No SL/trail hit, expiry at last in-window price |
| T4d | `test_simulate_exit_arms_moonshot_at_40pct` | Helper (simulator) | A2 fix: peak ≥ 40 + moonshot_enabled → effective trail = max(base, 30) |
| T4e | `test_simulate_exit_locked_trail_widens_beyond_moonshot` | Helper (simulator) | Stack≥4 trail (35%) widens beyond moonshot's 30% — locked params win |
| T4f1 | `test_load_signal_params_returns_row_when_present` | Helper | M3: reads signal_params row for given signal_type |
| T4f2 | `test_load_signal_params_falls_back_when_missing` | Helper | M3: settings defaults when row/table absent |
| T4g1 | `test_path_density_returns_zero_for_empty_path` | Helper | S4: empty path → density=0 |
| T4g2 | `test_path_density_returns_one_for_hourly_samples` | Helper | S4: 1 sample/hour → density=1.0 |

**Build-phase deferred tests (none).** Section A/B/B2/C/D output is harder to unit-test (depends on real data shapes) — covered by Task 7 smoke test against `scout.db` copy.

---

## Failure modes (12 — silent-failure-first ordering)

| # | Failure | Silent or loud? | Mitigation in plan v2 | Residual risk |
|---|---|---|---|---|
| F1 | Headline lift biased LOW because stack count uses `closed_at` not extended window | **Silent** (bad number, plausible-looking) | M1 fix: count over `[opened_at, opened_at + 504h]` capped at as_of | If conviction-lock causes signals to fire AFTER trade closes (because the system would NOT have closed it yet under locked params), the live system would see those signals — but the historical record only has signals that DID fire under actual conditions. Honest limitation. |
| F2 | Headline lift biased HIGH because moonshot in actual but not in baseline simulation | **Silent** (apples-to-oranges; favors greenlight) | A2 fix: simulator includes moonshot AND Section B uses simulator for BOTH baseline + locked (delta-of-deltas) | Simulator simplifications (no ladder, no peak-fade) cancel symmetrically; residual risk only if conviction-locked params + actual ladder behavior differ in non-symmetric ways |
| F3 | Decision gate fires on noise (lift% on tiny absolute $) | **Silent** (greenlight on $20 delta) | A3 fix: compound gate `lift>=10% AND |delta|>=$100 AND locked>=5` | If actual aggregate is exactly 0, lift% is undefined; check uses `if baseline_total else 0.0` |
| F4 | Operator can't reproduce the LAB +$531 mental model from script output | **Silent** (operator distrusts findings, decision blocked) | A1 fix: Section B2 first-entry-hold simulation explicitly outputs per-token actual-sum vs hypothetical-first-hold | First-entry hold ignores portfolio constraints (slot occupancy); upper-bound estimate. Documented. |
| F5 | Stale 30d window: re-running tomorrow gives different answer for same code | **Silent** (no audit trail) | S3/D5 fix: `--as-of` arg pins window; embedded in JSON output | Operator must remember to pass `--as-of` for reproducible runs; default is `now` (acceptable for first run) |
| F6 | Cohort survey inflated by full-30d window (most active tokens hit N≥3 trivially) | **Silent** (cohort looks larger than reality) | S1/D3 fix: TRUE 7d rolling, 24h step | If a token has N=3 sources fire in a single 24h burst that doesn't span any of the 7d windows tested, miss-counted. Acceptable approximation. |
| F7 | Per-trade replay ignores portfolio constraints (slot occupancy when trade stays open longer) | Silent (overstates lift in tight-slot scenarios) | A1 partial: documented in plan §"Honest scope"; full counterfactual portfolio sim is v2 work | If slot occupancy is binding, the lift estimate is upper-bound. Currently <20 trades/day so not binding. |
| F8 | Snapshot price-path has multi-hour gaps for tokens not on any leaderboard | **Silent** (simulator misses peaks) | S4 fix: path_density per trade; trades with density < 0.2 excluded from headline + flagged in detailed output | Headline lift skips low-density trades; detailed output preserves them with flag |
| F9 | Recent trades (< 504h ago at as_of) get clipped sim window | Silent (biases LOW for recent trades) | S6 fix: truncated_window flag per trade in deltas; reported as subset metric | Operator can re-run with `--as-of` 30d in the past to avoid clipping for any specific window |
| F10 | Baseline params hardcoded → wrong baseline → wrong lift | **Silent** | M3 fix: `_load_signal_params` reads `signal_params` per signal_type | If `signal_params` row absent for a signal_type, falls back to settings defaults (warns? no — silent) |
| F11 | SL exits at threshold not observed price → unrealistic uniform losses | Silent (understates SL depth) | N4 fix: SL exits at observed price (gap-down realistic) | None |
| F12 | datetime string-comparison bug (`min("2026-05-04T...", "2026-05-04 ...")`) | **Loud** under specific input shapes; **silent** for matching formats | N3 fix: parse both to datetime, min(), .isoformat(); applied via `_min_iso_ts` helper in Section C | None |
| F13 | `signal_params` table empty/absent → `_load_signal_params` returns hardcoded defaults | **Silent** (lift number runs against wrong baseline) | Smoke test (Task 7) prints actual baseline params per signal_type so operator sees if defaults are firing | If prod has signal_params populated by Tier 1a but a specific signal_type row is missing, that signal's baseline silently falls to defaults |
| F14 | chain_completed historical signal_data has empty symbol+name pre-BL-076 | **Silent (benign)** — stack count uses coin_id/token_id columns, NOT signal_data JSON | None needed — verified benign | None |
| F15 | Saturation at stack=4 hides upside for stack>=6 tokens (BIO/LAB) | **Silent** (locked params for stack=10 are same as stack=4) | Documented as BL-067 spec choice. If Section D shows >=5 tokens with stack>=6 AND those tokens hit `held_to_end` (locked params capped before trade naturally exited), recommend BL-067-v2 spec extension to stack=6+ in findings §5 | If saturation matters, BL-067-v2 (`--max-stack-bonus`) re-run before production rollout |
| F16 | Single-window regime caveat — backtest run on whatever 30d window operator picks | **Silent** (findings don't generalize across regimes) | Re-run quarterly with shifted `--as-of`; track lift drift | None — findings are point-estimate, not stationary |

---

## Output schema

`tasks/findings_bl067_backtest_conviction_lock.json`:

```json
{
  "as_of": "2026-05-04T13:00:00+00:00",
  "days": 30,
  "section_a": {"<stack_count>": {"n": int, "avg_pnl_usd": float, ...}},
  "section_b_n2": {
    "threshold": 2,
    "actual_total": float, "baseline_total": float, "sim_total": float,
    "delta_vs_baseline": float, "delta_vs_actual": float,
    "lift_pct": float, "gate_passed": bool,
    "locked_count": int,
    "by_signal": {"<signal_type>": {"n_locked": int, ...}},
    "deltas": [{"id": int, "token_id": str, "stack": int,
                "is_locked": bool, "truncated_window": bool,
                "path_density": float, "actual_pnl": float,
                "baseline_pnl": float, "sim_pnl": float, ...}]
  },
  "section_b_n3": {... same shape},
  "section_b2": [{"token_id": str, "trade_count": int, "stack": int,
                  "actual_sum_pnl": float, "first_entry_hold_pnl": float,
                  "delta": float, ...}],
  "section_c": {"bio-protocol": [...], "lab": [...]},
  "section_d": {"candidates_count": int, "n3_count": int, "n5_count": int,
                "top_n3": [[token_id, stack], ...]}
}
```

Markdown: §1 sections auto-filled from JSON; §2 (decision narrative) + §5 (resolved design questions) operator-edited per N1.

---

## Performance notes

- `_count_stacked_signals_in_window` makes ~9 SELECTs per call. Section A: N_trades calls. Section B: N_trades × 1 call. Section B2: N_unique_tokens × 1. Section D: N_candidates × N_days (7d rolling) calls.
- At observed scale (30d ≈ 1500 trades, ~500 unique tokens, ~few hundred cohort candidates × 30 days = ~10k stack-count evaluations), full run < 30s on local Python with sqlite WAL on prod DB copy.
- Each stack-count helper has 8-9 indexed SELECTs (one per source); query is O(log n) via `coin_id` indexes verified in BL-076 design.
- No write path — read-only against scout.db copy.

---

## Rollback

**No rollback required.** Pure research script. PR adds files only:
- `scripts/backtest_conviction_lock.py`
- `tests/test_backtest_conviction_lock.py`

Reverting the PR removes both. No DB state, no settings, no service.

---

## Operational notes (Task 7 — Run + findings)

After PR merge, the run task is operator-facing:

1. **SCP scout.db locally** (read-only research; don't run on prod live):
   ```bash
   ssh root@89.167.116.187 'cp /root/gecko-alpha/scout.db /tmp/scout-bl067.db'
   scp root@89.167.116.187:/tmp/scout-bl067.db /tmp/scout-bl067.db
   ```
2. **Run backtest (REQUIRED `--as-of` for reproducible findings):**
   ```bash
   cd C:/projects/gecko-alpha && uv run python scripts/backtest_conviction_lock.py \
       --db /tmp/scout-bl067.db \
       --as-of "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
       --days 30 | tee /tmp/bl067_run.txt
   ```
3. **Findings doc:** auto-emitted at `tasks/findings_bl067_backtest_conviction_lock.md`. Edit §2 (decision) + §5 (resolved design questions).

4. **Decision matrix (B × B2, per A2 fix):**

   | Section B (per-trade) | Section B2 (first-entry hold) | Action |
   |---|---|---|
   | PASS at both N=2 + N=3 | (any) | **GREENLIGHT BL-067 at N=3** (conservative) |
   | PASS at N=2 only | (any) | **GREENLIGHT-AT-N=3-ONLY** unless per-signal subset shows specific signal_type benefits at N=2 with locked_count >= 5 AND lift >= 20% |
   | PASS at N=3 only | PASS | GREENLIGHT BL-067 at N=3 |
   | FAIL both N | PASS | **CLOSE BL-067 as specified, OPEN BL-067-alt: first-entry sticky hold** for design (different feature — early-entry-hold not extending current trades) |
   | FAIL both N | FAIL | **CLOSE BL-067 as won't-fix**; document in backlog.md |

5. **If greenlight:** open BL-067 implementation PR (`scout/trading/conviction.py` + DB column + dashboard surface) per the BL-067 backlog spec.

6. **If GREENLIGHT but per-signal table flags `narrative_prediction.truncated_window_rate > 30%`:** before implementation, re-run with `--max-hours 720` (or skip narrative_prediction from initial rollout via `signal_params.conviction_lock_enabled=0`).

---

## Self-Review

1. **Hermes-first present:** ✓ table + ecosystem + verdict per convention.
2. **Drift grounding:** ✓ explicit file:line refs verified.
3. **Test matrix:** 13 active helper tests; section-level integration covered by Task 7 smoke test.
4. **Failure modes:** 12; **6 silent** (F1, F2, F3, F4, F8, F10) all mitigated; **6 loud or accepted** (F5-F7 documented limitations, F9 flag-not-block, F11-F12 fixed).
5. **Performance honest:** sub-30s for full 30d run; all helpers O(log n) via existing indexes.
6. **Rollback complete:** N/A — research script, files-only revert.
7. **Decision gate well-defined:** compound (lift% AND |delta| AND locked count) + dual-threshold sweep (N=2 + N=3).
8. **Methodology documented honestly:** per-trade replay (Section B) + first-entry hold (Section B2) — both upper-bound; counterfactual portfolio sim deferred to v2 with reasoning.
9. **Resume protocol respected:** zero production code; per backlog.md:412 the conviction-lock production code is gated on this backtest's output.
